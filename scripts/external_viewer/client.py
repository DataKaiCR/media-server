"""Bounded loopback API clients for external-viewer onboarding."""

from __future__ import annotations

import http.cookiejar
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

from jellyfin_policy.client import JellyfinClient, _RejectRedirect

from .config import _jellyseerr_origin


MAX_RESPONSE_BYTES = 2_097_152
MAX_SECRET_BYTES = 512
_USER_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)


class OnboardingClientError(RuntimeError):
    """An onboarding API operation failed within a bounded interface."""


def _private_ascii_secret(path: Path, label: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                raise OnboardingClientError(
                    f"{label} must be a private regular file"
                )
            raw = handle.read(MAX_SECRET_BYTES + 1)
    except OnboardingClientError:
        raise
    except OSError as error:
        raise OnboardingClientError(f"cannot read private {label}") from error
    if len(raw) > MAX_SECRET_BYTES:
        raise OnboardingClientError(f"{label} exceeds the safety limit")
    try:
        value = raw.decode("ascii").strip()
    except UnicodeDecodeError as error:
        raise OnboardingClientError(f"{label} must be ASCII") from error
    if (
        not 16 <= len(value) <= 256
        or not all(33 <= ord(character) <= 126 for character in value)
    ):
        raise OnboardingClientError(f"{label} has an invalid shape")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or not _USER_ID_RE.fullmatch(value):
        raise OnboardingClientError(f"API returned an invalid {label}")
    return value


def _json_body(response: Any) -> object:
    raw = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise OnboardingClientError("API response exceeds the safety limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise OnboardingClientError("API returned invalid JSON") from error


class ExternalJellyfinClient(JellyfinClient):
    """Jellyfin policy client plus narrowly scoped account lifecycle calls."""

    _authorization = (
        'MediaBrowser Client="ExternalViewerOnboarding", '
        'Device="LoopbackMaintenance", '
        'DeviceId="external-viewer-onboarding", Version="1.0"'
    )

    def _account_request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: object | None = None,
        token: str | None = None,
        authenticated: bool = True,
        expect_empty: bool = False,
    ) -> object | None:
        data = None if payload is None else json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": self._authorization,
        }
        if authenticated:
            headers["X-Emby-Token"] = self.token if token is None else token
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            opener = urllib.request.build_opener(_RejectRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                if expect_empty:
                    raw = response.read(MAX_RESPONSE_BYTES + 1)
                    if raw:
                        raise OnboardingClientError(
                            "Jellyfin lifecycle operation returned a body"
                        )
                    return None
                return _json_body(response)
        except urllib.error.HTTPError as error:
            error.read(MAX_RESPONSE_BYTES + 1)
            raise OnboardingClientError(
                f"Jellyfin lifecycle operation failed with HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise OnboardingClientError(
                f"Jellyfin lifecycle request failed: {type(error).__name__}"
            ) from error

    def create_user(self, username: str, password: str) -> str:
        result = self._account_request(
            "/Users/New",
            method="POST",
            payload={"Name": username, "Password": password},
        )
        if not isinstance(result, dict):
            raise OnboardingClientError("Jellyfin create-user response is invalid")
        identifier = _identifier(result.get("Id"), "Jellyfin user identifier")
        if result.get("Policy", {}).get("IsAdministrator") is not False:
            raise OnboardingClientError("Jellyfin created an unexpectedly privileged user")
        return identifier

    def delete_user(self, user_id: object) -> None:
        identifier = urllib.parse.quote(
            _identifier(user_id, "Jellyfin user identifier"), safe=""
        )
        self._account_request(
            f"/Users/{identifier}", method="DELETE", expect_empty=True
        )

    def authenticate_user(self, username: str, password: str) -> tuple[str, str]:
        result = self._account_request(
            "/Users/AuthenticateByName",
            method="POST",
            payload={"Username": username, "Pw": password},
        )
        if not isinstance(result, dict):
            raise OnboardingClientError("Jellyfin authentication response is invalid")
        token = result.get("AccessToken")
        if not isinstance(token, str) or not 16 <= len(token) <= 512:
            raise OnboardingClientError("Jellyfin returned an invalid viewer token")
        identifier = _identifier(
            result.get("User", {}).get("Id"), "Jellyfin user identifier"
        )
        return identifier, token

    def user_views(self, user_id: object, token: str) -> list[object]:
        identifier = urllib.parse.quote(
            _identifier(user_id, "Jellyfin user identifier"), safe=""
        )
        result = self._account_request(
            f"/Users/{identifier}/Views", token=token
        )
        if not isinstance(result, dict) or not isinstance(result.get("Items"), list):
            raise OnboardingClientError("Jellyfin views response is invalid")
        return result["Items"]

    def logout(self, token: str) -> None:
        self._account_request(
            "/Sessions/Logout",
            method="POST",
            token=token,
            expect_empty=True,
        )

    def public_users(self) -> list[object]:
        result = self._account_request("/Users/Public", authenticated=False)
        if not isinstance(result, list):
            raise OnboardingClientError("Jellyfin public-users response is invalid")
        return result


class JellyseerrClient:
    """Request-only Jellyseerr account import through a loopback API."""

    def __init__(
        self, base_url: str, api_key_file: Path, timeout: float = 30
    ) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 120
        ):
            raise OnboardingClientError(
                "Jellyseerr timeout must be finite and between 0 and 120"
            )
        self.base_url = _jellyseerr_origin(base_url)
        self.api_key = _private_ascii_secret(api_key_file, "Jellyseerr API key")
        self.timeout = timeout

    def _request(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: object | None = None,
        authenticated: bool = True,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> tuple[int, object | None]:
        data = None if payload is None else json.dumps(
            payload, separators=(",", ":")
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers["X-Api-Key"] = self.api_key
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=data,
            headers=headers,
            method=method,
        )
        client = opener or urllib.request.build_opener(_RejectRedirect())
        try:
            with client.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise OnboardingClientError(
                        "Jellyseerr API response exceeds the safety limit"
                    )
                if not raw:
                    return response.status, None
                try:
                    return response.status, json.loads(raw)
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise OnboardingClientError(
                        "Jellyseerr API returned invalid JSON"
                    ) from error
        except urllib.error.HTTPError as error:
            error.read(MAX_RESPONSE_BYTES + 1)
            raise OnboardingClientError(
                f"Jellyseerr API operation failed with HTTP {error.code}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as error:
            raise OnboardingClientError(
                f"Jellyseerr API request failed: {type(error).__name__}"
            ) from error

    def assert_safe_defaults(self) -> None:
        _, result = self._request("/settings/main")
        if (
            not isinstance(result, dict)
            or result.get("defaultPermissions") != 32
            or result.get("newPlexLogin") is not False
        ):
            raise OnboardingClientError(
                "Jellyseerr request-only provisioning defaults have drifted"
            )

    def users(self) -> list[dict[str, object]]:
        _, result = self._request("/user?take=100&skip=0")
        if not isinstance(result, dict) or not isinstance(result.get("results"), list):
            raise OnboardingClientError("Jellyseerr users response is invalid")
        if not all(isinstance(row, dict) for row in result["results"]):
            raise OnboardingClientError("Jellyseerr returned a malformed user")
        return result["results"]

    def available_jellyfin_users(self) -> list[dict[str, object]]:
        _, result = self._request("/settings/jellyfin/users")
        if not isinstance(result, list) or not all(
            isinstance(row, dict) for row in result
        ):
            raise OnboardingClientError(
                "Jellyseerr Jellyfin-users response is invalid"
            )
        return result

    def import_jellyfin_user(self, jellyfin_user_id: str) -> int:
        identifier = _identifier(jellyfin_user_id, "Jellyfin import identifier")
        status, result = self._request(
            "/user/import-from-jellyfin",
            method="POST",
            payload={"jellyfinUserIds": [identifier]},
        )
        if (
            status != 201
            or not isinstance(result, list)
            or len(result) != 1
            or not isinstance(result[0], dict)
            or not isinstance(result[0].get("id"), int)
            or result[0].get("permissions") != 32
        ):
            raise OnboardingClientError("Jellyseerr user import is unsafe")
        return int(result[0]["id"])

    def delete_user(self, user_id: int) -> None:
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise OnboardingClientError("invalid Jellyseerr user identifier")
        self._request(f"/user/{user_id}", method="DELETE")

    def authenticate_user(self, username: str, password: str) -> int:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            _RejectRedirect(), urllib.request.HTTPCookieProcessor(jar)
        )
        status, result = self._request(
            "/auth/jellyfin",
            method="POST",
            payload={"username": username, "password": password},
            authenticated=False,
            opener=opener,
        )
        if status != 200 or not isinstance(result, dict):
            raise OnboardingClientError("Jellyseerr viewer authentication failed")
        permission = result.get("permissions")
        if isinstance(permission, bool) or not isinstance(permission, int):
            raise OnboardingClientError(
                "Jellyseerr authentication returned invalid permissions"
            )
        return permission
