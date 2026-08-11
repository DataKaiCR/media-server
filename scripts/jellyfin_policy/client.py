"""Bounded loopback-only Jellyfin API client."""

from __future__ import annotations

import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 2_097_152
MAX_API_KEY_BYTES = 512
_USER_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)


class ClientError(RuntimeError):
    """A bounded Jellyfin API operation failed."""


def _api_key(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise ClientError("Jellyfin API key must be a private regular file")
            raw = handle.read(MAX_API_KEY_BYTES + 1)
    except OSError as error:
        raise ClientError("cannot read the private Jellyfin API key") from error
    if len(raw) > MAX_API_KEY_BYTES:
        raise ClientError("Jellyfin API key exceeds the safety limit")
    try:
        token = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise ClientError("Jellyfin API key must be ASCII") from error
    if not 16 <= len(token) <= 256 or not all(char.isalnum() for char in token):
        raise ClientError("Jellyfin API key has an invalid shape")
    return token


def _loopback_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ClientError("Jellyfin URL has an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ClientError("Jellyfin URL must be a loopback HTTP(S) origin")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname
        ).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ClientError("Jellyfin URL must use a loopback host")
    return value.rstrip("/")


def _json_response(response: Any) -> object:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClientError("Jellyfin API response exceeds the safety limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ClientError("Jellyfin API returned invalid JSON") from error


def _user_id(value: object) -> str:
    if not isinstance(value, str) or not _USER_ID_RE.fullmatch(value):
        raise ClientError("Jellyfin returned an invalid user identifier")
    return value


class JellyfinClient:
    def __init__(self, base_url: str, api_key_file: Path, timeout: float = 15) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise ClientError("Jellyfin timeout must be finite and between 0 and 120")
        self.base_url = _loopback_origin(base_url)
        self.token = _api_key(api_key_file)
        self.timeout = timeout

    def _request(
        self, endpoint: str, *, method: str = "GET", payload: object | None = None
    ) -> object | None:
        data = None if payload is None else json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=data,
            headers={
                "X-Emby-Token": self.token,
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if method == "POST" and response.status in {200, 204}:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if raw:
                        raise ClientError("Jellyfin policy update returned a body")
                    return None
                return _json_response(response)
        except (urllib.error.URLError, TimeoutError) as error:
            raise ClientError(
                f"Jellyfin API request failed: {type(error).__name__}"
            ) from error

    def users(self) -> list[object]:
        result = self._request("/Users")
        if not isinstance(result, list):
            raise ClientError("Jellyfin users response has an invalid shape")
        return result

    def virtual_folders(self) -> list[object]:
        result = self._request("/Library/VirtualFolders")
        if not isinstance(result, list):
            raise ClientError("Jellyfin folders response has an invalid shape")
        return result

    def update_policy(self, user_id: object, policy: dict[str, object]) -> None:
        identifier = urllib.parse.quote(_user_id(user_id), safe="")
        self._request(f"/Users/{identifier}/Policy", method="POST", payload=policy)
