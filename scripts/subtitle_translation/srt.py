"""Strict UTF-8 SRT parsing and timing-preserving rendering."""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import Iterable

from .errors import ValidationError


MAX_SRT_BYTES = 16_777_216
MAX_SRT_CUES = 100_000
MAX_CUE_TEXT_CHARS = 10_000
TIMESTAMP_RE = re.compile(
    r"^(?P<start>\d{2,6}:\d{2}:\d{2},\d{3}) --> "
    r"(?P<end>\d{2,6}:\d{2}:\d{2},\d{3})(?P<settings>(?: .*)?)$"
)


@dataclass(frozen=True)
class Cue:
    index: int
    timestamp: str
    start_ms: int
    end_ms: int
    text: str


def timestamp_ms(value: str) -> int:
    hours, minutes, rest = value.split(":")
    seconds, milliseconds = rest.split(",")
    fields = (int(hours), int(minutes), int(seconds), int(milliseconds))
    if fields[1] >= 60 or fields[2] >= 60 or fields[3] >= 1000:
        raise ValidationError(f"invalid timestamp component: {value}")
    return ((fields[0] * 60 + fields[1]) * 60 + fields[2]) * 1000 + fields[3]


def _subtitle_text(raw: bytes) -> str:
    if not raw:
        raise ValidationError("subtitle is empty")
    if len(raw) > MAX_SRT_BYTES:
        raise ValidationError("subtitle exceeds the safe input size")
    if b"\x00" in raw:
        raise ValidationError("subtitle contains NUL bytes")
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as error:
        raise ValidationError("subtitle is not valid UTF-8") from error
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if not text.strip():
        raise ValidationError("subtitle has no content")
    return text


def _runtime_milliseconds(runtime_seconds: float | None) -> int | None:
    if runtime_seconds is None:
        return None
    if (
        isinstance(runtime_seconds, bool)
        or not isinstance(runtime_seconds, (int, float))
        or not math.isfinite(runtime_seconds)
        or runtime_seconds <= 0
    ):
        raise ValidationError("media runtime must be finite and positive")
    return round(runtime_seconds * 1000)


def parse_srt_bytes(raw: bytes, runtime_seconds: float | None = None) -> list[Cue]:
    text = _subtitle_text(raw)
    runtime_ms = _runtime_milliseconds(runtime_seconds)
    blocks = re.split(r"\n[ \t]*\n", text)
    if len(blocks) > MAX_SRT_CUES:
        raise ValidationError("subtitle exceeds the safe cue count")
    cues: list[Cue] = []
    seen_indexes: set[int] = set()
    seen_timings: set[tuple[int, int]] = set()
    previous: Cue | None = None

    for position, block in enumerate(blocks, start=1):
        lines = block.split("\n")
        if len(lines) < 3:
            raise ValidationError(f"cue block {position} is incomplete")
        if not re.fullmatch(r"\d{1,9}", lines[0]):
            raise ValidationError(f"cue block {position} has an invalid index")
        index = int(lines[0])
        if index <= 0 or index in seen_indexes:
            raise ValidationError(f"cue index {index} is invalid or duplicated")
        match = TIMESTAMP_RE.fullmatch(lines[1])
        if not match:
            raise ValidationError(f"cue {index} has a malformed timestamp line")
        start_ms = timestamp_ms(match.group("start"))
        end_ms = timestamp_ms(match.group("end"))
        if start_ms >= end_ms:
            raise ValidationError(f"cue {index} does not have positive duration")
        timing = (start_ms, end_ms)
        if timing in seen_timings:
            raise ValidationError(f"cue {index} duplicates an earlier timestamp")
        content = "\n".join(lines[2:]).strip()
        if not content:
            raise ValidationError(f"cue {index} has empty text")
        if len(content) > MAX_CUE_TEXT_CHARS:
            raise ValidationError(f"cue {index} exceeds the safe text size")
        cue = Cue(index, lines[1], start_ms, end_ms, content)
        if previous is not None:
            if cue.index <= previous.index:
                raise ValidationError("cue indexes regress")
            if cue.start_ms < previous.start_ms or cue.end_ms < previous.end_ms:
                raise ValidationError("cue timestamps regress")
            if cue.start_ms < previous.end_ms:
                raise ValidationError("cue timestamps overlap")
        if runtime_ms is not None and cue.end_ms > runtime_ms:
            raise ValidationError(f"cue {index} ends beyond media runtime")
        seen_indexes.add(index)
        seen_timings.add(timing)
        cues.append(cue)
        previous = cue

    if not cues:
        raise ValidationError("subtitle contains no cues")
    if runtime_ms is not None and runtime_ms >= 20 * 60 * 1000:
        if cues[-1].end_ms < runtime_ms * 0.5:
            raise ValidationError("subtitle appears truncated before the media midpoint")
    return cues


def parse_srt(path: Path, runtime_seconds: float | None = None) -> list[Cue]:
    if path.is_symlink():
        raise ValidationError("source subtitle must not be a symlink")
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_SRT_BYTES + 1)
    except OSError as error:
        raise ValidationError(f"cannot read source subtitle: {error.strerror}") from error
    return parse_srt_bytes(raw, runtime_seconds)


def render_srt(cues: Iterable[Cue]) -> bytes:
    blocks = [f"{cue.index}\n{cue.timestamp}\n{cue.text}" for cue in cues]
    return ("\n\n".join(blocks) + "\n").encode("utf-8")
