"""Strict private configuration for post-import language verification."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath

from .config import (
    ConfigError,
    ServiceConfig,
    _bounded_integer,
    _private_config,
    _report_dir,
    _service,
)


@dataclass(frozen=True)
class PathMapping:
    radarr_prefix: PurePosixPath
    host_root: Path


@dataclass(frozen=True)
class LanguageVerificationConfig:
    report_dir: Path
    page_size: int
    max_requests_per_instance: int
    max_radarr_movies: int
    max_files: int
    parser_timeout_seconds: int
    max_parser_output_bytes: int
    max_parser_memory_bytes: int
    ffprobe_command: Path
    latino_profile_ids: frozenset[int]
    path_mappings: tuple[PathMapping, ...]
    radarr: ServiceConfig
    jellyseerr: tuple[ServiceConfig, ...]
    config_sha256: str


def _absolute_posix_prefix(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value.startswith("/"):
        raise ConfigError("path_mapping.radarr_prefix must be an absolute POSIX path")
    path = PurePosixPath(value)
    if ".." in path.parts or str(path) == "/":
        raise ConfigError("path_mapping.radarr_prefix must be a bounded non-root path")
    return path


def _host_root(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError("path_mapping.host_root must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise ConfigError("path_mapping.host_root must be an existing absolute non-symlink directory")
    return path.resolve()


def _path_mapping(row: object) -> PathMapping:
    if not isinstance(row, dict):
        raise ConfigError("each path_mapping entry must be a TOML table")
    unknown = set(row) - {"radarr_prefix", "host_root"}
    if unknown:
        raise ConfigError(f"unknown path_mapping setting: {sorted(unknown)[0]}")
    return PathMapping(
        radarr_prefix=_absolute_posix_prefix(row.get("radarr_prefix")),
        host_root=_host_root(row.get("host_root")),
    )


def _path_mappings(value: object) -> tuple[PathMapping, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ConfigError("path_mapping must define between one and sixteen mappings")
    mappings = tuple(_path_mapping(row) for row in value)
    prefixes = [str(mapping.radarr_prefix) for mapping in mappings]
    if len(prefixes) != len(set(prefixes)):
        raise ConfigError("path_mapping radarr prefixes must be unique")
    for index, left in enumerate(mappings):
        for right in mappings[index + 1 :]:
            if left.radarr_prefix in right.radarr_prefix.parents or right.radarr_prefix in left.radarr_prefix.parents:
                raise ConfigError("path_mapping radarr prefixes must not overlap")
    return mappings


def _ffprobe_command(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError("ffprobe_command must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigError("ffprobe_command must be an absolute regular non-symlink file")
    if not os.access(path, os.X_OK):
        raise ConfigError("ffprobe_command must be executable")
    return path.resolve()


def _profile_ids(value: object) -> frozenset[int]:
    if not isinstance(value, list) or not value:
        raise ConfigError("latino_profile_ids must be a non-empty integer array")
    result = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 100000:
            raise ConfigError("latino_profile_ids must contain positive bounded integers")
        result.append(item)
    if len(result) != len(set(result)):
        raise ConfigError("latino_profile_ids must be unique")
    return frozenset(result)


def _jellyseerr_services(value: object) -> tuple[ServiceConfig, ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 8:
        raise ConfigError("jellyseerr must define between one and eight instances")
    services = tuple(_service(row, "jellyseerr") for row in value)
    ids = [service.service_id for service in services]
    if len(ids) != len(set(ids)):
        raise ConfigError("jellyseerr instance ids must be unique")
    return services


def _limits(raw: dict) -> dict[str, int]:
    return {
        "page_size": _bounded_integer(raw.get("page_size", 100), "page_size", 1, 1000),
        "max_requests_per_instance": _bounded_integer(
            raw.get("max_requests_per_instance", 5000),
            "max_requests_per_instance",
            1,
            10000,
        ),
        "max_radarr_movies": _bounded_integer(
            raw.get("max_radarr_movies", 10000), "max_radarr_movies", 1, 100000
        ),
        "max_files": _bounded_integer(raw.get("max_files", 5000), "max_files", 1, 10000),
        "parser_timeout_seconds": _bounded_integer(
            raw.get("parser_timeout_seconds", 30), "parser_timeout_seconds", 1, 300
        ),
        "max_parser_output_bytes": _bounded_integer(
            raw.get("max_parser_output_bytes", 2 * 1024 * 1024),
            "max_parser_output_bytes",
            4096,
            16 * 1024 * 1024,
        ),
        "max_parser_memory_bytes": _bounded_integer(
            raw.get("max_parser_memory_bytes", 512 * 1024 * 1024),
            "max_parser_memory_bytes",
            64 * 1024 * 1024,
            4 * 1024 * 1024 * 1024,
        ),
    }


def load_language_config(path: Path) -> LanguageVerificationConfig:
    raw, config_sha256 = _private_config(path)
    allowed = {
        "version",
        "report_dir",
        "page_size",
        "max_requests_per_instance",
        "max_radarr_movies",
        "max_files",
        "parser_timeout_seconds",
        "max_parser_output_bytes",
        "max_parser_memory_bytes",
        "ffprobe_command",
        "latino_profile_ids",
        "path_mapping",
        "radarr",
        "jellyseerr",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown top-level setting: {sorted(unknown)[0]}")
    if raw.get("version") != 1:
        raise ConfigError("configuration version must be 1")
    return LanguageVerificationConfig(
        report_dir=_report_dir(raw.get("report_dir")),
        ffprobe_command=_ffprobe_command(raw.get("ffprobe_command", "/usr/bin/ffprobe")),
        latino_profile_ids=_profile_ids(raw.get("latino_profile_ids")),
        path_mappings=_path_mappings(raw.get("path_mapping")),
        radarr=_service(raw.get("radarr"), "radarr", default_id="radarr"),
        jellyseerr=_jellyseerr_services(raw.get("jellyseerr")),
        config_sha256=config_sha256,
        **_limits(raw),
    )
