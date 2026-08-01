"""Bounded local image decoding, EXIF evidence, and quality signals."""

from __future__ import annotations

import datetime as dt
import math
import os
from pathlib import Path
import re
import shutil
import signal
import struct
import warnings

try:
    from PIL import Image, ImageOps
except ImportError:  # Optional local capability.
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]

from .bounded import BoundedProcessResult, run_bounded
from .config import PhotoAnalysisConfig
from .model import Finding


THUMBNAIL_SIDE = 64
THUMBNAIL_BYTES = THUMBNAIL_SIDE * THUMBNAIL_SIDE
MAX_EXIF_BYTES = 2_097_152
DEEP_IMAGE_FORMATS = {"bmp", "gif", "heif", "jpeg", "png", "tiff", "webp"}
TYPE_SIZES = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8}


class ExifParseError(ValueError):
    """Embedded EXIF metadata is malformed or outside the read bound."""


class PhotoDecodeTimeout(TimeoutError):
    """An in-process image decode exceeded its wall-clock bound."""


def _jpeg_exif(data: bytes) -> bytes | None:
    position = 2
    while position + 4 <= len(data) and data[position] == 0xFF:
        marker = data[position + 1]
        position += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        length = int.from_bytes(data[position:position + 2], "big")
        if length < 2 or position + length > len(data):
            return None
        payload = data[position + 2:position + length]
        if marker == 0xE1 and payload.startswith(b"Exif\x00\x00"):
            return payload[6:]
        position += length
    return None


def _png_exif(data: bytes) -> bytes | None:
    position = 8
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        chunk_type = data[position + 4:position + 8]
        end = position + 12 + length
        if end > len(data):
            return None
        if chunk_type == b"eXIf":
            return data[position + 8:position + 8 + length]
        if chunk_type == b"IEND":
            return None
        position = end
    return None


def _tiff_readers(data: bytes):
    if len(data) < 8 or data[:2] not in {b"II", b"MM"}:
        raise ExifParseError
    endian = "<" if data[:2] == b"II" else ">"
    if struct.unpack_from(endian + "H", data, 2)[0] != 42:
        raise ExifParseError

    def unsigned_short(offset: int) -> int:
        if offset < 0 or offset + 2 > len(data):
            raise ExifParseError
        return struct.unpack_from(endian + "H", data, offset)[0]

    def unsigned_long(offset: int) -> int:
        if offset < 0 or offset + 4 > len(data):
            raise ExifParseError
        return struct.unpack_from(endian + "I", data, offset)[0]

    return endian, unsigned_short, unsigned_long


def _ifd_entries(data: bytes, offset: int) -> dict[int, tuple[int, int, bytes]]:
    endian, short, long = _tiff_readers(data)
    count = min(short(offset), 4096)
    entries: dict[int, tuple[int, int, bytes]] = {}
    for index in range(count):
        position = offset + 2 + index * 12
        if position + 12 > len(data):
            raise ExifParseError
        tag, value_type = struct.unpack_from(endian + "HH", data, position)
        value_count = long(position + 4)
        size = TYPE_SIZES.get(value_type, 0) * value_count
        if size <= 0 or size > MAX_EXIF_BYTES:
            continue
        if size <= 4:
            raw = data[position + 8:position + 8 + size]
        else:
            value_offset = long(position + 8)
            if value_offset + size > len(data):
                continue
            raw = data[value_offset:value_offset + size]
        entries[tag] = (value_type, value_count, raw)
    return entries


def _entry_integer(
    entry: tuple[int, int, bytes] | None, endian: str
) -> int | None:
    if entry is None:
        return None
    value_type, _, raw = entry
    if value_type == 3 and len(raw) >= 2:
        return struct.unpack_from(endian + "H", raw)[0]
    if value_type == 4 and len(raw) >= 4:
        return struct.unpack_from(endian + "I", raw)[0]
    return None


def _entry_text(entry: tuple[int, int, bytes] | None) -> str:
    if entry is None or entry[0] != 2:
        return ""
    return " ".join(entry[2].split(b"\x00", 1)[0].decode(
        "utf-8", errors="replace"
    ).split())[:256]


def parse_tiff_exif(data: bytes) -> dict[str, object]:
    endian, _, long = _tiff_readers(data)
    root = _ifd_entries(data, long(4))
    exif_offset = _entry_integer(root.get(0x8769), endian)
    gps_offset = _entry_integer(root.get(0x8825), endian)
    exif = _ifd_entries(data, exif_offset) if exif_offset is not None else {}
    return {
        "make": _entry_text(root.get(0x010F)),
        "model": _entry_text(root.get(0x0110)),
        "orientation": _entry_integer(root.get(0x0112), endian),
        "capture_time_raw": _entry_text(exif.get(0x9003)),
        "timezone_offset": _entry_text(exif.get(0x9011)),
        "lens_model": _entry_text(exif.get(0xA434)),
        "gps_present": gps_offset is not None,
    }


def _read_exif(path: Path, detected_format: str) -> tuple[dict[str, object], bool]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            data = handle.read(MAX_EXIF_BYTES)
        payload = (
            _jpeg_exif(data) if detected_format == "jpeg"
            else _png_exif(data) if detected_format == "png"
            else data if detected_format == "tiff"
            else None
        )
        return (parse_tiff_exif(payload), True) if payload else ({}, False)
    except (OSError, ExifParseError, struct.error):
        return {}, False


def _capture_time(raw: object) -> tuple[str | None, bool]:
    if not isinstance(raw, str) or not raw:
        return None, True
    match = re.fullmatch(r"(\d{4}):(\d{2}):(\d{2}) (\d{2}):(\d{2}):(\d{2})", raw)
    if match is None:
        return None, False
    value = "-".join(match.groups()[:3]) + "T" + ":".join(match.groups()[3:])
    try:
        dt.datetime.fromisoformat(value)
    except ValueError:
        return None, False
    return value, True


def _timezone_offset(raw: object) -> tuple[str | None, bool]:
    if not isinstance(raw, str) or not raw:
        return None, True
    match = re.fullmatch(r"[+-](\d{2}):(\d{2})", raw)
    if match is None:
        return None, False
    hours, minutes = (int(value) for value in match.groups())
    if hours > 14 or minutes > 59:
        return None, False
    return raw, True


def _perceptual_hash(pixels: bytes) -> str:
    samples: list[int] = []
    for row in range(8):
        y = min(63, round((row + 0.5) * THUMBNAIL_SIDE / 8 - 0.5))
        for column in range(9):
            x = min(63, round((column + 0.5) * THUMBNAIL_SIDE / 9 - 0.5))
            samples.append(pixels[y * THUMBNAIL_SIDE + x])
    value = 0
    for row in range(8):
        for column in range(8):
            left = samples[row * 9 + column]
            right = samples[row * 9 + column + 1]
            value = (value << 1) | int(left > right)
    return f"{value:016x}"


def _visual_descriptor(pixels: bytes) -> list[int]:
    descriptor: list[int] = []
    block = THUMBNAIL_SIDE // 4
    for block_y in range(4):
        for block_x in range(4):
            values = []
            for y in range(block_y * block, (block_y + 1) * block):
                start = y * THUMBNAIL_SIDE + block_x * block
                values.extend(pixels[start:start + block])
            descriptor.append(round(sum(values) / len(values)))
    return descriptor


def quality_evidence(pixels: bytes) -> tuple[dict[str, object], list[str]]:
    histogram = [0] * 256
    for value in pixels:
        histogram[value] += 1
    total = len(pixels)
    mean = sum(value * count for value, count in enumerate(histogram)) / total
    variance = sum(
        ((value - mean) ** 2) * count for value, count in enumerate(histogram)
    ) / total
    entropy = -sum(
        (count / total) * math.log2(count / total)
        for count in histogram if count
    )
    laplacian = []
    for y in range(1, THUMBNAIL_SIDE - 1):
        for x in range(1, THUMBNAIL_SIDE - 1):
            center = pixels[y * THUMBNAIL_SIDE + x]
            neighbors = (
                pixels[y * THUMBNAIL_SIDE + x - 1]
                + pixels[y * THUMBNAIL_SIDE + x + 1]
                + pixels[(y - 1) * THUMBNAIL_SIDE + x]
                + pixels[(y + 1) * THUMBNAIL_SIDE + x]
            )
            laplacian.append(abs(4 * center - neighbors))
    sharpness = sum(laplacian) / len(laplacian)
    dark_fraction = sum(histogram[:9]) / total
    bright_fraction = sum(histogram[247:]) / total
    reasons = []
    if dark_fraction >= 0.90:
        reasons.append("extreme-darkness")
    if bright_fraction >= 0.90:
        reasons.append("extreme-brightness")
    contrast = math.sqrt(variance)
    if sharpness < 2.0 and contrast < 8.0 and entropy < 3.0:
        reasons.append("very-low-detail")
    if sharpness < 4.0 and contrast >= 12.0 and entropy >= 3.0:
        reasons.append("possible-soft-focus")
    return {
        "mean_luminance": round(mean, 2),
        "contrast_standard_deviation": round(contrast, 2),
        "entropy_bits": round(entropy, 3),
        "edge_strength": round(sharpness, 3),
        "dark_fraction": round(dark_fraction, 4),
        "bright_fraction": round(bright_fraction, 4),
    }, reasons


def _decode_with_pillow(
    path: Path, settings: PhotoAnalysisConfig
) -> BoundedProcessResult:
    if Image is None or ImageOps is None:
        return BoundedProcessResult(None, b"", b"", unavailable=True)

    def timeout_handler(_signum, _frame):
        raise PhotoDecodeTimeout

    old_handler = signal.getsignal(signal.SIGALRM)
    old_limit = Image.MAX_IMAGE_PIXELS
    descriptor: int | None = None
    try:
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.setitimer(signal.ITIMER_REAL, settings.parser_timeout_seconds)
        Image.MAX_IMAGE_PIXELS = settings.max_image_pixels
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as handle, warnings.catch_warnings():
            descriptor = None
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(handle) as source:
                source.seek(0)
                source.draft("L", (64, 64))
                source.load()
                oriented = ImageOps.exif_transpose(source)
                grayscale = oriented.convert("L").resize(
                    (64, 64), Image.Resampling.LANCZOS
                )
                pixels = grayscale.tobytes()
        return BoundedProcessResult(0, pixels, b"")
    except PhotoDecodeTimeout:
        return BoundedProcessResult(None, b"", b"", timed_out=True)
    except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning):
        return BoundedProcessResult(1, b"", b"")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        Image.MAX_IMAGE_PIXELS = old_limit
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def _decode_with_imagemagick(
    path: Path, settings: PhotoAnalysisConfig
) -> BoundedProcessResult:
    executable = shutil.which("magick")
    if executable is None:
        return BoundedProcessResult(None, b"", b"", unavailable=True)
    return run_bounded(
        [
            executable, f"{path}[0]", "-auto-orient", "-resize", "64x64!",
            "-colorspace", "Gray", "-depth", "8", "gray:-",
        ],
        settings.parser_timeout_seconds,
        65_536,
        settings.max_parser_memory_bytes,
    )


def _decode_thumbnail(
    path: Path, settings: PhotoAnalysisConfig
) -> BoundedProcessResult:
    if Image is not None:
        return _decode_with_pillow(path, settings)
    return _decode_with_imagemagick(path, settings)


def _decode_failure(result: BoundedProcessResult) -> Finding | None:
    if result.timed_out:
        return Finding(
            "photo-analysis-timeout", "warning",
            "Bounded local image analysis timed out",
        )
    if result.output_limited:
        return Finding(
            "photo-analysis-output-limited", "warning",
            "Local image analyzer exceeded its output bound",
        )
    if result.returncode != 0 or len(result.stdout) != THUMBNAIL_BYTES:
        return Finding(
            "photo-decode-failed", "error",
            "Local image decoder could not produce bounded visual evidence",
        )
    return None


def analyze_photo(
    path: Path,
    detected_format: str | None,
    settings: PhotoAnalysisConfig,
) -> tuple[dict[str, object], list[Finding]]:
    if not settings.enabled or detected_format not in DEEP_IMAGE_FORMATS:
        return {}, []
    exif, exif_present = _read_exif(path, detected_format)
    capture_time, capture_valid = _capture_time(exif.get("capture_time_raw"))
    timezone, timezone_valid = _timezone_offset(exif.get("timezone_offset"))
    photo: dict[str, object] = {
        "exif_present": exif_present,
        "capture_time": capture_time,
        "capture_time_valid": capture_valid,
        "timezone_offset": timezone,
        "timezone_offset_valid": timezone_valid,
        "camera_make": exif.get("make") or None,
        "camera_model": exif.get("model") or None,
        "lens_model": exif.get("lens_model") or None,
        "orientation": exif.get("orientation"),
    }
    if settings.location_detail == "presence":
        photo["location"] = {"gps_present": bool(exif.get("gps_present"))}
    result = _decode_thumbnail(path, settings)
    if result.unavailable:
        return {"photo": photo}, []
    if failure := _decode_failure(result):
        return {"photo": photo}, [failure]
    pixels = result.stdout
    photo["deep_decode"] = True
    photo["deep_decoder"] = "pillow" if Image is not None else "imagemagick"
    photo["visual_fingerprint"] = {
        "algorithm": "dhash-64-v1",
        "value": _perceptual_hash(pixels),
        "local_descriptor_v1": _visual_descriptor(pixels),
    }
    findings: list[Finding] = []
    if settings.quality_signals:
        quality, reasons = quality_evidence(pixels)
        photo["quality"] = quality
        if reasons:
            findings.append(
                Finding(
                    "photo-quality-review", "warning",
                    "Bounded quality signals suggest human review",
                    evidence={"reasons": reasons},
                )
            )
    return {"photo": photo}, findings
