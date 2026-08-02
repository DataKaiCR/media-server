"""Read-only qBittorrent Web API client."""

from __future__ import annotations

import http.cookiejar
import json
from typing import Any
import urllib.parse
import urllib.request

from .config import QBittorrentConfig


MAX_RESPONSE_BYTES = 32 * 1024 * 1024
READ_ENDPOINTS = {
    "app/preferences",
    "app/version",
    "sync/maindata?rid=0",
    "torrents/info",
}


class ClientError(RuntimeError):
    """A sanitized qBittorrent API failure."""


class QBittorrentClient:
    def __init__(self, config: QBittorrentConfig) -> None:
        self.config = config
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _read(self, request: urllib.request.Request) -> bytes:
        try:
            with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except Exception as error:
            raise ClientError("qBittorrent API request failed") from error
        if len(content) > MAX_RESPONSE_BYTES:
            raise ClientError("qBittorrent API response exceeded the safety limit")
        return content

    def authenticate(self) -> None:
        credentials = self.config.credentials
        if credentials is None:
            return
        body = urllib.parse.urlencode(
            {"username": credentials.username, "password": credentials.password}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{self.config.url}/api/v2/auth/login",
            data=body,
            method="POST",
            headers={"Origin": self.config.url, "Referer": self.config.url},
        )
        if self._read(request).decode("utf-8", errors="replace").strip() != "Ok.":
            raise ClientError("qBittorrent authentication was rejected")

    def get_bytes(self, endpoint: str) -> bytes:
        if endpoint not in READ_ENDPOINTS:
            raise ClientError("qBittorrent endpoint is not in the read-only allowlist")
        request = urllib.request.Request(
            f"{self.config.url}/api/v2/{endpoint}",
            method="GET",
            headers={"Origin": self.config.url, "Referer": self.config.url},
        )
        return self._read(request)

    def get_json(self, endpoint: str) -> Any:
        try:
            return json.loads(self.get_bytes(endpoint))
        except json.JSONDecodeError as error:
            raise ClientError("qBittorrent returned invalid JSON") from error

    def snapshot(self) -> dict[str, Any]:
        self.authenticate()
        try:
            version = self.get_bytes("app/version").decode("ascii").strip()
        except UnicodeDecodeError as error:
            raise ClientError("qBittorrent returned an invalid version") from error
        torrents = self.get_json("torrents/info")
        main_data = self.get_json("sync/maindata?rid=0")
        preferences = self.get_json("app/preferences")
        if not isinstance(torrents, list) or not isinstance(main_data, dict) or not isinstance(preferences, dict):
            raise ClientError("qBittorrent returned an unexpected response shape")
        return {
            "version": version,
            "torrents": torrents,
            "server_state": main_data.get("server_state") or {},
            "preferences": preferences,
        }
