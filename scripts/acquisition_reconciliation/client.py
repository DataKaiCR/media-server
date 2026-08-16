"""Bounded read-only Jellyseerr and Radarr API clients."""

from __future__ import annotations

import json
from typing import Any
import urllib.parse
import urllib.request

from .config import ReconciliationConfig, ServiceConfig
from .errors import ClientError


MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class ReadOnlyApiClient:
    """GET-only client whose subclasses expose a fixed endpoint allowlist."""

    endpoints: frozenset[str] = frozenset()
    api_version = ""

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.opener = urllib.request.build_opener()

    def get_json(self, endpoint: str, query: dict[str, object] | None = None) -> Any:
        if endpoint not in self.endpoints:
            raise ClientError(
                f"{self.config.service_id} endpoint is not in the read-only allowlist"
            )
        url = f"{self.config.url}/api/{self.api_version}/{endpoint}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "X-Api-Key": self.config.api_key},
        )
        try:
            with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
                content = response.read(MAX_RESPONSE_BYTES + 1)
        except Exception as error:
            raise ClientError(f"{self.config.service_id} API request failed") from error
        if len(content) > MAX_RESPONSE_BYTES:
            raise ClientError(
                f"{self.config.service_id} API response exceeded the safety limit"
            )
        try:
            return json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ClientError(f"{self.config.service_id} returned invalid JSON") from error


class JellyseerrClient(ReadOnlyApiClient):
    endpoints = frozenset({"request"})
    api_version = "v1"

    def approved_movie_requests(self, maximum: int, page_size: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        total: int | None = None
        while total is None or len(rows) < total:
            payload = self.get_json(
                "request",
                {
                    "take": min(page_size, maximum),
                    "skip": len(rows),
                    "sort": "added",
                    "filter": "all",
                },
            )
            if not isinstance(payload, dict):
                raise ClientError(f"{self.config.service_id} returned an unexpected response shape")
            page_info = payload.get("pageInfo")
            results = payload.get("results")
            if not isinstance(page_info, dict) or not isinstance(results, list):
                raise ClientError(f"{self.config.service_id} returned an unexpected response shape")
            reported_total = page_info.get("results")
            if (
                isinstance(reported_total, bool)
                or not isinstance(reported_total, int)
                or reported_total < 0
            ):
                raise ClientError(f"{self.config.service_id} returned an invalid result count")
            if reported_total > maximum:
                raise ClientError(f"{self.config.service_id} exceeded the configured record limit")
            total = reported_total
            if not all(isinstance(row, dict) for row in results):
                raise ClientError(f"{self.config.service_id} returned an unexpected request shape")
            if not results and len(rows) < total:
                raise ClientError(f"{self.config.service_id} pagination stopped before completion")
            rows.extend(results)
            if len(rows) > maximum:
                raise ClientError(f"{self.config.service_id} exceeded the configured record limit")
        return [
            row
            for row in rows[: total or 0]
            if row.get("status") == 2
            and row.get("type") == "movie"
            and isinstance(row.get("media"), dict)
            and row["media"].get("mediaType") == "movie"
        ]


class RadarrClient(ReadOnlyApiClient):
    endpoints = frozenset({"movie", "queue", "history"})
    api_version = "v3"

    def movies(self, maximum: int) -> list[dict[str, Any]]:
        payload = self.get_json("movie")
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise ClientError("radarr returned an unexpected movie response shape")
        if len(payload) > maximum:
            raise ClientError("radarr exceeded the configured movie limit")
        return payload

    def _paged_records(
        self,
        endpoint: str,
        maximum: int,
        page_size: int,
        extra: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        total: int | None = None
        while total is None or len(rows) < total:
            query: dict[str, object] = {
                "page": page,
                "pageSize": min(page_size, maximum),
            }
            if extra:
                query.update(extra)
            payload = self.get_json(endpoint, query)
            if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
                raise ClientError(f"radarr returned an unexpected {endpoint} response shape")
            reported_total = payload.get("totalRecords")
            records = payload["records"]
            if (
                isinstance(reported_total, bool)
                or not isinstance(reported_total, int)
                or reported_total < 0
            ):
                raise ClientError(f"radarr returned an invalid {endpoint} result count")
            if reported_total > maximum:
                raise ClientError(f"radarr exceeded the configured {endpoint} record limit")
            if not all(isinstance(row, dict) for row in records):
                raise ClientError(f"radarr returned an unexpected {endpoint} record shape")
            total = reported_total
            if not records and len(rows) < total:
                raise ClientError(f"radarr {endpoint} pagination stopped before completion")
            rows.extend(records)
            if len(rows) > maximum:
                raise ClientError(f"radarr exceeded the configured {endpoint} record limit")
            page += 1
        return rows[: total or 0]

    def snapshot(self, config: ReconciliationConfig) -> dict[str, Any]:
        return {
            "movies": self.movies(config.max_radarr_movies),
            "queue": self._paged_records(
                "queue",
                config.max_queue_records,
                config.page_size,
                {"includeMovie": "true"},
            ),
            "history": self._paged_records(
                "history",
                config.max_history_records,
                config.page_size,
                {"sortKey": "date", "sortDirection": "descending"},
            ),
        }
