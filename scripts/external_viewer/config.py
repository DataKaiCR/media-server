"""Strict private configuration for isolated external-viewer onboarding."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tomllib
import unicodedata
import urllib.parse


_MAX_PRIVATE_FILE_BYTES = 65_536
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,64}$")
_BASE_PATH_RE = re.compile(r"^/[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*$")


class OnboardingConfigError(ValueError):
    """Private onboarding input is invalid or unsafe."""


@dataclass(frozen=True)
class ViewerCredential:
    username: str
    password: str


@dataclass(frozen=True)
class OnboardingConfig:
    policy_config_file: Path
    credential_file: Path
    jellyseerr_base_url: str
    jellyseerr_api_key_file: Path
    state_dir: Path


def _inside_git_worktree(path: Path) -> bool:
    start = path if path.is_dir() else path.parent
    return any((parent / ".git").exists() for parent in (start, *start.parents))


def _absolute_path(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value:
        raise OnboardingConfigError(f"{label} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute() or path.is_symlink():
        raise OnboardingConfigError(f"{label} must be an absolute non-symlink path")
    return path


def _read_private_file(path: Path, label: str) -> bytes:
    if not path.is_absolute() or path.is_symlink():
        raise OnboardingConfigError(
            f"{label} must be an absolute regular non-symlink file"
        )
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise OnboardingConfigError(
                    f"{label} must be mode 0600 or stricter"
                )
            raw = handle.read(_MAX_PRIVATE_FILE_BYTES + 1)
    except OnboardingConfigError:
        raise
    except OSError as error:
        raise OnboardingConfigError(f"cannot read {label}") from error
    if len(raw) > _MAX_PRIVATE_FILE_BYTES:
        raise OnboardingConfigError(f"{label} exceeds the safety limit")
    return raw


def _private_file_path(value: object, label: str) -> Path:
    path = _absolute_path(value, label)
    _read_private_file(path, label)
    return path


def _private_state_dir(value: object) -> Path:
    path = _absolute_path(value, "state_dir")
    try:
        metadata = path.stat()
    except OSError as error:
        raise OnboardingConfigError("state_dir is unavailable") from error
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OnboardingConfigError("state_dir must be a mode-0700 private directory")
    return path


def _safe_base_path(value: str) -> bool:
    return bool(
        _BASE_PATH_RE.fullmatch(value)
        and all(segment not in {".", ".."} for segment in value.split("/"))
    )


def _jellyseerr_origin(value: object) -> str:
    if not isinstance(value, str):
        raise OnboardingConfigError(
            "jellyseerr_base_url must be a loopback HTTP(S) API origin"
        )
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise OnboardingConfigError("jellyseerr_base_url has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not _safe_base_path(parsed.path.rstrip("/"))
        or parsed.path.rstrip("/") != "/api/v1"
    ):
        raise OnboardingConfigError(
            "jellyseerr_base_url must be a loopback HTTP(S) /api/v1 origin"
        )
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname
        ).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise OnboardingConfigError("jellyseerr_base_url must use a loopback host")
    return value.rstrip("/")


def _document(path: Path) -> dict[str, object]:
    raw = _read_private_file(path, "onboarding configuration")
    try:
        document = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise OnboardingConfigError("cannot parse onboarding configuration") from error
    return document


def load_onboarding_config(path: Path) -> OnboardingConfig:
    document = _document(path)
    allowed = {
        "version",
        "jellyfin_policy_config",
        "credential_file",
        "jellyseerr_base_url",
        "jellyseerr_api_key_file",
        "state_dir",
    }
    if set(document) != allowed:
        raise OnboardingConfigError(
            "onboarding configuration must contain exactly the documented settings"
        )
    if document.get("version") != 1:
        raise OnboardingConfigError("onboarding configuration version must be 1")
    config = OnboardingConfig(
        policy_config_file=_private_file_path(
            document.get("jellyfin_policy_config"), "jellyfin_policy_config"
        ),
        credential_file=_private_file_path(
            document.get("credential_file"), "credential_file"
        ),
        jellyseerr_base_url=_jellyseerr_origin(
            document.get("jellyseerr_base_url")
        ),
        jellyseerr_api_key_file=_private_file_path(
            document.get("jellyseerr_api_key_file"), "jellyseerr_api_key_file"
        ),
        state_dir=_private_state_dir(document.get("state_dir")),
    )
    private_paths = (
        path,
        config.policy_config_file,
        config.credential_file,
        config.jellyseerr_api_key_file,
        config.state_dir,
    )
    if any(_inside_git_worktree(item) for item in private_paths):
        raise OnboardingConfigError("private onboarding state must remain outside Git")
    return config


def load_viewer_credential(path: Path) -> ViewerCredential:
    raw = _read_private_file(path, "credential_file")
    try:
        document = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OnboardingConfigError("cannot parse credential_file") from error
    if not isinstance(document, dict) or set(document) != {"username", "password"}:
        raise OnboardingConfigError(
            "credential_file must contain exactly username and password"
        )
    username = document.get("username")
    password = document.get("password")
    if not isinstance(username, str) or not _NAME_RE.fullmatch(username):
        raise OnboardingConfigError("viewer username must be bounded and contain no controls")
    if (
        not isinstance(password, str)
        or not 12 <= len(password) <= 256
        or any(unicodedata.category(character).startswith("C") for character in password)
    ):
        raise OnboardingConfigError(
            "viewer password must be bounded and contain no control characters"
        )
    return ViewerCredential(username, password)
