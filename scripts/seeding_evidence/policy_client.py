"""Narrow qBittorrent client for native share-limit policy mutations."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import urllib.parse
import urllib.request

from .client import ClientError
from .config import QBittorrentConfig
from .tag_client import QBittorrentTagClient


_MAX_MINUTES = 525_600


@dataclass(frozen=True)
class ShareLimits:
    ratio: float
    seeding_minutes: int
    inactive_minutes: int = -1


class QBittorrentPolicyClient(QBittorrentTagClient):
    """Set only native share limits and the safe global Stop action."""

    def __init__(self, config: QBittorrentConfig) -> None:
        super().__init__(config)

    def _post_policy_form(self, endpoint: str, fields: dict[str, str]) -> None:
        if endpoint not in {"app/setPreferences", "torrents/setShareLimits"}:
            raise ClientError("qBittorrent endpoint is not in the policy-mutation allowlist")
        request = urllib.request.Request(
            f"{self.config.url}/api/v2/{endpoint}",
            data=urllib.parse.urlencode(fields).encode("ascii"),
            method="POST",
            headers={"Origin": self.config.url, "Referer": self.config.url},
        )
        self._read(request)

    @staticmethod
    def _validated_limits(limits: ShareLimits) -> ShareLimits:
        ratio = limits.ratio
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not math.isfinite(float(ratio))
            or (ratio not in {-2, -1} and not 0 < ratio <= 1000)
        ):
            raise ClientError("invalid ratio share limit")
        for value in (limits.seeding_minutes, limits.inactive_minutes):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ClientError("invalid time share limit")
            if value not in {-2, -1} and not 1 <= value <= _MAX_MINUTES:
                raise ClientError("invalid time share limit")
        return limits

    def set_share_limits(self, hashes: tuple[str, ...], limits: ShareLimits) -> None:
        limits = self._validated_limits(limits)
        self._post_policy_form(
            "torrents/setShareLimits",
            {
                "hashes": self._validated_hashes(hashes),
                "ratioLimit": str(limits.ratio),
                "seedingTimeLimit": str(limits.seeding_minutes),
                "inactiveSeedingTimeLimit": str(limits.inactive_minutes),
            },
        )

    def set_safe_global_share_behavior(self) -> None:
        settings = {
            "max_ratio_enabled": False,
            "max_seeding_time_enabled": False,
            "max_inactive_seeding_time_enabled": False,
            "max_ratio_act": 0,
        }
        self._post_policy_form(
            "app/setPreferences",
            {"json": json.dumps(settings, separators=(",", ":"))},
        )
