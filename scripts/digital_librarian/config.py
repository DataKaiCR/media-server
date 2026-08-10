"""Strict TOML configuration for private digital collections."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import tomllib


COLLECTION_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
COLLECTION_KINDS = {"audiovisual", "books", "photos"}
COLLECTION_ROLES = {"library", "intake"}
AUDIOVISUAL_LAYOUTS = {"mixed", "movies", "series"}


class ConfigError(ValueError):
    """The Librarian configuration is invalid or unsafe."""


@dataclass(frozen=True)
class CollectionConfig:
    collection_id: str
    kind: str
    role: str
    root: Path
    exclude_globs: tuple[str, ...]
    media_layout: str | None = None


@dataclass(frozen=True)
class AudiovisualAnalysisConfig:
    enabled: bool = True
    parser_timeout_seconds: int = 30
    max_parser_output_bytes: int = 4_194_304
    max_parser_memory_bytes: int = 1_073_741_824
    packet_order_sampling: bool = True
    packet_sample_packets: int = 50_000
    interleave_skew_threshold_seconds: int = 30
    subtitle_max_bytes: int = 8_388_608
    large_file_bytes: int = 21_474_836_480
    high_bitrate_bits_per_second: int = 40_000_000
    subtitle_runtime_tolerance_seconds: int = 30


@dataclass(frozen=True)
class BookAnalysisConfig:
    pdf_text_layer: bool = True
    pdf_sample_pages: int = 12
    parser_timeout_seconds: int = 30
    max_parser_output_bytes: int = 1_048_576
    max_parser_memory_bytes: int = 1_073_741_824


@dataclass(frozen=True)
class PhotoAnalysisConfig:
    enabled: bool = True
    location_detail: str = "presence"
    quality_signals: bool = True
    perceptual_duplicates: bool = True
    near_duplicate_distance: int = 4
    burst_window_seconds: int = 2
    burst_max_span_seconds: int = 15
    parser_timeout_seconds: int = 20
    max_parser_memory_bytes: int = 1_073_741_824
    max_image_pixels: int = 50_000_000


@dataclass(frozen=True)
class LibrarianConfig:
    report_dir: Path
    collections: tuple[CollectionConfig, ...]
    audiovisual_analysis: AudiovisualAnalysisConfig
    book_analysis: BookAnalysisConfig
    photo_analysis: PhotoAnalysisConfig


def _absolute_directory(value: object, field_name: str, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field_name} must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{field_name} must be absolute")
    if path.is_symlink():
        raise ConfigError(f"{field_name} must not be a symlink")
    if must_exist and not path.is_dir():
        raise ConfigError(f"{field_name} must be an existing directory")
    return path.resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _bounded_integer(
    table: dict[str, object], section: str, key: str,
    default: int, bounds: tuple[int, int],
) -> int:
    minimum, maximum = bounds
    value = table.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{section}.{key} must be between {minimum} and {maximum}")
    return value


def _audiovisual_config(
    table: dict[str, object], enabled: bool, packet_order_sampling: bool
) -> AudiovisualAnalysisConfig:
    section = "audiovisual_analysis"
    return AudiovisualAnalysisConfig(
        enabled=enabled,
        parser_timeout_seconds=_bounded_integer(
            table, section, "parser_timeout_seconds", 30, (1, 120)
        ),
        max_parser_output_bytes=_bounded_integer(
            table, section, "max_parser_output_bytes",
            4_194_304, (65_536, 16_777_216),
        ),
        max_parser_memory_bytes=_bounded_integer(
            table, section, "max_parser_memory_bytes",
            1_073_741_824, (268_435_456, 4_294_967_296),
        ),
        packet_order_sampling=packet_order_sampling,
        packet_sample_packets=_bounded_integer(
            table, section, "packet_sample_packets", 50_000, (1_000, 100_000)
        ),
        interleave_skew_threshold_seconds=_bounded_integer(
            table, section, "interleave_skew_threshold_seconds", 30, (5, 600)
        ),
        subtitle_max_bytes=_bounded_integer(
            table, section, "subtitle_max_bytes", 8_388_608, (65_536, 67_108_864)
        ),
        large_file_bytes=_bounded_integer(
            table, section, "large_file_bytes",
            21_474_836_480, (1_073_741_824, 1_099_511_627_776),
        ),
        high_bitrate_bits_per_second=_bounded_integer(
            table, section, "high_bitrate_bits_per_second",
            40_000_000, (1_000_000, 500_000_000),
        ),
        subtitle_runtime_tolerance_seconds=_bounded_integer(
            table, section, "subtitle_runtime_tolerance_seconds", 30, (0, 300)
        ),
    )


def _audiovisual_analysis(value: object) -> AudiovisualAnalysisConfig:
    if value is None:
        return AudiovisualAnalysisConfig()
    if not isinstance(value, dict):
        raise ConfigError("audiovisual_analysis must be a TOML table")
    allowed = {
        "enabled", "parser_timeout_seconds", "max_parser_output_bytes",
        "max_parser_memory_bytes", "packet_order_sampling",
        "packet_sample_packets", "interleave_skew_threshold_seconds",
        "subtitle_max_bytes", "large_file_bytes",
        "high_bitrate_bits_per_second", "subtitle_runtime_tolerance_seconds",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown audiovisual_analysis setting: {sorted(unknown)[0]}")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("audiovisual_analysis.enabled must be true or false")
    packet_order_sampling = value.get("packet_order_sampling", True)
    if not isinstance(packet_order_sampling, bool):
        raise ConfigError(
            "audiovisual_analysis.packet_order_sampling must be true or false"
        )
    return _audiovisual_config(value, enabled, packet_order_sampling)


def _book_analysis(value: object) -> BookAnalysisConfig:
    if value is None:
        return BookAnalysisConfig()
    if not isinstance(value, dict):
        raise ConfigError("book_analysis must be a TOML table")
    allowed = {
        "pdf_text_layer", "pdf_sample_pages", "parser_timeout_seconds",
        "max_parser_output_bytes", "max_parser_memory_bytes",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown book_analysis setting: {sorted(unknown)[0]}")
    pdf_text_layer = value.get("pdf_text_layer", True)
    if not isinstance(pdf_text_layer, bool):
        raise ConfigError("book_analysis.pdf_text_layer must be true or false")
    return BookAnalysisConfig(
        pdf_text_layer=pdf_text_layer,
        pdf_sample_pages=_bounded_integer(
            value, "book_analysis", "pdf_sample_pages", 12, (1, 50)
        ),
        parser_timeout_seconds=_bounded_integer(
            value, "book_analysis", "parser_timeout_seconds", 30, (1, 120)
        ),
        max_parser_output_bytes=_bounded_integer(
            value, "book_analysis", "max_parser_output_bytes",
            1_048_576, (65_536, 8_388_608),
        ),
        max_parser_memory_bytes=_bounded_integer(
            value, "book_analysis", "max_parser_memory_bytes",
            1_073_741_824, (268_435_456, 4_294_967_296),
        ),
    )


def _photo_analysis(value: object) -> PhotoAnalysisConfig:
    if value is None:
        return PhotoAnalysisConfig()
    if not isinstance(value, dict):
        raise ConfigError("photo_analysis must be a TOML table")
    allowed = {
        "enabled", "location_detail", "quality_signals",
        "perceptual_duplicates", "near_duplicate_distance",
        "burst_window_seconds", "burst_max_span_seconds", "parser_timeout_seconds",
        "max_parser_memory_bytes", "max_image_pixels",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ConfigError(f"unknown photo_analysis setting: {sorted(unknown)[0]}")
    booleans: dict[str, bool] = {}
    for key in ("enabled", "quality_signals", "perceptual_duplicates"):
        setting = value.get(key, getattr(PhotoAnalysisConfig(), key))
        if not isinstance(setting, bool):
            raise ConfigError(f"photo_analysis.{key} must be true or false")
        booleans[key] = setting
    location = value.get("location_detail", "presence")
    if location not in {"none", "presence"}:
        raise ConfigError("photo_analysis.location_detail must be none or presence")
    return PhotoAnalysisConfig(
        **booleans,
        location_detail=str(location),
        near_duplicate_distance=_bounded_integer(
            value, "photo_analysis", "near_duplicate_distance", 4, (0, 8)
        ),
        burst_window_seconds=_bounded_integer(
            value, "photo_analysis", "burst_window_seconds", 2, (1, 10)
        ),
        burst_max_span_seconds=_bounded_integer(
            value, "photo_analysis", "burst_max_span_seconds", 15, (2, 120)
        ),
        parser_timeout_seconds=_bounded_integer(
            value, "photo_analysis", "parser_timeout_seconds", 20, (1, 120)
        ),
        max_parser_memory_bytes=_bounded_integer(
            value, "photo_analysis", "max_parser_memory_bytes",
            1_073_741_824, (268_435_456, 4_294_967_296),
        ),
        max_image_pixels=_bounded_integer(
            value, "photo_analysis", "max_image_pixels",
            50_000_000, (1_000_000, 200_000_000),
        ),
    )


def _private_document(path: Path) -> dict[str, object]:
    if not path.is_absolute():
        raise ConfigError("Librarian configuration path must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ConfigError(
            "Librarian configuration must be a regular non-symlink file"
        )
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError("Librarian configuration must be mode 0600 or stricter")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("cannot read Librarian configuration") from error
    allowed = {
        "version", "report_dir", "audiovisual_analysis", "book_analysis",
        "photo_analysis", "collections",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown top-level setting: {sorted(unknown)[0]}")
    return raw


def load_config(path: Path) -> LibrarianConfig:
    raw = _private_document(path)
    if raw.get("version") != 1:
        raise ConfigError("configuration version must be 1")
    report_dir = _absolute_directory(raw.get("report_dir"), "report_dir", False)
    audiovisual_analysis = _audiovisual_analysis(raw.get("audiovisual_analysis"))
    book_analysis = _book_analysis(raw.get("book_analysis"))
    photo_analysis = _photo_analysis(raw.get("photo_analysis"))
    rows = raw.get("collections")
    if not isinstance(rows, list) or not rows:
        raise ConfigError("at least one collection is required")

    collections: list[CollectionConfig] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("each collection must be a TOML table")
        collection_allowed = {
            "id", "kind", "role", "root", "exclude_globs", "media_layout",
        }
        collection_unknown = set(row) - collection_allowed
        if collection_unknown:
            raise ConfigError(
                f"unknown collection setting: {sorted(collection_unknown)[0]}"
            )
        collection_id = row.get("id")
        if not isinstance(collection_id, str) or not COLLECTION_ID_RE.fullmatch(collection_id):
            raise ConfigError("collection id must be a lowercase public-safe identifier")
        if collection_id in seen_ids:
            raise ConfigError(f"duplicate collection id: {collection_id}")
        kind = row.get("kind")
        if kind not in COLLECTION_KINDS:
            raise ConfigError(f"unsupported collection kind: {kind}")
        role = row.get("role", "library")
        if role not in COLLECTION_ROLES:
            raise ConfigError(f"unsupported collection role: {role}")
        if role == "intake" and kind != "books":
            raise ConfigError("the intake role is currently limited to book collections")
        root = _absolute_directory(
            row.get("root"), f"collection {collection_id} root", True
        )
        if role == "intake" and stat.S_IMODE(root.stat().st_mode) & 0o077:
            raise ConfigError("book intake root must be mode 0700 or more restrictive")
        excludes = row.get("exclude_globs", [])
        if not isinstance(excludes, list) or not all(
            isinstance(pattern, str) and pattern for pattern in excludes
        ):
            raise ConfigError(f"collection {collection_id} exclude_globs must be strings")
        media_layout = row.get("media_layout")
        if kind == "audiovisual":
            media_layout = media_layout or "mixed"
            if media_layout not in AUDIOVISUAL_LAYOUTS:
                raise ConfigError(
                    f"collection {collection_id} media_layout must be mixed, movies, or series"
                )
        elif media_layout is not None:
            raise ConfigError("media_layout is limited to audiovisual collections")
        collections.append(
            CollectionConfig(
                collection_id, kind, role, root, tuple(excludes), media_layout
            )
        )
        seen_ids.add(collection_id)

    for index, collection in enumerate(collections):
        if _is_within(report_dir, collection.root):
            raise ConfigError("report_dir must be outside every collection")
        for other in collections[index + 1:]:
            if _is_within(collection.root, other.root) or _is_within(other.root, collection.root):
                raise ConfigError("collection roots must not overlap")
    return LibrarianConfig(
        report_dir, tuple(collections), audiovisual_analysis,
        book_analysis, photo_analysis
    )
