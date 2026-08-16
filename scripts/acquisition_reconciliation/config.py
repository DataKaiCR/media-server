"""Strict private configuration for acquisition reconciliation audits."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import ipaddress
from pathlib import Path
import re
import stat
import tomllib
from urllib.parse import urlsplit


ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
API_KEY_RE = re.compile(r"^[!-~]{1,4096}$")


class ConfigError(ValueError):
    """The acquisition reconciliation configuration is invalid or unsafe."""


def _inside_git_worktree(path: Path) -> bool:
    start = path if path.is_dir() else path.parent
    return any((parent / ".git").exists() for parent in (start, *start.parents))


@dataclass(frozen=True)
class ServiceConfig:
    service_id: str
    url: str
    api_key: str
    timeout_seconds: int


@dataclass(frozen=True)
class ReconciliationConfig:
    report_dir: Path
    recent_search_hours: int
    page_size: int
    max_requests_per_instance: int
    max_radarr_movies: int
    max_queue_records: int
    max_history_records: int
    radarr: ServiceConfig
    jellyseerr: tuple[ServiceConfig, ...]
    config_sha256: str


def _private_regular_file(path: Path, field: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"{field} must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"{field} must be an existing regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError(f"{field} must be mode 0600 or more restrictive")
    resolved = path.resolve()
    if _inside_git_worktree(resolved):
        raise ConfigError(f"{field} must remain outside Git worktrees")
    return resolved


def _private_config(path: Path) -> tuple[dict, str]:
    path = _private_regular_file(path, "configuration file")
    raw_bytes = path.read_bytes()
    try:
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("cannot parse acquisition reconciliation configuration") from error
    return document, hashlib.sha256(raw_bytes).hexdigest()


def _report_dir(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError("report_dir must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ConfigError("report_dir must be an absolute non-symlink path")
    if path.exists() and not path.is_dir():
        raise ConfigError("report_dir must be a directory when it exists")
    resolved = path.resolve()
    if _inside_git_worktree(resolved):
        raise ConfigError("report_dir must remain outside Git worktrees")
    return resolved


def _loopback_url(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a non-empty URL")
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ConfigError(f"{field} contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"{field} must be an HTTP(S) origin without credentials, path, query, or fragment"
        )
    hostname = parsed.hostname
    is_loopback = hostname == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ConfigError(f"{field} must use a loopback host")
    return value.rstrip("/")


def _api_key(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field} must be a path string")
    path = _private_regular_file(Path(value), field)
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"cannot parse {field}") from error
    if set(document) != {"api_key"}:
        raise ConfigError(f"{field} must contain only api_key")
    key = document.get("api_key")
    if not isinstance(key, str) or not API_KEY_RE.fullmatch(key):
        raise ConfigError(f"{field} api_key must be a bounded printable string")
    return key


def _bounded_integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{field} must be between {minimum} and {maximum}")
    return value


def _service(row: object, field: str, default_id: str | None = None) -> ServiceConfig:
    if not isinstance(row, dict):
        raise ConfigError(f"{field} must be a TOML table")
    allowed = {"url", "api_key_file", "timeout_seconds"}
    if default_id is None:
        allowed.add("id")
    unknown = set(row) - allowed
    if unknown:
        raise ConfigError(f"unknown {field} setting: {sorted(unknown)[0]}")
    service_id = default_id if default_id is not None else row.get("id")
    if not isinstance(service_id, str) or not ID_RE.fullmatch(service_id):
        raise ConfigError(f"{field}.id must be a lowercase public-safe identifier")
    timeout = _bounded_integer(
        row.get("timeout_seconds", 20), f"{field}.timeout_seconds", 1, 120
    )
    return ServiceConfig(
        service_id=service_id,
        url=_loopback_url(row.get("url"), f"{field}.url"),
        api_key=_api_key(row.get("api_key_file"), f"{field}.api_key_file"),
        timeout_seconds=timeout,
    )


def _limits(raw: dict) -> dict[str, int]:
    return {
        "recent_search_hours": _bounded_integer(
            raw.get("recent_search_hours", 72), "recent_search_hours", 1, 8760
        ),
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
        "max_queue_records": _bounded_integer(
            raw.get("max_queue_records", 10000), "max_queue_records", 1, 100000
        ),
        "max_history_records": _bounded_integer(
            raw.get("max_history_records", 50000),
            "max_history_records",
            1,
            250000,
        ),
    }


def load_config(path: Path) -> ReconciliationConfig:
    raw, config_sha256 = _private_config(path)
    allowed = {
        "version",
        "report_dir",
        "recent_search_hours",
        "page_size",
        "max_requests_per_instance",
        "max_radarr_movies",
        "max_queue_records",
        "max_history_records",
        "radarr",
        "jellyseerr",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown top-level setting: {sorted(unknown)[0]}")
    if raw.get("version") != 1:
        raise ConfigError("configuration version must be 1")

    rows = raw.get("jellyseerr")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 8:
        raise ConfigError("jellyseerr must define between one and eight instances")
    jellyseerr = tuple(_service(row, "jellyseerr") for row in rows)
    ids = [service.service_id for service in jellyseerr]
    if len(ids) != len(set(ids)):
        raise ConfigError("jellyseerr instance ids must be unique")

    return ReconciliationConfig(
        report_dir=_report_dir(raw.get("report_dir")),
        radarr=_service(raw.get("radarr"), "radarr", default_id="radarr"),
        jellyseerr=jellyseerr,
        config_sha256=config_sha256,
        **_limits(raw),
    )
