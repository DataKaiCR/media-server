"""Iron-Grade tests for generated-subtitle provenance marking."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "mark-generated-subtitle.py"
CONTENT = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"


class GeneratedSubtitleMarkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.subtitle = self.root / "movie.en.srt"
        self.manifest = self.root / "private" / "generated.jsonl"
        self.subtitle.write_bytes(CONTENT)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_marker(
        self,
        provider: str,
        *,
        subtitle: Path | None = None,
        manifest: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--provider",
                provider,
                "--subtitle",
                str(subtitle or self.subtitle),
                "--score",
                "33.5",
                "--manifest",
                str(manifest or self.manifest),
            ],
            check=check,
            capture_output=True,
            text=True,
        )

    def assert_xattr_missing(self, path: Path, name: str) -> None:
        with self.assertRaises(OSError):
            os.getxattr(path, name)

    def test_marks_whisper_with_private_manifest_and_unchanged_content(self) -> None:
        self.run_marker("whisperai")

        digest = hashlib.sha256(CONTENT).hexdigest()
        self.assertEqual(
            os.getxattr(self.subtitle, "user.media_server.generated"), b"true"
        )
        self.assertEqual(
            os.getxattr(self.subtitle, "user.media_server.subtitle_source"),
            b"whisperai",
        )
        self.assertEqual(
            os.getxattr(self.subtitle, "user.media_server.subtitle_sha256"),
            digest.encode("ascii"),
        )
        records = [
            json.loads(line) for line in self.manifest.read_text().splitlines()
        ]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider"], "whisperai")
        self.assertEqual(records[0]["sha256"], digest)
        self.assertEqual(self.manifest.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.manifest.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.subtitle.read_bytes(), CONTENT)

    def test_human_replacement_clears_every_generated_marker(self) -> None:
        self.run_marker("whisperai")
        os.setxattr(
            self.subtitle, "user.media_server.translation_model", b"model"
        )
        os.setxattr(
            self.subtitle, "user.media_server.target_language", b"es-419"
        )

        self.run_marker("opensubtitlescom")

        for name in (
            "user.media_server.generated",
            "user.media_server.subtitle_source",
            "user.media_server.subtitle_sha256",
            "user.media_server.translation_model",
            "user.media_server.target_language",
        ):
            self.assert_xattr_missing(self.subtitle, name)
        self.assertEqual(self.subtitle.read_bytes(), CONTENT)
        self.assertEqual(len(self.manifest.read_text().splitlines()), 1)

    def test_manifest_failure_restores_the_exact_prior_xattr_state(self) -> None:
        old_hash = b"a" * 64
        os.setxattr(
            self.subtitle, "user.media_server.source_sha256", old_hash
        )
        invalid_manifest = self.root / "manifest-is-a-directory"
        invalid_manifest.mkdir()

        result = self.run_marker(
            "whisperai", manifest=invalid_manifest, check=False
        )

        self.assertNotEqual(result.returncode, 0)
        self.assert_xattr_missing(
            self.subtitle, "user.media_server.generated"
        )
        self.assert_xattr_missing(
            self.subtitle, "user.media_server.subtitle_sha256"
        )
        self.assertEqual(
            os.getxattr(self.subtitle, "user.media_server.source_sha256"),
            old_hash,
        )
        self.assertEqual(self.subtitle.read_bytes(), CONTENT)

    def test_subtitle_symlink_is_refused_without_marking_its_target(self) -> None:
        target = self.root / "target.srt"
        target.write_bytes(CONTENT)
        link = self.root / "linked.srt"
        link.symlink_to(target)

        result = self.run_marker(
            "whisperai", subtitle=link, check=False
        )

        self.assertNotEqual(result.returncode, 0)
        self.assert_xattr_missing(target, "user.media_server.generated")
        self.assertFalse(self.manifest.exists())
        self.assertEqual(target.read_bytes(), CONTENT)

    def test_manifest_symlink_is_refused_and_target_is_unchanged(self) -> None:
        target = self.root / "other-manifest"
        target.write_text("do not append\n", encoding="utf-8")
        self.manifest.parent.mkdir(mode=0o700)
        self.manifest.symlink_to(target)

        result = self.run_marker("whisperai", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(encoding="utf-8"), "do not append\n")
        self.assert_xattr_missing(
            self.subtitle, "user.media_server.generated"
        )


if __name__ == "__main__":
    unittest.main()
