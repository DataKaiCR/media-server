"""Bounded local audiovisual, stream, subtitle, and provenance evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
from typing import Any

from .bounded import BoundedProcessResult, run_bounded
from .config import AudiovisualAnalysisConfig
from .model import Finding


VIDEO_EXTENSIONS = {
    ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".ts", ".vob", ".webm", ".wmv",
}
AUDIO_EXTENSIONS = {
    ".aac", ".flac", ".m4a", ".mka", ".mp3", ".ogg", ".opus", ".wav",
}
SUBTITLE_EXTENSIONS = {".ass", ".idx", ".srt", ".ssa", ".sub", ".vtt"}
TEXT_SUBTITLE_EXTENSIONS = {".ass", ".idx", ".srt", ".ssa", ".vtt"}
ARTWORK_EXTENSIONS = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".svg", ".webp"}
MEDIA_METADATA_EXTENSIONS = {".nfo"}
PROBE_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS

_XATTRS = {
    "generated": "user.media_server.generated",
    "source": "user.media_server.subtitle_source",
    "subtitle_hash": "user.media_server.subtitle_sha256",
    "source_hash": "user.media_server.source_sha256",
    "model": "user.media_server.translation_model",
    "target_language": "user.media_server.target_language",
}
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LANGUAGE_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$", re.IGNORECASE)
_SRT_TIME_RE = re.compile(
    r"^(?P<start>\d{1,3}:\d{2}:\d{2}[,.]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,3}:\d{2}:\d{2}[,.]\d{3})(?:\s+.*)?$"
)
_VTT_TIME_RE = re.compile(
    r"^(?P<start>(?:\d{1,3}:)?\d{2}:\d{2}\.\d{3})\s+-->\s+"
    r"(?P<end>(?:\d{1,3}:)?\d{2}:\d{2}\.\d{3})(?:\s+.*)?$"
)
_IDX_TIME_RE = re.compile(r"^timestamp:\s*(\d{1,3}:\d{2}:\d{2}:\d{3})", re.IGNORECASE)


def _positive_float(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or result <= 0:
        return None
    return result


def _bounded_integer(value: object, maximum: int = 1_000_000_000) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= maximum else None


def _language(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().replace("_", "-")
    return normalized if _LANGUAGE_RE.fullmatch(normalized) else None


def _rate(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        rate = float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None
    return round(rate, 3) if math.isfinite(rate) and 0 < rate < 1000 else None


def _safe_formats(value: object) -> list[str]:
    if not isinstance(value, str):
        return []
    return [
        item for item in value.split(",")[:8]
        if re.fullmatch(r"[a-z0-9_]{1,32}", item)
    ]


def _stream_summary(stream: object) -> dict[str, Any] | None:
    if not isinstance(stream, dict):
        return None
    stream_type = stream.get("codec_type")
    if stream_type not in {"video", "audio", "subtitle", "attachment", "data"}:
        return None
    codec = stream.get("codec_name")
    result: dict[str, Any] = {
        "type": stream_type,
        "codec": codec if isinstance(codec, str) and re.fullmatch(r"[a-z0-9_]{1,32}", codec) else None,
    }
    language = _language((stream.get("tags") or {}).get("language") if isinstance(stream.get("tags"), dict) else None)
    if language:
        result["language"] = language
    disposition = stream.get("disposition")
    if isinstance(disposition, dict):
        result["default"] = bool(disposition.get("default"))
        result["forced"] = bool(disposition.get("forced"))
    if stream_type == "video":
        result.update({
            "width": _bounded_integer(stream.get("width"), 100_000),
            "height": _bounded_integer(stream.get("height"), 100_000),
            "pixel_format": (
                stream.get("pix_fmt")
                if isinstance(stream.get("pix_fmt"), str)
                and re.fullmatch(r"[a-z0-9_]{1,32}", stream["pix_fmt"])
                else None
            ),
            "frame_rate": _rate(stream.get("r_frame_rate")),
        })
    if stream_type == "audio":
        result.update({
            "channels": _bounded_integer(stream.get("channels"), 128),
            "channel_layout": (
                stream.get("channel_layout")
                if isinstance(stream.get("channel_layout"), str)
                and re.fullmatch(r"[A-Za-z0-9_.()+ -]{1,64}", stream["channel_layout"])
                else None
            ),
        })
    return result


def _probe_result(path: Path, settings: AudiovisualAnalysisConfig) -> BoundedProcessResult:
    return run_bounded(
        [
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_entries",
            "format=format_name,duration,bit_rate:"
            "stream=codec_type,codec_name,width,height,pix_fmt,r_frame_rate,channels,channel_layout:"
            "stream_tags=language:stream_disposition=default,forced:"
            "chapter=start_time,end_time",
            str(path),
        ],
        settings.parser_timeout_seconds,
        settings.max_parser_output_bytes,
        settings.max_parser_memory_bytes,
    )


def probe_media(
    path: Path, extension: str, size: int, settings: AudiovisualAnalysisConfig
) -> tuple[dict[str, Any], list[Finding]]:
    if shutil.which("ffprobe") is None:
        return {}, []
    result = _probe_result(path, settings)
    if result.timed_out:
        return {}, [Finding("media-probe-timeout", "warning", "Bounded ffprobe analysis timed out")]
    if result.output_limited:
        return {}, [Finding("media-probe-output-limit", "warning", "Bounded ffprobe output exceeded its limit")]
    if result.unavailable:
        return {}, [Finding("media-probe-unavailable", "warning", "ffprobe could not be executed")]
    if result.returncode != 0:
        return {}, [Finding("media-container-invalid", "error", "ffprobe rejected the media container")]
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, [Finding("media-probe-invalid-output", "warning", "ffprobe returned invalid bounded JSON")]
    if not isinstance(payload, dict):
        return {}, [Finding("media-probe-invalid-output", "warning", "ffprobe returned an unexpected document")]

    raw_streams = payload.get("streams")
    streams = [summary for raw in (raw_streams if isinstance(raw_streams, list) else [])[:128] if (summary := _stream_summary(raw))]
    format_row = payload.get("format") if isinstance(payload.get("format"), dict) else {}
    duration = _positive_float(format_row.get("duration"))
    bit_rate = _bounded_integer(format_row.get("bit_rate"), 10_000_000_000)
    media = {
        "container_formats": _safe_formats(format_row.get("format_name")),
        "duration_seconds": round(duration, 3) if duration is not None else None,
        "bit_rate_bits_per_second": bit_rate,
        "stream_count": len(raw_streams) if isinstance(raw_streams, list) else 0,
        "streams": streams,
        "chapter_count": len(payload.get("chapters")) if isinstance(payload.get("chapters"), list) else 0,
    }
    findings: list[Finding] = []
    video_streams = [stream for stream in streams if stream["type"] == "video"]
    audio_streams = [stream for stream in streams if stream["type"] == "audio"]
    if not streams:
        findings.append(Finding("media-streams-missing", "error", "Media container has no readable streams"))
    if extension in VIDEO_EXTENSIONS and not video_streams:
        findings.append(Finding("video-stream-missing", "error", "Video file has no readable video stream"))
    if extension in VIDEO_EXTENSIONS and not audio_streams:
        findings.append(Finding("audio-stream-missing", "info", "Video file has no readable audio stream"))
    if duration is None:
        findings.append(Finding("media-duration-missing", "warning", "Media runtime is unavailable or invalid"))
    if any(not stream.get("width") or not stream.get("height") for stream in video_streams):
        findings.append(Finding("video-dimensions-invalid", "error", "A video stream has missing or invalid dimensions"))
    large = size >= settings.large_file_bytes
    high_bitrate = bit_rate is not None and bit_rate >= settings.high_bitrate_bits_per_second
    if large or high_bitrate:
        findings.append(
            Finding(
                "oversized-media-review", "info",
                "Media crosses a configured size or bitrate review threshold; no action is proposed",
                evidence={
                    "bytes": size,
                    "bit_rate_bits_per_second": bit_rate,
                    "large_file_threshold_crossed": large,
                    "high_bitrate_threshold_crossed": high_bitrate,
                    "automatic_action": False,
                },
            )
        )
    return media, findings


def _clock_seconds(value: str, millisecond_separator: str = ".") -> float | None:
    normalized = value.replace(",", millisecond_separator)
    parts = normalized.split(":")
    if len(parts) not in {2, 3}:
        return None
    try:
        hours = int(parts[0]) if len(parts) == 3 else 0
        minutes = int(parts[-2])
        seconds = float(parts[-1])
    except ValueError:
        return None
    if hours < 0 or not 0 <= minutes < 60 or not 0 <= seconds < 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def _timing_evidence(pairs: list[tuple[float, float]], malformed: int) -> dict[str, Any]:
    nonpositive = 0
    regressions = 0
    overlaps = 0
    previous: tuple[float, float] | None = None
    for start, end in pairs:
        nonpositive += start >= end
        if previous is not None:
            regressions += start < previous[0] or end < previous[1]
            overlaps += start < previous[1]
        previous = (start, end)
    return {
        "cue_count": len(pairs),
        "malformed_timing_count": malformed,
        "nonpositive_duration_count": nonpositive,
        "timing_regression_count": regressions,
        "overlap_count": overlaps,
        "first_start_seconds": round(min((pair[0] for pair in pairs), default=0), 3) if pairs else None,
        "last_end_seconds": round(max((pair[1] for pair in pairs), default=0), 3) if pairs else None,
    }


def _parse_text_timing(text: str, extension: str) -> dict[str, Any]:
    pattern = _SRT_TIME_RE if extension == ".srt" else _VTT_TIME_RE
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    pairs: list[tuple[float, float]] = []
    malformed = 0
    empty_text = 0
    for position, line in enumerate(lines):
        if "-->" not in line:
            continue
        match = pattern.fullmatch(line.strip())
        if not match:
            malformed += 1
            continue
        start = _clock_seconds(match.group("start"))
        end = _clock_seconds(match.group("end"))
        if start is None or end is None:
            malformed += 1
            continue
        pairs.append((start, end))
        content: list[str] = []
        for candidate in lines[position + 1:]:
            if not candidate.strip() or "-->" in candidate:
                break
            content.append(candidate)
        empty_text += not any(value.strip() for value in content)
    evidence = _timing_evidence(pairs, malformed)
    evidence["empty_text_cue_count"] = empty_text
    evidence["format"] = "srt" if extension == ".srt" else "webvtt"
    return evidence


def _parse_ass(text: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    malformed = 0
    empty_text = 0
    columns: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if line.casefold().startswith("format:"):
            columns = [item.strip().casefold() for item in line.split(":", 1)[1].split(",")]
        if not line.casefold().startswith("dialogue:"):
            continue
        values = [item.strip() for item in line.split(":", 1)[1].split(",", max(9, len(columns) - 1))]
        try:
            start_index = columns.index("start") if columns else 1
            end_index = columns.index("end") if columns else 2
            start = _clock_seconds(values[start_index])
            end = _clock_seconds(values[end_index])
        except (ValueError, IndexError):
            start = end = None
        if start is None or end is None:
            malformed += 1
        else:
            pairs.append((start, end))
            text_index = columns.index("text") if "text" in columns else len(values) - 1
            empty_text += text_index >= len(values) or not values[text_index].strip()
    evidence = _timing_evidence(pairs, malformed)
    evidence["empty_text_cue_count"] = empty_text
    evidence["format"] = "ass"
    return evidence


def _parse_idx(text: str) -> dict[str, Any]:
    pairs: list[tuple[float, float]] = []
    malformed = 0
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.casefold().startswith("timestamp:"):
            continue
        match = _IDX_TIME_RE.match(line.strip())
        if not match:
            malformed += 1
            continue
        hours, minutes, seconds, milliseconds = match.group(1).split(":")
        start = _clock_seconds(f"{hours}:{minutes}:{seconds}.{milliseconds}")
        if start is None:
            malformed += 1
        else:
            pairs.append((start, start + 0.001))
    evidence = _timing_evidence(pairs, malformed)
    evidence["format"] = "vobsub-index"
    evidence["last_end_seconds"] = evidence["first_start_seconds"] if len(pairs) == 1 else evidence["last_end_seconds"]
    return evidence


def _xattr_values(path: Path) -> dict[str, bytes]:
    values: dict[str, bytes] = {}
    for key, name in _XATTRS.items():
        try:
            value = os.getxattr(path, name, follow_symlinks=False)
        except OSError:
            continue
        if len(value) <= 512:
            values[key] = value
    return values


def subtitle_provenance(path: Path, raw: bytes) -> tuple[dict[str, Any], list[Finding]]:
    values = _xattr_values(path)
    generated = values.get("generated") == b"true"
    source_raw = values.get("source", b"").decode("ascii", errors="ignore")
    source = source_raw if source_raw in {"whisperai", "ollama-translation"} else None
    marked_hash = values.get("subtitle_hash", b"").decode("ascii", errors="ignore").casefold()
    current_hash = hashlib.sha256(raw).hexdigest()
    hash_valid = bool(_HASH_RE.fullmatch(marked_hash))
    target = _language(values.get("target_language", b"").decode("ascii", errors="ignore"))
    evidence = {
        "classification": source if generated and source else ("generated-unknown" if generated else "human-or-unmarked"),
        "generated_marker": generated,
        "source_marker_recognized": source is not None,
        "subtitle_hash_marker_present": "subtitle_hash" in values,
        "subtitle_hash_marker_valid": hash_valid,
        "subtitle_hash_matches": hash_valid and marked_hash == current_hash,
        "source_hash_marker_present": bool(_HASH_RE.fullmatch(values.get("source_hash", b"").decode("ascii", errors="ignore").casefold())),
        "translation_model_marker_present": bool(values.get("model")),
        "target_language": target,
    }
    findings: list[Finding] = []
    if generated:
        required = source is not None and hash_valid
        if source == "ollama-translation":
            required = required and evidence["source_hash_marker_present"] and evidence["translation_model_marker_present"] and target is not None
        if not required:
            findings.append(Finding("subtitle-provenance-incomplete", "warning", "Generated subtitle provenance markers are incomplete or invalid"))
        if hash_valid and marked_hash != current_hash:
            findings.append(Finding("subtitle-provenance-mismatch", "error", "Generated subtitle content no longer matches its provenance hash"))
    elif values:
        findings.append(Finding("subtitle-provenance-stale", "warning", "Subtitle has provenance markers without an active generated marker"))
    return evidence, findings


def analyze_subtitle(
    path: Path, extension: str, size: int, settings: AudiovisualAnalysisConfig
) -> tuple[dict[str, Any], list[Finding]]:
    if size > settings.subtitle_max_bytes:
        return {
            "format": extension.removeprefix("."),
            "analysis_complete": False,
            "size_limit_exceeded": True,
        }, [Finding("subtitle-analysis-size-limit", "warning", "Subtitle exceeds the configured bounded analysis size")]
    try:
        raw = path.read_bytes()
    except OSError:
        return {}, [Finding("subtitle-read-failed", "error", "Subtitle could not be read")]
    provenance, findings = subtitle_provenance(path, raw)
    metadata: dict[str, Any] = {"provenance": provenance, "analysis_complete": False}
    if not raw:
        findings.append(Finding("subtitle-empty", "error", "Subtitle is empty"))
        return metadata, findings
    if extension == ".sub":
        metadata.update({"format": "vobsub-payload", "binary_timing_validation": False})
        findings.append(Finding("subtitle-validation-limited", "info", "Binary subtitle timing requires its matching index and is not deeply parsed"))
        return metadata, findings
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError:
        findings.append(Finding("subtitle-non-utf8", "warning", "Text subtitle is not valid UTF-8"))
        return metadata, findings
    if "\x00" in text:
        findings.append(Finding("subtitle-nul-bytes", "error", "Text subtitle contains NUL characters"))
        return metadata, findings
    if extension in {".srt", ".vtt"}:
        timing = _parse_text_timing(text, extension)
    elif extension in {".ass", ".ssa"}:
        timing = _parse_ass(text)
    elif extension == ".idx":
        timing = _parse_idx(text)
    else:
        timing = {"format": extension.removeprefix("."), "cue_count": 0}
    metadata.update(timing)
    metadata["analysis_complete"] = True
    if not timing.get("cue_count"):
        findings.append(Finding("subtitle-cues-missing", "error", "Subtitle has no readable timing cues"))
    if timing.get("malformed_timing_count") or timing.get("nonpositive_duration_count") or timing.get("timing_regression_count") or timing.get("empty_text_cue_count"):
        findings.append(Finding("subtitle-timing-invalid", "warning", "Subtitle contains malformed, empty, or regressing cue evidence"))
    if timing.get("overlap_count"):
        findings.append(Finding("subtitle-timing-overlap", "info", "Subtitle has overlapping cues that merit review but may be intentional"))
    return metadata, findings


def analyze_audiovisual(
    path: Path, extension: str, settings: AudiovisualAnalysisConfig
) -> tuple[dict[str, Any], list[Finding]]:
    if not settings.enabled:
        return {}, []
    try:
        size = path.stat().st_size
    except OSError:
        return {}, [Finding("audiovisual-stat-failed", "error", "Audiovisual file metadata cannot be read")]
    if extension in PROBE_EXTENSIONS:
        media, findings = probe_media(path, extension, size, settings)
        return ({"media": media} if media else {}), findings
    if extension in SUBTITLE_EXTENSIONS:
        subtitle, findings = analyze_subtitle(path, extension, size, settings)
        return {"subtitle": subtitle}, findings
    return {}, []
