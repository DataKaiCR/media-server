"""Bounded physical packet-order evidence for audiovisual containers."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from .bounded import BoundedProcessResult, run_bounded
from .config import AudiovisualAnalysisConfig
from .model import Finding


PacketRow = tuple[int, float, int | None, int]


@dataclass
class AudioPacketStats:
    sampled_packets: int = 0
    maximum_lag_seconds: float = 0.0
    maximum_lead_seconds: float = 0.0


def _bounded_integer(value: object, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        return None
    return result if 0 <= result <= maximum else None


def _finite_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or abs(result) > 1_000_000_000:
        return None
    return result


def _packet_probe_result(
    path: Path, settings: AudiovisualAnalysisConfig
) -> BoundedProcessResult:
    return run_bounded(
        [
            "ffprobe", "-v", "error", "-read_intervals",
            f"%+#{settings.packet_sample_packets}", "-show_packets",
            "-show_entries", "packet=stream_index,pts_time,dts_time,pos",
            "-of", "compact=p=0:nk=0", str(path),
        ],
        settings.parser_timeout_seconds,
        settings.max_parser_output_bytes,
        settings.max_parser_memory_bytes,
    )


def _packet_row(
    raw_line: bytes, sequence: int
) -> tuple[PacketRow | None, int | None]:
    if not raw_line or len(raw_line) > 512:
        return None, None
    try:
        fields = dict(
            field.partition("=")[::2]
            for field in raw_line.decode("ascii", errors="strict").split("|")
            if "=" in field
        )
    except (UnicodeDecodeError, ValueError):
        return None, None
    stream_index = _bounded_integer(fields.get("stream_index"), 4096)
    if stream_index is None:
        return None, None
    timestamp = _finite_float(fields.get("dts_time"))
    if timestamp is None:
        timestamp = _finite_float(fields.get("pts_time"))
    if timestamp is None:
        return None, stream_index
    position = _bounded_integer(fields.get("pos"), 9_223_372_036_854_775_807)
    return (stream_index, timestamp, position, sequence), stream_index


def _stream_indexes(raw_streams: list[object], stream_type: str) -> list[int]:
    indexes: list[int] = []
    seen: set[int] = set()
    for stream in raw_streams[:128]:
        if not isinstance(stream, dict) or stream.get("codec_type") != stream_type:
            continue
        disposition = stream.get("disposition")
        if (
            stream_type == "video"
            and isinstance(disposition, dict)
            and disposition.get("attached_pic")
        ):
            continue
        index = _bounded_integer(stream.get("index"), 4096)
        if index is not None and index not in seen:
            indexes.append(index)
            seen.add(index)
    return indexes


def _primary_video_index(raw_streams: list[object]) -> int | None:
    video_indexes = _stream_indexes(raw_streams, "video")
    for stream in raw_streams[:128]:
        if not isinstance(stream, dict):
            continue
        index = _bounded_integer(stream.get("index"), 4096)
        if index not in video_indexes:
            continue
        disposition = stream.get("disposition")
        if isinstance(disposition, dict) and disposition.get("default"):
            return index
    return video_indexes[0] if video_indexes else None


def _selected_packets(
    output: bytes, selected_streams: set[int]
) -> tuple[list[PacketRow], int, bool, int]:
    packets: list[PacketRow] = []
    discarded = 0
    for sequence, line in enumerate(output.splitlines()):
        row, stream_index = _packet_row(line, sequence)
        if row is None:
            if stream_index is None or stream_index in selected_streams:
                discarded += 1
        elif stream_index in selected_streams:
            packets.append(row)
    positioned = sum(row[2] is not None for row in packets)
    positions_complete = bool(packets) and positioned == len(packets)
    if positions_complete:
        packets.sort(key=lambda row: (int(row[2]), row[3]))
    return packets, positioned, positions_complete, discarded


def _measure_audio_skew(
    packets: list[PacketRow], video_index: int, audio_indexes: list[int]
) -> list[dict[str, int | float]]:
    stats = {index: AudioPacketStats() for index in audio_indexes}
    video_frontier: float | None = None
    for stream_index, timestamp, _position, _sequence in packets:
        if stream_index == video_index:
            video_frontier = (
                timestamp if video_frontier is None
                else max(video_frontier, timestamp)
            )
            continue
        stream_stats = stats.get(stream_index)
        if stream_stats is None:
            continue
        stream_stats.sampled_packets += 1
        if video_frontier is not None:
            stream_stats.maximum_lag_seconds = max(
                stream_stats.maximum_lag_seconds, video_frontier - timestamp, 0.0
            )
            stream_stats.maximum_lead_seconds = max(
                stream_stats.maximum_lead_seconds, timestamp - video_frontier, 0.0
            )
    return [
        {
            "stream_index": index,
            "sampled_packets": row.sampled_packets,
            "maximum_lag_seconds": row.maximum_lag_seconds,
            "maximum_lead_seconds": row.maximum_lead_seconds,
        }
        for index, row in sorted(stats.items())
        if row.sampled_packets
    ]


def _render_audio_stats(
    rows: list[dict[str, int | float]],
) -> list[dict[str, int | float]]:
    return [
        {
            **row,
            "maximum_lag_seconds": round(float(row["maximum_lag_seconds"]), 3),
            "maximum_lead_seconds": round(float(row["maximum_lead_seconds"]), 3),
        }
        for row in rows
    ]


def _packet_order_evidence(
    output: bytes,
    raw_streams: list[object],
    settings: AudiovisualAnalysisConfig,
    analysis_complete: bool,
) -> dict[str, Any]:
    video_index = _primary_video_index(raw_streams)
    audio_indexes = _stream_indexes(raw_streams, "audio")
    selected = set(audio_indexes)
    if video_index is not None:
        selected.add(video_index)
    packets, positioned, positions_complete, discarded = _selected_packets(
        output, selected
    )
    per_stream = _measure_audio_skew(
        packets, video_index, audio_indexes
    ) if video_index is not None else []
    maximum_lag = max(
        (float(row["maximum_lag_seconds"]) for row in per_stream), default=0.0
    )
    maximum_lead = max(
        (float(row["maximum_lead_seconds"]) for row in per_stream), default=0.0
    )
    threshold = settings.interleave_skew_threshold_seconds
    affected = sum(
        max(float(row["maximum_lag_seconds"]), float(row["maximum_lead_seconds"]))
        >= threshold
        for row in per_stream
    )
    rendered_streams = _render_audio_stats(per_stream)
    return {
        "analysis_complete": analysis_complete,
        "packet_limit": settings.packet_sample_packets,
        "sampled_packet_count": len(packets),
        "positioned_packet_count": positioned,
        "discarded_packet_row_count": discarded,
        "physical_order_from_positions": positions_complete,
        "primary_video_stream_index": video_index,
        "audio_stream_count": len(audio_indexes),
        "observed_audio_stream_count": len(per_stream),
        "threshold_seconds": threshold,
        "maximum_audio_lag_seconds": round(maximum_lag, 3),
        "maximum_audio_lead_seconds": round(maximum_lead, 3),
        "threshold_crossed_stream_count": affected,
        "audio_streams": rendered_streams,
        "automatic_action": False,
    }


def _incomplete_reason(result: BoundedProcessResult) -> str | None:
    if result.timed_out:
        return "timeout"
    if result.output_limited:
        return "output-limit"
    if result.unavailable:
        return "unavailable"
    if result.returncode != 0:
        return "parser-error"
    return None


def _sample_findings(
    evidence: dict[str, Any], reason: str | None
) -> list[Finding]:
    findings: list[Finding] = []
    if reason is not None:
        findings.append(Finding(
            "media-packet-order-sample-incomplete", "info",
            "Bounded physical packet-order sampling did not complete",
            evidence={
                "reason": reason,
                "sampled_packet_count": evidence["sampled_packet_count"],
                "automatic_action": False,
            },
        ))
    elif not evidence["sampled_packet_count"]:
        findings.append(Finding(
            "media-packet-order-sample-empty", "info",
            "No timestamped packets were available in the bounded sample",
            evidence={"automatic_action": False},
        ))
    if evidence["threshold_crossed_stream_count"]:
        findings.append(Finding(
            "media-packet-interleave-skew", "warning",
            "Audio/video physical packet order has severe timestamp skew",
            evidence={
                "maximum_audio_lag_seconds": evidence["maximum_audio_lag_seconds"],
                "maximum_audio_lead_seconds": evidence["maximum_audio_lead_seconds"],
                "threshold_seconds": evidence["threshold_seconds"],
                "affected_audio_stream_count": evidence["threshold_crossed_stream_count"],
                "sampled_packet_count": evidence["sampled_packet_count"],
                "automatic_action": False,
            },
        ))
    return findings


def sample_packet_order(
    path: Path, raw_streams: list[object], settings: AudiovisualAnalysisConfig
) -> tuple[dict[str, Any], list[Finding]]:
    if not settings.packet_order_sampling:
        return {}, []
    video_index = _primary_video_index(raw_streams)
    audio_indexes = _stream_indexes(raw_streams, "audio")
    if video_index is None or not audio_indexes:
        return {}, []
    result = _packet_probe_result(path, settings)
    reason = _incomplete_reason(result)
    evidence = _packet_order_evidence(
        result.stdout, raw_streams, settings, analysis_complete=reason is None
    )
    if reason is None and evidence["discarded_packet_row_count"]:
        reason = "malformed-output"
        evidence["analysis_complete"] = False
    return evidence, _sample_findings(evidence, reason)
