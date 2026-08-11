"""Strict private configuration for Jellyfin viewer policy enforcement."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import os
from pathlib import Path
import re
import stat
import tomllib
import urllib.parse


_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
_ROLES = {"guest", "household", "restricted"}


class ConfigError(ValueError):
    """The policy configuration is invalid or unsafe."""


@dataclass(frozen=True)
class UserRule:
    name: str
    role: str


@dataclass(frozen=True)
class PolicyConfig:
    base_url: str
    api_key_file: Path
    backup_dir: Path
    users: tuple[UserRule, ...]


def _private_regular_file(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ConfigError(f"{label} must be an absolute regular non-symlink file")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ConfigError(f"{label} must be mode 0600 or stricter")
    return path


def _loopback_origin(value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError("base_url must be a loopback HTTP(S) origin")
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ConfigError("base_url has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("base_url must be a loopback HTTP(S) origin")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname
        ).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ConfigError("base_url must use a loopback host")
    return value.rstrip("/")


def _inside_git_worktree(path: Path) -> bool:
    start = path if path.is_dir() else path.parent
    return any((parent / ".git").exists() for parent in (start, *start.parents))


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise ConfigError(f"{label} must be an absolute non-symlink path")
    return path


def _user_rules(value: object) -> tuple[UserRule, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("users must contain at least one account rule")
    rules: list[UserRule] = []
    seen: set[str] = set()
    for row in value:
        if not isinstance(row, dict) or set(row) != {"name", "role"}:
            raise ConfigError("each user rule must contain only name and role")
        name = row.get("name")
        role = row.get("role")
        if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
            raise ConfigError("user names must be bounded and contain no controls")
        if role not in _ROLES:
            raise ConfigError("user roles must be guest, household, or restricted")
        normalized = name.casefold()
        if normalized in seen:
            raise ConfigError("user rules must be unique case-insensitively")
        seen.add(normalized)
        rules.append(UserRule(name, str(role)))
    return tuple(rules)


def load_config(path: Path) -> PolicyConfig:
    path = _private_regular_file(path, "configuration")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("cannot read policy configuration") from error
    allowed = {"version", "base_url", "api_key_file", "backup_dir", "users"}
    if set(raw) - allowed:
        raise ConfigError("configuration contains an unknown top-level setting")
    if raw.get("version") != 1:
        raise ConfigError("configuration version must be 1")
    api_key_file = _private_regular_file(
        _absolute_path(raw.get("api_key_file"), "api_key_file"), "api_key_file"
    )
    backup_dir = _absolute_path(raw.get("backup_dir"), "backup_dir")
    if any(_inside_git_worktree(item) for item in (path, api_key_file, backup_dir)):
        raise ConfigError("private policy state must remain outside Git worktrees")
    return PolicyConfig(
        base_url=_loopback_origin(raw.get("base_url")),
        api_key_file=api_key_file,
        backup_dir=backup_dir,
        users=_user_rules(raw.get("users")),
    )
