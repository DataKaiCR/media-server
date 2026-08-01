import fcntl
import hashlib
import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from digital_librarian.book_common import BoundedProcessResult, normalize_isbn, run_bounded
from digital_librarian.books import add_bibliographic_groups, add_external_cover_evidence
from digital_librarian.cli import AuditAlreadyRunning, run
from digital_librarian.config import BookAnalysisConfig, ConfigError, load_config
from digital_librarian.formats import inspect_file
from digital_librarian.mobi_analysis import analyze_mobi
from digital_librarian.model import CollectionReport, FileRecord
from digital_librarian.pdf_analysis import analyze_pdf
from digital_librarian.report import publish_report, report_document
from digital_librarian.scanner import audit_collection


JPEG = (
    b"\xff\xd8\xff\xc0\x00\x11\x08\x00\x10\x00\x20\x03"
    b"\x01\x11\x00\x02\x11\x00\x03\x11\x00\xff\xd9"
)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (32).to_bytes(4, "big") + (16).to_bytes(4, "big")
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"
ISBN = "9780306406157"


def write_epub(path: Path, *, broken_spine: bool = False) -> None:
    container = """<?xml version="1.0"?>
    <container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
      <rootfiles><rootfile full-path="OPS/package.opf" media-type="application/oebps-package+xml"/></rootfiles>
    </container>"""
    spine_id = "missing" if broken_spine else "chapter"
    package = f"""<?xml version="1.0"?>
    <package xmlns="http://www.idpf.org/2007/opf" version="3.0">
      <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
        <dc:title>Example Book</dc:title><dc:creator>Example Author</dc:creator>
        <dc:language>en</dc:language><dc:identifier>urn:isbn:{ISBN}</dc:identifier>
        <meta property="belongs-to-collection" id="series">Example Series</meta>
        <meta refines="#series" property="collection-type">series</meta>
        <meta refines="#series" property="group-position">2</meta>
      </metadata>
      <manifest>
        <item id="cover" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
        <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
        <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
      </manifest>
      <spine><itemref idref="{spine_id}"/></spine>
    </package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/package.opf", package)
        archive.writestr("OPS/cover.jpg", JPEG)
        archive.writestr("OPS/nav.xhtml", "<html/>")
        archive.writestr("OPS/chapter.xhtml", "<html/>")


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
        for entry in collections:
            collection_id, kind, root, *optional_role = entry
            lines.extend(
                [
                    "[[collections]]",
                    f'id = "{collection_id}"',
                    f'kind = "{kind}"',
                    *([f'role = "{optional_role[0]}"'] if optional_role else []),
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

    def test_loads_bounded_book_analysis_and_intake_role(self) -> None:
        self.books.chmod(0o700)
        path = self.write_config(
            collections=[("phone-intake", "books", self.books, "intake")]
        )
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n[book_analysis]\npdf_text_layer = false\n"
                "pdf_sample_pages = 8\nparser_timeout_seconds = 10\n"
                "max_parser_output_bytes = 131072\n"
                "max_parser_memory_bytes = 536870912\n"
            )
        config = load_config(path)
        self.assertEqual(config.collections[0].role, "intake")
        self.assertFalse(config.book_analysis.pdf_text_layer)
        self.assertEqual(config.book_analysis.pdf_sample_pages, 8)
        self.assertEqual(config.book_analysis.max_parser_memory_bytes, 536870912)

    def test_rejects_unsafe_book_analysis_and_nonbook_intake(self) -> None:
        path = self.write_config(
            collections=[("photo-intake", "photos", self.photos, "intake")]
        )
        with self.assertRaisesRegex(ConfigError, "limited"):
            load_config(path)
        path = self.write_config(
            collections=[("book-intake", "books", self.books, "intake")]
        )
        with self.assertRaisesRegex(ConfigError, "0700"):
            load_config(path)
        path = self.write_config()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n[book_analysis]\npdf_sample_pages = 500\n")
        with self.assertRaisesRegex(ConfigError, "between"):
            load_config(path)

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

    def test_validates_epub_structure_and_bibliographic_metadata(self) -> None:
        epub = self.books / "book.epub"
        write_epub(epub)
        detected, metadata, findings = inspect_file(epub, ".epub", "books")
        self.assertEqual(detected, "epub")
        self.assertEqual(metadata["archive_members"], 6)
        self.assertEqual(metadata["epub"]["spine_items"], 1)
        self.assertTrue(metadata["epub"]["embedded_cover"])
        self.assertEqual(metadata["bibliographic"]["languages"], ["en"])
        self.assertEqual(
            metadata["bibliographic"]["identifiers"],
            [{"scheme": "isbn-13", "value": ISBN}],
        )
        self.assertEqual(metadata["bibliographic"]["series"], "Example Series")
        self.assertEqual(metadata["bibliographic"]["volume"], "2")
        self.assertEqual(findings, [])

    def test_reports_broken_epub_spine_without_reading_book_content(self) -> None:
        epub = self.books / "broken.epub"
        write_epub(epub, broken_spine=True)
        _, metadata, findings = inspect_file(epub, ".epub", "books")
        self.assertIn("epub-broken-spine", {finding.code for finding in findings})
        self.assertNotIn("chapter.xhtml", json.dumps(metadata))

    def test_reports_unknown_book_extensions_without_modifying_file(self) -> None:
        unknown = self.books / "notes.bin"
        unknown.write_bytes(b"private content")
        before = (unknown.read_bytes(), unknown.stat().st_mtime_ns)
        _, _, findings = inspect_file(unknown, ".bin", "books")
        self.assertIn("unsupported-extension", {finding.code for finding in findings})
        self.assertEqual((unknown.read_bytes(), unknown.stat().st_mtime_ns), before)


class BookAnalysisTest(TemporaryCollections):
    def test_validates_isbn_checksums(self) -> None:
        self.assertEqual(normalize_isbn("0-306-40615-2"), ISBN)
        self.assertEqual(normalize_isbn(ISBN), ISBN)
        self.assertIsNone(normalize_isbn("9780306406158"))

    def test_pdf_text_evidence_never_persists_extracted_text(self) -> None:
        pdf = self.books / "sample.pdf"
        pdf.write_bytes(PDF)
        private_text = "content that must never appear in a report"
        info = BoundedProcessResult(
            0,
            (
                f"Title: Example Book\nAuthor: Example Author\nKeywords: ISBN {ISBN}\n"
                "Pages: 2\nEncrypted: no\nPDF version: 1.7\n"
            ).encode(),
            b"",
        )
        extracted = BoundedProcessResult(
            0,
            ((private_text + " words " * 100) + "\f").encode(),
            b"",
        )
        with patch(
            "digital_librarian.pdf_analysis.shutil.which",
            side_effect=lambda name: name,
        ), patch(
            "digital_librarian.pdf_analysis.run_bounded",
            side_effect=[info, extracted],
        ):
            metadata, findings = analyze_pdf(pdf, BookAnalysisConfig())
        self.assertEqual(metadata["pdf"]["page_count"], 2)
        self.assertEqual(
            metadata["bibliographic"]["identifiers"],
            [{"scheme": "isbn-13", "value": ISBN}],
        )
        self.assertEqual(
            metadata["pdf"]["text_layer"]["ocr_recommendation"], "review"
        )
        rendered = json.dumps(
            {"metadata": metadata, "findings": [item.to_dict() for item in findings]}
        )
        self.assertNotIn(private_text, rendered)
        self.assertIn("pdf-low-text-density", {item.code for item in findings})

    def test_pdf_ocr_encryption_timeout_and_output_bounds(self) -> None:
        pdf = self.books / "bounded.pdf"
        pdf.write_bytes(PDF)
        info = BoundedProcessResult(
            0, b"Pages: 2\nEncrypted: no\nPDF version: 1.7\n", b""
        )
        no_text = BoundedProcessResult(0, b"\f\f", b"")
        with patch(
            "digital_librarian.pdf_analysis.shutil.which",
            side_effect=lambda name: name,
        ), patch(
            "digital_librarian.pdf_analysis.run_bounded",
            side_effect=[info, no_text],
        ):
            metadata, findings = analyze_pdf(pdf, BookAnalysisConfig())
        self.assertEqual(
            metadata["pdf"]["text_layer"]["ocr_recommendation"], "recommended"
        )
        self.assertIn("pdf-ocr-recommended", {item.code for item in findings})

        with patch(
            "digital_librarian.pdf_analysis.shutil.which", return_value="pdfinfo"
        ), patch(
            "digital_librarian.pdf_analysis.run_bounded",
            return_value=BoundedProcessResult(None, b"", b"", timed_out=True),
        ):
            _, timeout_findings = analyze_pdf(pdf, BookAnalysisConfig())
        self.assertEqual(timeout_findings[0].code, "pdf-validation-incomplete")

        encrypted = BoundedProcessResult(
            1, b"Encrypted: yes\nPages: 1\n", b"password required"
        )
        with patch(
            "digital_librarian.pdf_analysis.shutil.which", return_value="pdfinfo"
        ), patch(
            "digital_librarian.pdf_analysis.run_bounded", return_value=encrypted
        ) as parser:
            encrypted_metadata, encrypted_findings = analyze_pdf(
                pdf, BookAnalysisConfig()
            )
        parser.assert_called_once()
        self.assertTrue(encrypted_metadata["pdf"]["encrypted"])
        self.assertEqual(encrypted_findings[0].code, "pdf-encrypted")

    def test_external_parser_runner_enforces_time_and_output_limits(self) -> None:
        timed = run_bounded(
            [sys.executable, "-c", "import time; time.sleep(2)"],
            0.05,
            65_536,
        )
        self.assertTrue(timed.timed_out)
        limited = run_bounded(
            [sys.executable, "-c", "import os; os.write(1, b'x' * 131072)"],
            2,
            65_536,
        )
        self.assertTrue(limited.output_limited)
        self.assertLessEqual(len(limited.stdout), 65_536)

    def test_reads_bounded_mobi_metadata(self) -> None:
        mobi = self.books / "example.mobi"
        data = bytearray(768)
        record_offset = 86
        mobi_start = record_offset + 16
        mobi_length = 232
        data[76:78] = (1).to_bytes(2, "big")
        data[78:82] = record_offset.to_bytes(4, "big")
        data[mobi_start:mobi_start + 4] = b"MOBI"
        data[mobi_start + 4:mobi_start + 8] = mobi_length.to_bytes(4, "big")
        title = b"Example MOBI"
        title_offset = 500
        data[mobi_start + 84:mobi_start + 88] = (
            title_offset - record_offset
        ).to_bytes(4, "big")
        data[mobi_start + 88:mobi_start + 92] = len(title).to_bytes(4, "big")
        data[mobi_start + 128:mobi_start + 132] = (0x40).to_bytes(4, "big")
        position = mobi_start + mobi_length
        records = [(100, b"Example Author"), (104, ISBN.encode())]
        exth_length = 12 + sum(8 + len(value) for _, value in records)
        data[position:position + 4] = b"EXTH"
        data[position + 4:position + 8] = exth_length.to_bytes(4, "big")
        data[position + 8:position + 12] = len(records).to_bytes(4, "big")
        cursor = position + 12
        for record_type, value in records:
            data[cursor:cursor + 4] = record_type.to_bytes(4, "big")
            data[cursor + 4:cursor + 8] = (8 + len(value)).to_bytes(4, "big")
            data[cursor + 8:cursor + 8 + len(value)] = value
            cursor += 8 + len(value)
        data[title_offset:title_offset + len(title)] = title
        mobi.write_bytes(data)
        metadata, findings = analyze_mobi(mobi)
        self.assertEqual(findings, [])
        self.assertEqual(metadata["bibliographic"]["titles"], ["Example MOBI"])
        self.assertEqual(
            metadata["bibliographic"]["identifiers"][0]["value"], ISBN
        )


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

    def test_intake_hashes_every_file_and_never_proposes_import(self) -> None:
        self.books.chmod(0o700)
        (self.books / "exported.pdf").write_bytes(PDF)
        (self.books / "notes.txt").write_text("private notes", encoding="utf-8")
        config = load_config(
            self.write_config(
                collections=[("phone-intake", "books", self.books, "intake")]
            )
        ).collections[0]
        report = audit_collection(config, BookAnalysisConfig(pdf_text_layer=False))
        self.assertEqual(report.role, "intake")
        self.assertTrue(all(record.sha256 for record in report.files))
        self.assertTrue(
            all(
                record.metadata["intake_status"] == "awaiting-review"
                for record in report.files
            )
        )
        document = report_document([report], "a" * 64)
        self.assertEqual(document["proposed_actions"], [])

    def test_records_external_cover_candidates_without_selecting_one(self) -> None:
        report = CollectionReport("books", "books", "library", "/private")
        report.files = [
            FileRecord("title.pdf", ".pdf", 10, 1, "pdf"),
            FileRecord("title.jpg", ".jpg", 20, 1, "jpeg"),
        ]
        add_external_cover_evidence(report)
        self.assertEqual(
            report.files[0].metadata["cover_evidence"]["external_candidates"],
            ["title.jpg"],
        )
        self.assertEqual(report.findings, [])

    def test_groups_isbn_editions_without_collapsing_formats(self) -> None:
        bibliography = {
            "titles": ["Example Book"],
            "creators": ["Example Author"],
            "languages": ["en"],
            "identifiers": [{"scheme": "isbn-13", "value": ISBN}],
        }
        report = CollectionReport("books", "books", "library", "/private")
        report.files = [
            FileRecord("one.pdf", ".pdf", 10, 1, "pdf", metadata={"bibliographic": bibliography}),
            FileRecord("one.epub", ".epub", 20, 1, "epub", metadata={"bibliographic": bibliography}),
        ]
        add_bibliographic_groups(report)
        group = next(
            finding
            for finding in report.findings
            if finding.code == "probable-edition-group"
        )
        self.assertEqual(group.evidence["confidence"], "high")
        self.assertEqual(group.evidence["formats"], [".epub", ".pdf"])
        self.assertFalse(group.evidence["automatic_collapse"])


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
        self.assertEqual(document["schema_version"], 2)
        self.assertEqual(document["proposed_actions"], [])
        self.assertFalse(document["analysis"]["extracted_document_text_persisted"])
        self.assertFalse(document["capabilities"]["external_metadata_queries"]["enabled"])
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
