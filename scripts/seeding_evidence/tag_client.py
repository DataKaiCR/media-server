"""Narrow qBittorrent client for approved policy-tag mutations only."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request

from .client import ClientError, QBittorrentClient
from .config import QBittorrentConfig, TAG_RE


_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_MUTATION_ENDPOINTS = {
    "torrents/addTags",
    "torrents/createTags",
    "torrents/removeTags",
}
_MAX_HASHES_PER_REQUEST = 256


class QBittorrentTagClient(QBittorrentClient):
    """Expose only create/add/remove tag operations, never torrent deletion."""

    def __init__(self, config: QBittorrentConfig) -> None:
        super().__init__(config)

    def _post_form(self, endpoint: str, fields: dict[str, str]) -> None:
        if endpoint not in _MUTATION_ENDPOINTS:
            raise ClientError("qBittorrent endpoint is not in the tag-mutation allowlist")
        body = urllib.parse.urlencode(fields).encode("ascii")
        request = urllib.request.Request(
            f"{self.config.url}/api/v2/{endpoint}",
            data=body,
            method="POST",
            headers={"Origin": self.config.url, "Referer": self.config.url},
        )
        self._read(request)

    @staticmethod
    def _validated_tag(tag: str) -> str:
        if not TAG_RE.fullmatch(tag):
            raise ClientError("invalid policy tag")
        return tag

    @staticmethod
    def _validated_hashes(hashes: tuple[str, ...]) -> str:
        if not hashes or len(hashes) > _MAX_HASHES_PER_REQUEST:
            raise ClientError("invalid tag-mutation batch size")
        if any(not _HASH_RE.fullmatch(value) for value in hashes):
            raise ClientError("invalid torrent identifier")
        return "|".join(hashes)

    def create_tags(self, tags: tuple[str, ...]) -> None:
        if not tags:
            return
        unique = tuple(sorted(set(tags)))
        if len(unique) != len(tags) or any(not TAG_RE.fullmatch(tag) for tag in unique):
            raise ClientError("invalid policy tag set")
        self._post_form("torrents/createTags", {"tags": ",".join(unique)})

    def add_tag(self, hashes: tuple[str, ...], tag: str) -> None:
        self._post_form(
            "torrents/addTags",
            {
                "hashes": self._validated_hashes(hashes),
                "tags": self._validated_tag(tag),
            },
        )

    def remove_tag(self, hashes: tuple[str, ...], tag: str) -> None:
        self._post_form(
            "torrents/removeTags",
            {
                "hashes": self._validated_hashes(hashes),
                "tags": self._validated_tag(tag),
            },
        )
