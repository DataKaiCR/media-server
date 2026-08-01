import fcntl
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from digital_librarian.cli import AuditAlreadyRunning, run
from digital_librarian.config import ConfigError, load_config
from digital_librarian.formats import inspect_file
from digital_librarian.report import publish_report, report_document
from digital_librarian.scanner import audit_collection


JPEG = (
    b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x10\x00\x20\x03"
    b"\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (32).to_bytes(4, "big") + (16).to_bytes(4, "big")
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


class TemporaryCollections(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.photos = self.root / "photos"
        self.books = self.root / "books"
        self.reports = self.root / "private" / "reports"
        self.photos.mkdir()
        self.books.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, report_dir=None, collections=None) -> Path:
        report_dir = report_dir or self.reports
        collections = collections or [
            ("photos", "photos", self.photos),
            ("books", "books", self.books),
        ]
        lines = ["version = 1", f'report_dir = "{report_dir}"', ""]
        for collection_id, kind, root in collections:
            lines.extend(
                [
                    "[[collections]]",
                    f'id = "{collection_id}"',
                    f'kind = "{kind}"',
                    f'root = "{root}"',
                    'exclude_globs = ["ignored/**"]',
                    "",
                ]
            )
        path = self.root / "librarian.toml"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path


class ConfigTest(TemporaryCollections):
    def test_loads_disjoint_absolute_collections(self) -> None:
        config = load_config(self.write_config())
        self.assertEqual([row.collection_id for row in config.collections], ["photos", "books"])
        self.assertEqual(config.report_dir, self.reports.resolve())

    def test_rejects_report_inside_collection_and_overlapping_roots(self) -> None:
        with self.assertRaisesRegex(ConfigError, "outside"):
            load_config(self.write_config(report_dir=self.photos / "reports"))
        nested = self.photos / "nested"
        nested.mkdir()
        with self.assertRaisesRegex(ConfigError, "overlap"):
            load_config(
                self.write_config(
                    collections=[("photos", "photos", self.photos), ("nested", "books", nested)]
                )
            )

    def test_rejects_symlinked_collection_root(self) -> None:
        link = self.root / "photo-link"
        link.symlink_to(self.photos, target_is_directory=True)
        with self.assertRaisesRegex(ConfigError, "symlink"):
            load_config(
                self.write_config(collections=[("photos", "photos", link)])
            )

    def test_rejects_relative_paths_and_unsafe_ids(self) -> None:
        path = self.root / "bad.toml"
        path.write_text(
            'version=1\nreport_dir="relative"\n[[collections]]\nid="Private Name"\nkind="photos"\nroot="relative"\n',
            encoding="utf-8",
        )
        with self.assertRaises(ConfigError):
            load_config(path)


class FormatInspectionTest(TemporaryCollections):
    def test_reads_image_dimensions_and_pdf_termination(self) -> None:
        image = self.photos / "image.jpg"
        image.write_bytes(JPEG)
        detected, metadata, findings = inspect_file(image, ".jpg", "photos")
        self.assertEqual(detected, "jpeg")
        self.assertEqual(metadata["width"], 32)
        self.assertEqual(metadata["height"], 16)
        self.assertEqual(findings, [])

        pdf = self.books / "book.pdf"
        pdf.write_bytes(PDF)
        detected, _, findings = inspect_file(pdf, ".pdf", "books")
        self.assertEqual(detected, "pdf")
        self.assertNotIn("pdf-missing-eof", {finding.code for finding in findings})

    def test_detects_extension_mismatch_and_truncated_pdf(self) -> None:
        mismatch = self.photos / "image.png"
        mismatch.write_bytes(JPEG)
        _, _, findings = inspect_file(mismatch, ".png", "photos")
        self.assertIn("extension-format-mismatch", {finding.code for finding in findings})
        pdf = self.books / "broken.pdf"
        pdf.write_bytes(b"%PDF-1.4\nmissing trailer")
        _, _, findings = inspect_file(pdf, ".pdf", "books")
        self.assertIn("pdf-missing-eof", {finding.code for finding in findings})

    def test_validates_epub_container_metadata(self) -> None:
        epub = self.books / "book.epub"
        with zipfile.ZipFile(epub, "w") as archive:
            archive.writestr("mimetype", "application/epub+zip")
            archive.writestr("META-INF/container.xml", "<container/>")
        detected, metadata, findings = inspect_file(epub, ".epub", "books")
        self.assertEqual(detected, "epub")
        self.assertEqual(metadata["archive_members"], 2)
        self.assertEqual(findings, [])

    def test_reports_unknown_book_extensions_without_modifying_file(self) -> None:
        unknown = self.books / "notes.bin"
        unknown.write_bytes(b"private content")
        before = (unknown.read_bytes(), unknown.stat().st_mtime_ns)
        _, _, findings = inspect_file(unknown, ".bin", "books")
        self.assertIn("unsupported-extension", {finding.code for finding in findings})
        self.assertEqual((unknown.read_bytes(), unknown.stat().st_mtime_ns), before)


class ScannerTest(TemporaryCollections):
    def test_finds_duplicates_orphans_case_collisions_and_skips_symlinks(self) -> None:
        (self.photos / "A.jpg").write_bytes(JPEG)
        (self.photos / "a.JPG").write_bytes(JPEG)
        (self.photos / "orphan.xmp").write_text("metadata", encoding="utf-8")
        (self.photos / "linked.jpg").symlink_to(self.photos / "A.jpg")
        ignored = self.photos / "ignored"
        ignored.mkdir()
        (ignored / "hidden.jpg").write_bytes(JPEG)
        config = load_config(self.write_config()).collections[0]
        report = audit_collection(config)
        codes = [finding.code for finding in report.findings]
        self.assertIn("exact-duplicate-group", codes)
        self.assertIn("case-colliding-filenames", codes)
        self.assertIn("orphan-photo-sidecar", codes)
        self.assertIn("symlink-skipped", codes)
        self.assertNotIn("ignored/hidden.jpg", [record.relative_path for record in report.files])
        duplicates = next(f for f in report.findings if f.code == "exact-duplicate-group")
        self.assertEqual(duplicates.evidence["count"], 2)
        self.assertEqual(len(duplicates.evidence["sha256"]), 64)

    def test_hashes_only_equal_size_candidates(self) -> None:
        (self.books / "one.pdf").write_bytes(PDF)
        (self.books / "two.pdf").write_bytes(PDF + b" ")
        config = load_config(self.write_config()).collections[1]
        report = audit_collection(config)
        self.assertTrue(all(record.sha256 is None for record in report.files))


class ReportTest(TemporaryCollections):
    def test_rejects_concurrent_audit_for_the_same_report_directory(self) -> None:
        self.reports.mkdir(parents=True)
        lock_path = self.reports / ".audit.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(AuditAlreadyRunning):
                run(self.write_config())

    def test_end_to_end_report_is_private_atomic_and_report_only(self) -> None:
        photo = self.photos / "photo.png"
        photo.write_bytes(PNG)
        book = self.books / "book.pdf"
        book.write_bytes(PDF)
        source_hashes = {
            photo: hashlib.sha256(photo.read_bytes()).hexdigest(),
            book: hashlib.sha256(book.read_bytes()).hexdigest(),
        }
        result = run(self.write_config())
        destination = self.reports / result["report_file"]
        document = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(result["mode"], "report-only")
        self.assertEqual(document["proposed_actions"], [])
        self.assertEqual(document["summary"]["file_count"], 2)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.reports.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            hashlib.sha256(destination.read_bytes()).hexdigest(),
            result["report_sha256"],
        )
        for path, digest in source_hashes.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_report_document_contains_no_actions(self) -> None:
        reports = [audit_collection(load_config(self.write_config()).collections[0])]
        document = report_document(reports, "a" * 64)
        destination, digest = publish_report(self.reports, document)
        self.assertTrue(destination.is_file())
        self.assertEqual(len(digest), 64)
        self.assertEqual(document["mode"], "report-only")
        self.assertEqual(document["proposed_actions"], [])


if __name__ == "__main__":
    unittest.main()
