"""Strict private configuration for seeding evidence audits."""

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
TAG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")


class ConfigError(ValueError):
    """The seeding evidence configuration is invalid or unsafe."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


@dataclass(frozen=True)
class QBittorrentConfig:
    url: str
    credentials: Credentials | None
    timeout_seconds: int
    forwarded_port_file: Path | None


@dataclass(frozen=True)
class TierConfig:
    tier_id: str
    tag: str
    minimum_days: int
    target_ratio: float | None
    review_after_days: int
    protected: bool


@dataclass(frozen=True)
class EvidenceConfig:
    report_dir: Path
    qbittorrent: QBittorrentConfig
    tiers: tuple[TierConfig, ...]
    config_sha256: str


def _private_regular_file(path: Path, field: str) -> Path:
    if not path.is_absolute():
        raise ConfigError(f"{field} must be absolute")
    if path.is_symlink() or not path.is_file():
        raise ConfigError(f"{field} must be an existing regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError(f"{field} must be mode 0600 or more restrictive")
    return path.resolve()


def _private_config(path: Path) -> tuple[dict, str]:
    path = _private_regular_file(path, "configuration file")
    raw_bytes = path.read_bytes()
    try:
        document = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("cannot parse seeding evidence configuration") from error
    return document, hashlib.sha256(raw_bytes).hexdigest()


def _report_dir(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError("report_dir must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ConfigError("report_dir must be an absolute non-symlink path")
    if path.exists() and not path.is_dir():
        raise ConfigError("report_dir must be a directory when it exists")
    return path.resolve()


def _loopback_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError("qbittorrent.url must be a non-empty URL")
    parsed = urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ConfigError("qbittorrent.url contains an invalid port") from error
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
            "qbittorrent.url must be an HTTP(S) origin without credentials, "
            "path, query, or fragment"
        )
    hostname = parsed.hostname
    is_loopback = hostname == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            is_loopback = False
    if not is_loopback:
        raise ConfigError("qbittorrent.url must use a loopback host")
    return value.rstrip("/")


def _credentials(value: object) -> Credentials | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError("qbittorrent.credential_file must be a path string")
    path = _private_regular_file(Path(value), "qbittorrent.credential_file")
    try:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("cannot parse qBittorrent credential file") from error
    if set(document) != {"username", "password"}:
        raise ConfigError("qBittorrent credential file must contain only username and password")
    username = document.get("username")
    password = document.get("password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise ConfigError("qBittorrent username and password must be non-empty strings")
    return Credentials(username, password)


def _bounded_integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigError(f"{field} must be between {minimum} and {maximum}")
    return value


def _tier(row: object) -> TierConfig:
    if not isinstance(row, dict):
        raise ConfigError("each tiers entry must be a TOML table")
    allowed = {"id", "tag", "minimum_days", "target_ratio", "review_after_days", "protected"}
    unknown = set(row) - allowed
    if unknown:
        raise ConfigError(f"unknown tier setting: {sorted(unknown)[0]}")
    tier_id = row.get("id")
    tag = row.get("tag")
    if not isinstance(tier_id, str) or not ID_RE.fullmatch(tier_id):
        raise ConfigError("tier id must be a lowercase public-safe identifier")
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise ConfigError("tier tag must be a lowercase public-safe qBittorrent tag")
    minimum_days = _bounded_integer(row.get("minimum_days"), "tier.minimum_days", 1, 3650)
    review_after_days = _bounded_integer(
        row.get("review_after_days"), "tier.review_after_days", minimum_days, 3650
    )
    target = row.get("target_ratio")
    if target is not None:
        if isinstance(target, bool) or not isinstance(target, (int, float)) or not 0 < target <= 1000:
            raise ConfigError("tier.target_ratio must be greater than zero and at most 1000")
        target = float(target)
    protected = row.get("protected", False)
    if not isinstance(protected, bool):
        raise ConfigError("tier.protected must be true or false")
    if protected and target is not None:
        raise ConfigError("protected tiers must not define a cleanup ratio target")
    return TierConfig(tier_id, tag, minimum_days, target, review_after_days, protected)


def load_config(path: Path) -> EvidenceConfig:
    raw, config_sha256 = _private_config(path)
    allowed = {"version", "report_dir", "qbittorrent", "tiers"}
    unknown = set(raw) - allowed
    if unknown:
        raise ConfigError(f"unknown top-level setting: {sorted(unknown)[0]}")
    if raw.get("version") != 1:
        raise ConfigError("configuration version must be 1")

    qbittorrent = raw.get("qbittorrent")
    if not isinstance(qbittorrent, dict):
        raise ConfigError("qbittorrent must be a TOML table")
    qb_allowed = {"url", "credential_file", "timeout_seconds", "forwarded_port_file"}
    qb_unknown = set(qbittorrent) - qb_allowed
    if qb_unknown:
        raise ConfigError(f"unknown qbittorrent setting: {sorted(qb_unknown)[0]}")
    timeout = _bounded_integer(qbittorrent.get("timeout_seconds", 20), "qbittorrent.timeout_seconds", 1, 120)
    forwarded = qbittorrent.get("forwarded_port_file")
    forwarded_path = None
    if forwarded is not None:
        if not isinstance(forwarded, str) or not forwarded:
            raise ConfigError("qbittorrent.forwarded_port_file must be a path string")
        candidate = Path(forwarded)
        if not candidate.is_absolute() or candidate.is_symlink() or not candidate.is_file():
            raise ConfigError("qbittorrent.forwarded_port_file must be an existing regular non-symlink file")
        forwarded_path = candidate.resolve()

    rows = raw.get("tiers")
    if not isinstance(rows, list) or not rows:
        raise ConfigError("at least one seeding tier is required")
    tiers = tuple(_tier(row) for row in rows)
    ids = [tier.tier_id for tier in tiers]
    tags = [tier.tag for tier in tiers]
    if len(ids) != len(set(ids)):
        raise ConfigError("tier ids must be unique")
    if len(tags) != len(set(tags)):
        raise ConfigError("tier tags must be unique")

    return EvidenceConfig(
        report_dir=_report_dir(raw.get("report_dir")),
        qbittorrent=QBittorrentConfig(
            url=_loopback_url(qbittorrent.get("url")),
            credentials=_credentials(qbittorrent.get("credential_file")),
            timeout_seconds=timeout,
            forwarded_port_file=forwarded_path,
        ),
        tiers=tiers,
        config_sha256=config_sha256,
    )
