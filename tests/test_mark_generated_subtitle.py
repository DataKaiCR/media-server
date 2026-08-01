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


class GeneratedSubtitleMarkerTest(unittest.TestCase):
    def run_marker(self, provider: str, subtitle: Path, manifest: Path) -> None:
        subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--provider",
                provider,
                "--subtitle",
                str(subtitle),
                "--score",
                "33.5",
                "--manifest",
                str(manifest),
            ],
            check=True,
            capture_output=True,
            text=True,
        )

    def test_marks_whisper_and_clears_marker_for_human_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "movie.en.srt"
            manifest = root / "generated.jsonl"
            content = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n"
            subtitle.write_bytes(content)

            self.run_marker("whisperai", subtitle, manifest)

            digest = hashlib.sha256(content).hexdigest()
            self.assertEqual(os.getxattr(subtitle, "user.media_server.generated"), b"true")
            self.assertEqual(
                os.getxattr(subtitle, "user.media_server.subtitle_source"),
                b"whisperai",
            )
            self.assertEqual(
                os.getxattr(subtitle, "user.media_server.subtitle_sha256"),
                digest.encode("ascii"),
            )
            records = [json.loads(line) for line in manifest.read_text().splitlines()]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["provider"], "whisperai")
            self.assertEqual(records[0]["sha256"], digest)
            self.assertEqual(subtitle.read_bytes(), content)

            os.setxattr(subtitle, "user.media_server.translation_model", b"model")
            os.setxattr(subtitle, "user.media_server.target_language", b"es-419")
            self.run_marker("opensubtitlescom", subtitle, manifest)
            for name in [
                "user.media_server.generated",
                "user.media_server.translation_model",
                "user.media_server.target_language",
            ]:
                with self.assertRaises(OSError):
                    os.getxattr(subtitle, name)
            self.assertEqual(subtitle.read_bytes(), content)
            self.assertEqual(len(manifest.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
