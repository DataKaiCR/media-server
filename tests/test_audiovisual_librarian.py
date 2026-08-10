import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from digital_librarian.audiovisual import (
    analyze_subtitle,
    probe_media,
)
from digital_librarian.audiovisual_groups import add_audiovisual_findings
from digital_librarian.bounded import BoundedProcessResult
from digital_librarian.config import (
    AudiovisualAnalysisConfig,
    CollectionConfig,
    ConfigError,
    load_config,
)
from digital_librarian.formats import inspect_file
from digital_librarian.model import CollectionReport, FileRecord
from digital_librarian.report import report_document
from digital_librarian.scanner import audit_collection


MATROSKA = b"\x1aE\xdf\xa3" + b"\x00" * 128
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nPrivate dialogue\n"
def media_metadata(duration: float = 3600, codec: str = "h264") -> dict:
    return {
        "audiovisual": {
            "media": {
                "duration_seconds": duration,
                "streams": [
                    {
                        "type": "video", "codec": codec,
                        "width": 1920, "height": 1080,
                    }
                ],
            }
        }
    }


def subtitle_metadata(last_end: float = 3500) -> dict:
    return {
        "audiovisual": {
            "subtitle": {
                "format": "srt", "cue_count": 10,
                "last_end_seconds": last_end,
                "provenance": {"classification": "human-or-unmarked"},
            }
        }
    }


class AudiovisualTemporaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.media = self.root / "media"
        self.reports = self.root / "private" / "reports"
        self.media.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, *, kind: str = "audiovisual", layout: str | None = "movies", extra: str = "") -> Path:
        layout_line = f'media_layout = "{layout}"\n' if layout is not None else ""
        path = self.root / "librarian.toml"
        path.write_text(
            "version = 1\n"
            f'report_dir = "{self.reports}"\n'
            "[audiovisual_analysis]\n"
            "parser_timeout_seconds = 12\n"
            "max_parser_output_bytes = 131072\n"
            "max_parser_memory_bytes = 536870912\n"
            "packet_order_sampling = true\n"
            "packet_sample_packets = 50000\n"
            "interleave_skew_threshold_seconds = 30\n"
            "subtitle_max_bytes = 1048576\n"
            "large_file_bytes = 10737418240\n"
            "high_bitrate_bits_per_second = 30000000\n"
            "subtitle_runtime_tolerance_seconds = 8\n"
            f"{extra}\n"
            "[[collections]]\n"
            'id = "media"\n'
            f'kind = "{kind}"\n'
            f"{layout_line}"
            f'root = "{self.media}"\n'
            "exclude_globs = []\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        return path


class AudiovisualConfigTest(AudiovisualTemporaryTest):
    def test_loads_bounded_analysis_and_collection_layout(self) -> None:
        config = load_config(self.write_config())
        self.assertEqual(config.collections[0].media_layout, "movies")
        self.assertEqual(config.audiovisual_analysis.parser_timeout_seconds, 12)
        self.assertTrue(config.audiovisual_analysis.packet_order_sampling)
        self.assertEqual(config.audiovisual_analysis.packet_sample_packets, 50_000)
        self.assertEqual(
            config.audiovisual_analysis.interleave_skew_threshold_seconds, 30
        )
        self.assertEqual(config.audiovisual_analysis.subtitle_max_bytes, 1_048_576)
        self.assertEqual(config.audiovisual_analysis.large_file_bytes, 10_737_418_240)
        self.assertEqual(config.audiovisual_analysis.subtitle_runtime_tolerance_seconds, 8)

    def test_packet_config_defaults_and_exact_boundaries(self) -> None:
        default_path = self.write_config()
        default_text = default_path.read_text(encoding="utf-8")
        for line in (
            "packet_order_sampling = true\n",
            "packet_sample_packets = 50000\n",
            "interleave_skew_threshold_seconds = 30\n",
        ):
            default_text = default_text.replace(line, "")
        default_path.write_text(default_text, encoding="utf-8")
        defaults = load_config(default_path).audiovisual_analysis
        self.assertTrue(defaults.packet_order_sampling)
        self.assertEqual(defaults.packet_sample_packets, 50_000)
        self.assertEqual(defaults.interleave_skew_threshold_seconds, 30)

        valid_bounds = (
            ("packet_sample_packets = 50000", "packet_sample_packets = 1000", 1_000),
            ("packet_sample_packets = 50000", "packet_sample_packets = 100000", 100_000),
            (
                "interleave_skew_threshold_seconds = 30",
                "interleave_skew_threshold_seconds = 5",
                5,
            ),
            (
                "interleave_skew_threshold_seconds = 30",
                "interleave_skew_threshold_seconds = 600",
                600,
            ),
        )
        for original, replacement, expected in valid_bounds:
            with self.subTest(replacement=replacement):
                path = self.write_config()
                path.write_text(
                    path.read_text(encoding="utf-8").replace(original, replacement),
                    encoding="utf-8",
                )
                settings = load_config(path).audiovisual_analysis
                field = replacement.split(" =")[0]
                self.assertEqual(getattr(settings, field), expected)

    def test_packet_config_rejects_off_by_one_and_wrong_types(self) -> None:
        invalid_settings = (
            ("packet_order_sampling = true", 'packet_order_sampling = "yes"'),
            ("packet_sample_packets = 50000", "packet_sample_packets = 999"),
            ("packet_sample_packets = 50000", "packet_sample_packets = 100001"),
            ("packet_sample_packets = 50000", "packet_sample_packets = true"),
            (
                "interleave_skew_threshold_seconds = 30",
                "interleave_skew_threshold_seconds = 4",
            ),
            (
                "interleave_skew_threshold_seconds = 30",
                "interleave_skew_threshold_seconds = 601",
            ),
        )
        for original, replacement in invalid_settings:
            with self.subTest(replacement=replacement):
                invalid = self.write_config()
                invalid.write_text(
                    invalid.read_text(encoding="utf-8").replace(
                        original, replacement
                    ),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(ConfigError, original.split(" =")[0]):
                    load_config(invalid)

    def test_rejects_invalid_or_nonmedia_layout_and_unsafe_bounds(self) -> None:
        with self.assertRaisesRegex(ConfigError, "mixed, movies, or series"):
            load_config(self.write_config(layout="albums"))
        with self.assertRaisesRegex(ConfigError, "limited to audiovisual"):
            load_config(self.write_config(kind="photos", layout="movies"))
        unsafe = self.write_config()
        unsafe.write_text(
            unsafe.read_text(encoding="utf-8").replace(
                "subtitle_max_bytes = 1048576", "subtitle_max_bytes = 10"
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "between"):
            load_config(unsafe)


class AudiovisualInspectionTest(AudiovisualTemporaryTest):
    def test_detects_media_and_subtitle_formats_without_external_probe(self) -> None:
        movie = self.media / "movie.mkv"
        movie.write_bytes(MATROSKA)
        detected, _, findings = inspect_file(
            movie, ".mkv", "audiovisual",
            analysis=AudiovisualAnalysisConfig(enabled=False),
        )
        self.assertEqual(detected, "matroska")
        self.assertEqual(findings, [])

        subtitle = self.media / "movie.srt"
        subtitle.write_bytes(SRT)
        detected, _, findings = inspect_file(
            subtitle, ".srt", "audiovisual",
            analysis=AudiovisualAnalysisConfig(enabled=False),
        )
        self.assertEqual(detected, "srt")
        self.assertEqual(findings, [])

    def test_ffprobe_persists_only_bounded_selected_stream_evidence(self) -> None:
        movie = self.media / "movie.mkv"
        movie.write_bytes(MATROSKA)
        payload = {
            "format": {
                "filename": "/private/title.mkv",
                "format_name": "matroska,webm",
                "duration": "7200.5",
                "bit_rate": "50000000",
                "tags": {"title": "PRIVATE TITLE"},
            },
            "streams": [
                {
                    "index": 0, "codec_type": "video", "codec_name": "hevc",
                    "width": 3840, "height": 2160, "pix_fmt": "yuv420p10le",
                    "r_frame_rate": "24000/1001", "tags": {"title": "PRIVATE"},
                },
                {
                    "index": 1, "codec_type": "audio", "codec_name": "aac", "channels": 6,
                    "channel_layout": "5.1", "tags": {"language": "spa"},
                },
                {
                    "index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                    "tags": {"language": "eng", "title": "PRIVATE"},
                },
            ],
            "chapters": [{"start_time": "0", "end_time": "100"}],
        }
        result = BoundedProcessResult(0, json.dumps(payload).encode(), b"")
        settings = AudiovisualAnalysisConfig(
            packet_order_sampling=False,
            large_file_bytes=1024,
            high_bitrate_bits_per_second=40_000_000,
        )
        with patch("digital_librarian.audiovisual.shutil.which", return_value="ffprobe"), patch(
            "digital_librarian.audiovisual._probe_result", return_value=result
        ):
            metadata, findings = probe_media(movie, ".mkv", 2048, settings)
        self.assertEqual(metadata["duration_seconds"], 7200.5)
        self.assertEqual(metadata["streams"][0]["width"], 3840)
        self.assertEqual(metadata["streams"][1]["language"], "spa")
        self.assertEqual(metadata["chapter_count"], 1)
        self.assertIn("oversized-media-review", {item.code for item in findings})
        rendered = json.dumps(metadata)
        self.assertNotIn("PRIVATE", rendered)
        self.assertNotIn("filename", rendered)

    def test_ffprobe_failure_timeout_and_missing_video_are_evidence(self) -> None:
        movie = self.media / "movie.mkv"
        movie.write_bytes(MATROSKA)
        with patch("digital_librarian.audiovisual.shutil.which", return_value="ffprobe"), patch(
            "digital_librarian.audiovisual._probe_result",
            return_value=BoundedProcessResult(None, b"", b"", timed_out=True),
        ):
            _, findings = probe_media(movie, ".mkv", 100, AudiovisualAnalysisConfig())
        self.assertEqual(findings[0].code, "media-probe-timeout")

        audio_only = BoundedProcessResult(
            0,
            json.dumps({
                "format": {"format_name": "matroska", "duration": "10"},
                "streams": [{"codec_type": "audio", "codec_name": "aac", "channels": 2}],
            }).encode(),
            b"",
        )
        with patch("digital_librarian.audiovisual.shutil.which", return_value="ffprobe"), patch(
            "digital_librarian.audiovisual._probe_result", return_value=audio_only
        ):
            _, findings = probe_media(movie, ".mkv", 100, AudiovisualAnalysisConfig())
        self.assertIn("video-stream-missing", {item.code for item in findings})

    def test_every_ffprobe_failure_is_sanitized_evidence(self) -> None:
        movie = self.media / "movie.mkv"
        movie.write_bytes(MATROSKA)
        cases = (
            (
                BoundedProcessResult(0, b"", b"PRIVATE", output_limited=True),
                "media-probe-output-limit",
            ),
            (
                BoundedProcessResult(None, b"", b"PRIVATE", unavailable=True),
                "media-probe-unavailable",
            ),
            (
                BoundedProcessResult(1, b"", b"PRIVATE"),
                "media-container-invalid",
            ),
            (
                BoundedProcessResult(0, b"\xff", b"PRIVATE"),
                "media-probe-invalid-output",
            ),
            (
                BoundedProcessResult(0, b"[]", b"PRIVATE"),
                "media-probe-invalid-output",
            ),
        )
        for result, expected_code in cases:
            with self.subTest(code=expected_code), patch(
                "digital_librarian.audiovisual.shutil.which",
                return_value="ffprobe",
            ), patch(
                "digital_librarian.audiovisual._probe_result",
                return_value=result,
            ):
                metadata, findings = probe_media(
                    movie, ".mkv", 100, AudiovisualAnalysisConfig()
                )
                self.assertEqual(metadata, {})
                self.assertEqual([finding.code for finding in findings], [expected_code])
                self.assertNotIn(
                    "PRIVATE", json.dumps([finding.to_dict() for finding in findings])
                )

    def test_ffprobe_rejects_nonfinite_and_fractional_numeric_fields(self) -> None:
        movie = self.media / "movie.mkv"
        movie.write_bytes(MATROSKA)
        payload = {
            "format": {
                "format_name": "matroska",
                "duration": True,
                "bit_rate": float("inf"),
            },
            "streams": [
                {
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920.5,
                    "height": 1080,
                }
            ],
        }
        with patch(
            "digital_librarian.audiovisual.shutil.which", return_value="ffprobe"
        ), patch(
            "digital_librarian.audiovisual._probe_result",
            return_value=BoundedProcessResult(
                0, json.dumps(payload).encode("utf-8"), b""
            ),
        ):
            metadata, findings = probe_media(
                movie, ".mkv", 100, AudiovisualAnalysisConfig()
            )
        self.assertIsNone(metadata["duration_seconds"])
        self.assertIsNone(metadata["bit_rate_bits_per_second"])
        self.assertIsNone(metadata["streams"][0]["width"])
        codes = {finding.code for finding in findings}
        self.assertIn("media-duration-missing", codes)
        self.assertIn("video-dimensions-invalid", codes)

    def test_subtitle_integrity_and_generated_provenance_persist_no_dialogue(self) -> None:
        subtitle = self.media / "movie.es-419.srt"
        private_dialogue = b"1\n00:00:01,000 --> 00:00:02,000\nVERY PRIVATE DIALOGUE\n"
        subtitle.write_bytes(private_dialogue)
        digest = hashlib.sha256(private_dialogue).hexdigest().encode()
        xattrs = {
            "generated": b"true",
            "source": b"ollama-translation",
            "subtitle_hash": digest,
            "source_hash": b"a" * 64,
            "model": b"local-model",
            "target_language": b"es-419",
        }
        with patch("digital_librarian.audiovisual._xattr_values", return_value=xattrs):
            metadata, findings = analyze_subtitle(
                subtitle, ".srt", subtitle.stat().st_size,
                AudiovisualAnalysisConfig(),
            )
        self.assertEqual(findings, [])
        self.assertEqual(metadata["cue_count"], 1)
        self.assertEqual(metadata["provenance"]["classification"], "ollama-translation")
        self.assertTrue(metadata["provenance"]["subtitle_hash_matches"])
        self.assertNotIn("VERY PRIVATE", json.dumps(metadata))

    def test_vtt_ass_and_vobsub_index_timing_is_bounded(self) -> None:
        samples = {
            ".vtt": b"WEBVTT\n\n00:01.000 --> 00:02.000\nPRIVATE VTT\n",
            ".ass": (
                b"[Script Info]\n[Events]\n"
                b"Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
                b"Dialogue: 0,0:00:01.00,0:00:02.00,Default,,0,0,0,,PRIVATE ASS\n"
            ),
            ".idx": (
                b"# VobSub index file, v7\n"
                b"timestamp: 00:00:01:000, filepos: 000000000\n"
            ),
        }
        for extension, raw in samples.items():
            with self.subTest(extension=extension):
                path = self.media / f"subtitle{extension}"
                path.write_bytes(raw)
                with patch("digital_librarian.audiovisual._xattr_values", return_value={}):
                    metadata, findings = analyze_subtitle(
                        path, extension, len(raw), AudiovisualAnalysisConfig()
                    )
                self.assertEqual(metadata["cue_count"], 1)
                self.assertNotIn("PRIVATE", json.dumps(metadata))
                self.assertNotIn("subtitle-cues-missing", {item.code for item in findings})

    def test_subtitle_malformed_timing_and_hash_mismatch_are_findings(self) -> None:
        subtitle = self.media / "broken.srt"
        raw = b"1\n00:00:03,000 --> 00:00:02,000\ntext\n"
        subtitle.write_bytes(raw)
        xattrs = {
            "generated": b"true", "source": b"whisperai",
            "subtitle_hash": b"0" * 64,
        }
        with patch("digital_librarian.audiovisual._xattr_values", return_value=xattrs):
            _, findings = analyze_subtitle(
                subtitle, ".srt", len(raw), AudiovisualAnalysisConfig()
            )
        codes = {item.code for item in findings}
        self.assertIn("subtitle-timing-invalid", codes)
        self.assertIn("subtitle-provenance-mismatch", codes)

        empty = self.media / "empty-cue.srt"
        empty.write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\n\n")
        with patch("digital_librarian.audiovisual._xattr_values", return_value={}):
            metadata, empty_findings = analyze_subtitle(
                empty, ".srt", empty.stat().st_size, AudiovisualAnalysisConfig()
            )
        self.assertEqual(metadata["empty_text_cue_count"], 1)
        self.assertIn("subtitle-timing-invalid", {item.code for item in empty_findings})


class AudiovisualGroupingTest(unittest.TestCase):
    def test_movie_layout_groups_encodes_and_matches_subtitle_language(self) -> None:
        config = CollectionConfig(
            "movies", "audiovisual", "library", Path("/private"), (), "movies"
        )
        report = CollectionReport("movies", "audiovisual", "library", "/private")
        report.files = [
            FileRecord("Film/Film.mkv", ".mkv", 100, 1, "matroska", metadata=media_metadata()),
            FileRecord("Film/Film alternate.mp4", ".mp4", 80, 1, "mp4", metadata=media_metadata(codec="hevc")),
            FileRecord("Film/Film.ea.srt", ".srt", 10, 1, "srt", metadata=subtitle_metadata(100)),
            FileRecord("Film/poster.jpg", ".jpg", 10, 1, "jpeg"),
            FileRecord("Orphan/poster.jpg", ".jpg", 10, 1, "jpeg"),
            FileRecord("Orphan/movie.nfo", ".nfo", 10, 1, "text"),
        ]
        add_audiovisual_findings(report, config, AudiovisualAnalysisConfig())
        codes = {item.code for item in report.findings}
        self.assertIn("possible-redundant-encode-group", codes)
        self.assertIn("orphan-media-artwork", codes)
        self.assertIn("orphan-media-metadata", codes)
        self.assertIn("subtitle-possibly-truncated", codes)
        subtitle = next(row for row in report.files if row.extension == ".srt")
        evidence = subtitle.metadata["audiovisual"]["subtitle"]
        self.assertEqual(evidence["filename_language"], "es-419")
        self.assertEqual(evidence["matched_media_relative_path"], "Film/Film.mkv")
        group = next(item for item in report.findings if item.code == "possible-redundant-encode-group")
        self.assertFalse(group.evidence["automatic_delete"])

    def test_conventional_movie_subtitle_directory_uses_single_video_scope(self) -> None:
        config = CollectionConfig(
            "movies", "audiovisual", "library", Path("/private"), (), "movies"
        )
        report = CollectionReport("movies", "audiovisual", "library", "/private")
        report.files = [
            FileRecord("Film/Film.mkv", ".mkv", 100, 1, "matroska", metadata=media_metadata()),
            FileRecord("Film/Subtitles/English.srt", ".srt", 10, 1, "srt", metadata=subtitle_metadata()),
        ]
        add_audiovisual_findings(report, config, AudiovisualAnalysisConfig())
        codes = {item.code for item in report.findings}
        self.assertNotIn("unmatched-external-subtitle", codes)
        self.assertNotIn("audiovisual-scope-without-video", codes)
        subtitle = report.files[1].metadata["audiovisual"]["subtitle"]
        self.assertEqual(subtitle["filename_language"], "en")
        self.assertEqual(subtitle["matched_media_relative_path"], "Film/Film.mkv")

    def test_explicit_alternate_cuts_are_not_called_redundant(self) -> None:
        config = CollectionConfig(
            "movies", "audiovisual", "library", Path("/private"), (), "movies"
        )
        report = CollectionReport("movies", "audiovisual", "library", "/private")
        report.files = [
            FileRecord("Film/Film Theatrical.mkv", ".mkv", 100, 1, "matroska", metadata=media_metadata()),
            FileRecord("Film/Film Director's Cut.mkv", ".mkv", 120, 1, "matroska", metadata=media_metadata()),
        ]
        add_audiovisual_findings(report, config, AudiovisualAnalysisConfig())
        codes = {item.code for item in report.findings}
        self.assertIn("distinct-edition-group", codes)
        self.assertNotIn("possible-redundant-encode-group", codes)
        group = next(item for item in report.findings if item.code == "distinct-edition-group")
        self.assertFalse(group.evidence["automatic_collapse"])

    def test_series_layout_detects_duplicate_episode_and_unmatched_entries(self) -> None:
        config = CollectionConfig(
            "series", "audiovisual", "library", Path("/private"), (), "series"
        )
        report = CollectionReport("series", "audiovisual", "library", "/private")
        report.files = [
            FileRecord("Show/Season 01/Show S01E01.mkv", ".mkv", 100, 1, "matroska", metadata=media_metadata()),
            FileRecord("Show/Season 01/Show S01E01 alt.mp4", ".mp4", 90, 1, "mp4", metadata=media_metadata()),
            FileRecord("Show/Season 01/Special.mkv", ".mkv", 50, 1, "matroska", metadata=media_metadata()),
            FileRecord("Show/Season 01/Unknown.en.srt", ".srt", 10, 1, "srt", metadata=subtitle_metadata()),
        ]
        add_audiovisual_findings(report, config, AudiovisualAnalysisConfig())
        codes = {item.code for item in report.findings}
        self.assertIn("possible-redundant-encode-group", codes)
        self.assertIn("episode-pattern-missing", codes)
        self.assertIn("unmatched-external-subtitle", codes)


class AudiovisualEndToEndTest(AudiovisualTemporaryTest):
    def test_audit_is_read_only_and_report_declares_no_text_or_actions(self) -> None:
        folder = self.media / "Film"
        folder.mkdir()
        movie = folder / "Film.mkv"
        subtitle = folder / "Film.en.srt"
        movie.write_bytes(MATROSKA)
        subtitle.write_bytes(SRT)
        before = {
            path: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in (movie, subtitle)
        }
        config = CollectionConfig(
            "movies", "audiovisual", "library", self.media, (), "movies"
        )
        settings = AudiovisualAnalysisConfig(enabled=False)
        report = audit_collection(config, audiovisual_analysis=settings)
        document = report_document(
            [report], "a" * 64, audiovisual_analysis=settings
        )
        self.assertEqual(document["schema_version"], 5)
        self.assertEqual(document["proposed_actions"], [])
        self.assertFalse(document["analysis"]["subtitle_text_persisted"])
        self.assertFalse(document["analysis"]["raw_ffprobe_output_persisted"])
        self.assertFalse(document["analysis"]["raw_packet_output_persisted"])
        for path, state in before.items():
            self.assertEqual(
                (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns),
                state,
            )


if __name__ == "__main__":
    unittest.main()
