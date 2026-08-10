"""Iron-Grade falsification tests for bounded packet-order evidence.

Each case targets a regression that could hide malformed interleaving: ignoring
physical byte order, partially sorting incomplete positions, crossing the wrong
threshold boundary, trusting regressing timestamps, accepting malformed parser
rows, selecting cover art as video, leaking raw rows, or losing partial evidence
when the bounded parser fails. The optional real-tool test exercises ffmpeg and
ffprobe without mocks when both are installed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from digital_librarian.audiovisual import probe_media
from digital_librarian.bounded import BoundedProcessResult
from digital_librarian.config import AudiovisualAnalysisConfig
from digital_librarian.model import Finding
from digital_librarian.packet_order import sample_packet_order


MISSING = object()
PACKET_STREAMS: list[object] = [
    {
        "index": 0,
        "codec_type": "video",
        "disposition": {"default": 1, "attached_pic": 0},
    },
    {"index": 1, "codec_type": "audio", "disposition": {"default": 1}},
    {"index": 2, "codec_type": "audio", "disposition": {"default": 0}},
]


def packet_line(
    stream_index: object,
    pts: object,
    *,
    dts: object = MISSING,
    position: object = MISSING,
    extra: str = "",
) -> bytes:
    dts_value = pts if dts is MISSING else dts
    fields = [
        f"stream_index={stream_index}",
        f"pts_time={pts}",
        f"dts_time={dts_value}",
    ]
    if position is not MISSING:
        fields.append(f"pos={position}")
    if extra:
        fields.append(extra)
    return "|".join(fields).encode("ascii")


def packet_output(*lines: bytes) -> bytes:
    return b"\n".join(lines) + (b"\n" if lines else b"")


def finding_codes(findings: list[Finding]) -> set[str]:
    return {finding.code for finding in findings}


class PacketOrderTemporaryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"\x1aE\xdf\xa3" + b"\0" * 128)
        self.settings = AudiovisualAnalysisConfig(
            packet_sample_packets=50_000,
            interleave_skew_threshold_seconds=30,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def sample(
        self,
        output: bytes,
        *,
        streams: list[object] | None = None,
        result: BoundedProcessResult | None = None,
        settings: AudiovisualAnalysisConfig | None = None,
    ) -> tuple[dict[str, Any], list[Finding]]:
        bounded_result = result or BoundedProcessResult(0, output, b"")
        with patch(
            "digital_librarian.packet_order._packet_probe_result",
            return_value=bounded_result,
        ):
            return sample_packet_order(
                self.movie,
                PACKET_STREAMS if streams is None else streams,
                self.settings if settings is None else settings,
            )


class PacketOrderSkewFalsificationTest(PacketOrderTemporaryTest):
    def test_regression_detects_delayed_audio_from_physical_positions(self) -> None:
        output = packet_output(
            packet_line(1, 0, position=500),
            packet_line(0, 180, position=400),
            packet_line(0, 0, position=100),
            packet_line(2, 180, position=510),
            packet_line(0, 60, position=200),
            packet_line(0, 120, position=300),
        )

        evidence, findings = self.sample(output)

        streams = {row["stream_index"]: row for row in evidence["audio_streams"]}
        self.assertTrue(evidence["physical_order_from_positions"])
        self.assertEqual(streams[1]["maximum_lag_seconds"], 180.0)
        self.assertEqual(streams[2]["maximum_lag_seconds"], 0.0)
        self.assertEqual(evidence["threshold_crossed_stream_count"], 1)
        skew = next(
            finding
            for finding in findings
            if finding.code == "media-packet-interleave-skew"
        )
        self.assertEqual(skew.evidence["affected_audio_stream_count"], 1)
        self.assertFalse(skew.evidence["automatic_action"])

    def test_mixed_positions_fall_back_to_whole_demux_order(self) -> None:
        output = packet_output(
            packet_line(0, 0, position=100),
            packet_line(1, 0, position=500),
            packet_line(0, 120, position=200),
            packet_line(1, 120),
        )

        evidence, findings = self.sample(output)

        self.assertFalse(evidence["physical_order_from_positions"])
        self.assertEqual(evidence["positioned_packet_count"], 3)
        self.assertEqual(evidence["maximum_audio_lag_seconds"], 0.0)
        self.assertEqual(evidence["maximum_audio_lead_seconds"], 0.0)
        self.assertNotIn("media-packet-interleave-skew", finding_codes(findings))

    def test_exact_threshold_crosses_but_value_below_it_does_not(self) -> None:
        output = packet_output(
            packet_line(0, 0, position=100),
            packet_line(1, 29.999, position=200),
            packet_line(2, 30, position=210),
        )

        evidence, findings = self.sample(output)

        self.assertEqual(evidence["maximum_audio_lead_seconds"], 30.0)
        self.assertEqual(evidence["threshold_crossed_stream_count"], 1)
        self.assertIn("media-packet-interleave-skew", finding_codes(findings))

    def test_video_timestamp_regression_cannot_lower_the_frontier(self) -> None:
        output = packet_output(
            packet_line(0, 0, position=100),
            packet_line(0, 100, position=200),
            packet_line(0, 10, position=300),
            packet_line(1, 70, position=400),
        )

        evidence, _ = self.sample(output)

        self.assertEqual(evidence["maximum_audio_lag_seconds"], 30.0)
        self.assertEqual(evidence["maximum_audio_lead_seconds"], 0.0)
        self.assertEqual(evidence["threshold_crossed_stream_count"], 1)

    def test_dts_precedes_pts_and_missing_dts_falls_back_to_pts(self) -> None:
        output = packet_output(
            packet_line(0, 100, dts=0, position=100),
            packet_line(1, 200, dts=1, position=200),
            packet_line(2, 40, dts="N/A", position=300),
        )

        evidence, _ = self.sample(output)

        streams = {row["stream_index"]: row for row in evidence["audio_streams"]}
        self.assertEqual(streams[1]["maximum_lead_seconds"], 1.0)
        self.assertEqual(streams[2]["maximum_lead_seconds"], 40.0)
        self.assertEqual(evidence["threshold_crossed_stream_count"], 1)

    def test_cover_art_fractional_indexes_and_duplicates_do_not_add_streams(self) -> None:
        streams: list[object] = [
            {
                "index": 0,
                "codec_type": "video",
                "disposition": {"default": 1, "attached_pic": 1},
            },
            {"index": 1, "codec_type": "video", "disposition": {}},
            {"index": 2, "codec_type": "audio", "disposition": {}},
            {"index": 2, "codec_type": "audio", "disposition": {}},
            {"index": 3.5, "codec_type": "audio", "disposition": {}},
        ]
        output = packet_output(
            packet_line(1, 0, position=100),
            packet_line(2, 0, position=200),
            packet_line(3, 90, position=300),
        )

        evidence, findings = self.sample(output, streams=streams)

        self.assertEqual(evidence["primary_video_stream_index"], 1)
        self.assertEqual(evidence["audio_stream_count"], 1)
        self.assertEqual(evidence["observed_audio_stream_count"], 1)
        self.assertEqual(evidence["sampled_packet_count"], 2)
        self.assertNotIn("media-packet-interleave-skew", finding_codes(findings))


class PacketOrderInputFalsificationTest(PacketOrderTemporaryTest):
    def test_malformed_selected_rows_are_bounded_private_partial_evidence(self) -> None:
        secret = "PRIVATE_PACKET_VALUE_MUST_NOT_PERSIST"
        output = packet_output(
            packet_line(0, 0, position=100, extra=f"private={secret}"),
            b"\xff",
            b"stream_index=1|pts_time=0|dts_time=0|pos=200|" + b"X" * 600,
            packet_line(1, "N/A", dts="N/A", position=300),
            packet_line(1, "nan", dts="nan", position=310),
            packet_line(1, "inf", dts="inf", position=320),
            packet_line(1, 1_000_000_001, position=330),
            packet_line(1.5, 0, position=400),
            packet_line(9, "N/A", dts="N/A", position=500),
            packet_line(1, 0, position=600),
        )

        evidence, findings = self.sample(output)

        self.assertFalse(evidence["analysis_complete"])
        self.assertEqual(evidence["sampled_packet_count"], 2)
        self.assertEqual(evidence["discarded_packet_row_count"], 7)
        incomplete = next(
            finding
            for finding in findings
            if finding.code == "media-packet-order-sample-incomplete"
        )
        self.assertEqual(incomplete.evidence["reason"], "malformed-output")
        rendered = json.dumps(
            {"evidence": evidence, "findings": [row.to_dict() for row in findings]}
        )
        self.assertNotIn(secret, rendered)
        self.assertNotIn("pts_time", rendered)
        self.assertNotIn("dts_time", rendered)
        self.assertNotIn("stream_index=", rendered)

    def test_empty_successful_sample_is_evidence_not_a_parser_failure(self) -> None:
        evidence, findings = self.sample(b"")

        self.assertTrue(evidence["analysis_complete"])
        self.assertEqual(evidence["sampled_packet_count"], 0)
        self.assertEqual(evidence["discarded_packet_row_count"], 0)
        self.assertEqual(finding_codes(findings), {"media-packet-order-sample-empty"})

    def test_disabled_or_inapplicable_samples_never_invoke_ffprobe(self) -> None:
        cases = (
            (
                AudiovisualAnalysisConfig(packet_order_sampling=False),
                PACKET_STREAMS,
            ),
            (
                self.settings,
                [{"index": 1, "codec_type": "audio"}],
            ),
            (
                self.settings,
                [{"index": 0, "codec_type": "video"}],
            ),
        )
        for settings, streams in cases:
            with self.subTest(settings=settings, streams=streams), patch(
                "digital_librarian.packet_order._packet_probe_result"
            ) as probe:
                evidence, findings = sample_packet_order(
                    self.movie, streams, settings
                )
                self.assertEqual(evidence, {})
                self.assertEqual(findings, [])
                probe.assert_not_called()


class PacketOrderRecoveryFalsificationTest(PacketOrderTemporaryTest):
    def test_every_bounded_process_failure_retains_partial_skew_and_reason(self) -> None:
        partial = packet_output(
            packet_line(0, 0, position=100),
            packet_line(0, 90, position=200),
            packet_line(1, 0, position=300),
        )
        cases = (
            (
                "timeout",
                BoundedProcessResult(
                    None, partial, b"PRIVATE STDERR", timed_out=True
                ),
            ),
            (
                "output-limit",
                BoundedProcessResult(
                    -25, partial, b"PRIVATE STDERR", output_limited=True
                ),
            ),
            (
                "unavailable",
                BoundedProcessResult(
                    None, partial, b"PRIVATE STDERR", unavailable=True
                ),
            ),
            (
                "parser-error",
                BoundedProcessResult(1, partial, b"PRIVATE STDERR"),
            ),
        )
        for expected_reason, result in cases:
            with self.subTest(reason=expected_reason):
                evidence, findings = self.sample(partial, result=result)
                self.assertFalse(evidence["analysis_complete"])
                self.assertEqual(evidence["maximum_audio_lag_seconds"], 90.0)
                incomplete = next(
                    finding
                    for finding in findings
                    if finding.code == "media-packet-order-sample-incomplete"
                )
                self.assertEqual(incomplete.evidence["reason"], expected_reason)
                self.assertIn(
                    "media-packet-interleave-skew", finding_codes(findings)
                )
                self.assertNotIn(
                    "PRIVATE STDERR",
                    json.dumps([finding.to_dict() for finding in findings]),
                )

    def test_command_uses_argv_and_all_configured_resource_bounds(self) -> None:
        hostile_path = self.root / "movie; touch SHOULD_NOT_EXIST.mkv"
        hostile_path.write_bytes(self.movie.read_bytes())
        settings = AudiovisualAnalysisConfig(
            parser_timeout_seconds=7,
            max_parser_output_bytes=131_072,
            max_parser_memory_bytes=536_870_912,
            packet_sample_packets=1_000,
        )
        with patch(
            "digital_librarian.packet_order.run_bounded",
            return_value=BoundedProcessResult(0, b"", b""),
        ) as bounded:
            sample_packet_order(hostile_path, PACKET_STREAMS, settings)

        command, timeout, output_limit, memory_limit = bounded.call_args.args
        interval_index = command.index("-read_intervals") + 1
        self.assertIsInstance(command, list)
        self.assertEqual(command[interval_index], "%+#1000")
        self.assertEqual(command[-1], str(hostile_path))
        self.assertEqual(command.count(str(hostile_path)), 1)
        self.assertEqual(timeout, 7)
        self.assertEqual(output_limit, 131_072)
        self.assertEqual(memory_limit, 536_870_912)
        self.assertFalse((self.root / "SHOULD_NOT_EXIST.mkv").exists())


class PacketOrderIntegrationTest(PacketOrderTemporaryTest):
    def test_probe_media_surfaces_packet_evidence_and_warning(self) -> None:
        payload = {
            "format": {"format_name": "matroska", "duration": "120"},
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "h264",
                    "width": 1920,
                    "height": 1080,
                    "disposition": {"default": 1, "attached_pic": 0},
                },
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "aac",
                    "channels": 2,
                    "disposition": {"default": 1},
                },
            ],
        }
        packets = packet_output(
            packet_line(0, 0, position=100),
            packet_line(0, 90, position=200),
            packet_line(1, 0, position=300),
        )
        with patch(
            "digital_librarian.audiovisual.shutil.which", return_value="ffprobe"
        ), patch(
            "digital_librarian.audiovisual._probe_result",
            return_value=BoundedProcessResult(
                0, json.dumps(payload).encode("utf-8"), b""
            ),
        ), patch(
            "digital_librarian.packet_order._packet_probe_result",
            return_value=BoundedProcessResult(0, packets, b""),
        ):
            metadata, findings = probe_media(
                self.movie, ".mkv", self.movie.stat().st_size, self.settings
            )

        sample = metadata["packet_order_sample"]
        self.assertEqual(sample["maximum_audio_lag_seconds"], 90.0)
        self.assertIn("media-packet-interleave-skew", finding_codes(findings))

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "ffmpeg and ffprobe are required for real-tool integration",
    )
    def test_real_tools_sample_temporary_media_without_mutating_it(self) -> None:
        generated = self.root / "generated.mkv"
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=16x16:r=2:d=1",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-t",
                "1",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "ffv1",
                "-c:a",
                "pcm_s16le",
                "-y",
                str(generated),
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=15,
        )
        before = (
            hashlib.sha256(generated.read_bytes()).hexdigest(),
            generated.stat().st_size,
            generated.stat().st_mtime_ns,
        )
        streams: list[object] = [
            {
                "index": 0,
                "codec_type": "video",
                "disposition": {"default": 1, "attached_pic": 0},
            },
            {"index": 1, "codec_type": "audio", "disposition": {"default": 1}},
        ]

        evidence, findings = sample_packet_order(
            generated,
            streams,
            AudiovisualAnalysisConfig(
                parser_timeout_seconds=15,
                packet_sample_packets=1_000,
                interleave_skew_threshold_seconds=30,
            ),
        )

        after = (
            hashlib.sha256(generated.read_bytes()).hexdigest(),
            generated.stat().st_size,
            generated.stat().st_mtime_ns,
        )
        self.assertTrue(evidence["analysis_complete"])
        self.assertGreater(evidence["sampled_packet_count"], 0)
        self.assertEqual(evidence["observed_audio_stream_count"], 1)
        self.assertEqual(evidence["threshold_crossed_stream_count"], 0)
        self.assertEqual(findings, [])
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
