"""Map reviewed seeding tiers to qBittorrent's native share limits."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any

from .client import ClientError
from .config import EvidenceConfig, TierConfig
from .policy_client import QBittorrentPolicyClient, ShareLimits


_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_SAFE_GLOBAL_BEHAVIOR = {
    "max_ratio_enabled": False,
    "max_seeding_time_enabled": False,
    "max_inactive_seeding_time_enabled": False,
    "max_ratio_act": 0,
}
_BATCH_SIZE = 256


@dataclass(frozen=True)
class ShareLimitChange:
    torrent_hash: str = field(repr=False)
    tier_id: str
    previous: ShareLimits
    expected: ShareLimits
    threshold_already_met: bool


@dataclass(frozen=True)
class ShareLimitPlan:
    changes: tuple[ShareLimitChange, ...]
    torrent_count: int
    classified_count: int
    unclassified_count: int
    conflicting_count: int
    invalid_record_count: int
    forced_torrent_count: int
    drift_counts: dict[str, int]

    def aggregate(self) -> dict[str, Any]:
        return {
            "torrent_count": self.torrent_count,
            "classified_count": self.classified_count,
            "unclassified_count": self.unclassified_count,
            "conflicting_policy_tag_count": self.conflicting_count,
            "invalid_record_count": self.invalid_record_count,
            "forced_torrent_count": self.forced_torrent_count,
            "share_limit_drift_count": len(self.changes),
            "share_limit_drift_counts": dict(sorted(self.drift_counts.items())),
        }


def _tags(value: object) -> set[str] | None:
    if not isinstance(value, str):
        return None
    return {tag.strip() for tag in value.split(",") if tag.strip()}


def _ratio(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result) or (result not in {-2, -1} and result < 0):
        return None
    return result


def _minutes(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= -2 else None


def _current_limits(torrent: dict[str, Any]) -> ShareLimits | None:
    ratio = _ratio(torrent.get("ratio_limit"))
    seeding = _minutes(torrent.get("seeding_time_limit"))
    inactive = _minutes(torrent.get("inactive_seeding_time_limit"))
    if ratio is None or seeding is None or inactive is None:
        return None
    return ShareLimits(ratio, seeding, inactive)


def _managed_tiers(
    config: EvidenceConfig, native_stop_tiers: tuple[str, ...] | None
) -> dict[str, TierConfig]:
    if native_stop_tiers is None:
        native_stop_tiers = tuple(
            tier.tier_id for tier in config.tiers if tier.native_stop
        )
    if len(native_stop_tiers) != len(set(native_stop_tiers)):
        raise ValueError("native stop tiers must be unique")
    tiers = {tier.tier_id: tier for tier in config.tiers}
    for tier_id in native_stop_tiers:
        tier = tiers.get(tier_id)
        if tier is None:
            raise ValueError("native stop tier is not configured")
        if tier.protected:
            raise ValueError("protected tiers cannot use native stop limits")
    return {tier_id: tiers[tier_id] for tier_id in native_stop_tiers}


def _expected_limits(tier: TierConfig, managed: dict[str, TierConfig]) -> ShareLimits:
    if tier.tier_id not in managed:
        return ShareLimits(-1, -1, -1)
    ratio = tier.target_ratio if tier.target_ratio is not None else -1
    return ShareLimits(ratio, tier.minimum_days * 1440, -1)


def _threshold_met(torrent: dict[str, Any], limits: ShareLimits) -> bool:
    try:
        progress = float(torrent.get("progress", 0))
    except (TypeError, ValueError, OverflowError):
        progress = 0
    if not math.isfinite(progress) or torrent.get("force_start") is True or progress < 1:
        return False
    ratio = _ratio(torrent.get("ratio")) or 0
    seed_seconds = torrent.get("seeding_time", 0)
    if isinstance(seed_seconds, bool):
        seed_seconds = 0
    try:
        seed_minutes = max(0, int(seed_seconds)) / 60
    except (TypeError, ValueError, OverflowError):
        seed_minutes = 0
    return (
        (limits.ratio >= 0 and ratio >= limits.ratio)
        or (limits.seeding_minutes >= 0 and seed_minutes >= limits.seeding_minutes)
    )


def build_share_limit_plan(
    config: EvidenceConfig,
    torrents: list[object],
    *,
    native_stop_tiers: tuple[str, ...] | None = None,
) -> ShareLimitPlan:
    """Build an aggregate-safe reconciliation plan from existing policy tags."""

    tiers_by_tag = {tier.tag: tier for tier in config.tiers}
    managed = _managed_tiers(config, native_stop_tiers)
    changes: list[ShareLimitChange] = []
    drift_counts: dict[str, int] = {}
    classified = unclassified = conflicts = invalid = forced = 0
    for torrent in torrents:
        if not isinstance(torrent, dict):
            invalid += 1
            continue
        tags = _tags(torrent.get("tags"))
        if tags is None:
            invalid += 1
            continue
        matches = [tiers_by_tag[tag] for tag in tags if tag in tiers_by_tag]
        if not matches:
            unclassified += 1
            continue
        if len(matches) > 1:
            conflicts += 1
            continue
        classified += 1
        if torrent.get("force_start") is True:
            forced += 1
        torrent_hash = torrent.get("hash")
        previous = _current_limits(torrent)
        if (
            not isinstance(torrent_hash, str)
            or not _HASH_RE.fullmatch(torrent_hash)
            or previous is None
        ):
            invalid += 1
            continue
        tier = matches[0]
        expected = _expected_limits(tier, managed)
        if previous != expected:
            changes.append(
                ShareLimitChange(
                    torrent_hash.lower(),
                    tier.tier_id,
                    previous,
                    expected,
                    _threshold_met(torrent, expected),
                )
            )
            drift_counts[tier.tier_id] = drift_counts.get(tier.tier_id, 0) + 1
    return ShareLimitPlan(
        tuple(changes),
        len(torrents),
        classified,
        unclassified,
        conflicts,
        invalid,
        forced,
        drift_counts,
    )


def _torrent_rows(client: QBittorrentPolicyClient) -> list[object]:
    torrents = client.get_json("torrents/info")
    if not isinstance(torrents, list):
        raise ClientError("qBittorrent returned an unexpected torrent response shape")
    return torrents


def _global_behavior(client: QBittorrentPolicyClient) -> tuple[bool, dict[str, Any]]:
    preferences = client.get_json("app/preferences")
    if not isinstance(preferences, dict):
        raise ClientError("qBittorrent returned an unexpected preference response shape")
    safe = all(preferences.get(key) == value for key, value in _SAFE_GLOBAL_BEHAVIOR.items())
    return safe, preferences


def _group_changes(
    changes: tuple[ShareLimitChange, ...], *, previous: bool = False
) -> tuple[tuple[tuple[str, ...], ShareLimits], ...]:
    grouped: dict[ShareLimits, list[str]] = {}
    for change in changes:
        limits = change.previous if previous else change.expected
        grouped.setdefault(limits, []).append(change.torrent_hash)
    return tuple(
        (tuple(hashes[index : index + _BATCH_SIZE]), limits)
        for limits, hashes in grouped.items()
        for index in range(0, len(hashes), _BATCH_SIZE)
    )


def _apply_changes(
    client: QBittorrentPolicyClient, changes: tuple[ShareLimitChange, ...]
) -> None:
    attempted: list[ShareLimitChange] = []
    try:
        for batch, limits in _group_changes(changes):
            attempted.extend(
                change for change in changes if change.torrent_hash in set(batch)
            )
            client.set_share_limits(batch, limits)
    except Exception as error:
        for batch, limits in _group_changes(tuple(attempted), previous=True):
            try:
                client.set_share_limits(batch, limits)
            except Exception:
                pass
        raise ClientError("native share-limit mutation failed and was rolled back") from error


def _verification_failures(
    rows: list[object], changes: tuple[ShareLimitChange, ...]
) -> tuple[list[ShareLimitChange], int]:
    by_hash = {
        row.get("hash", "").lower(): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("hash"), str)
    }
    failures: list[ShareLimitChange] = []
    removed_after_stop = 0
    for change in changes:
        row = by_hash.get(change.torrent_hash)
        if row is None and change.threshold_already_met:
            removed_after_stop += 1
        elif row is None or _current_limits(row) != change.expected:
            failures.append(change)
    return failures, removed_after_stop


def enforce_share_limits(
    config: EvidenceConfig,
    *,
    apply: bool,
    native_stop_tiers: tuple[str, ...] | None = None,
    client: QBittorrentPolicyClient | None = None,
) -> dict[str, Any]:
    """Report or reconcile native limits; qBittorrent performs any later stop."""

    client = client or QBittorrentPolicyClient(config.qbittorrent)
    client.authenticate()
    managed_tiers = tuple(_managed_tiers(config, native_stop_tiers))
    plan = build_share_limit_plan(
        config, _torrent_rows(client), native_stop_tiers=managed_tiers
    )
    global_safe, _ = _global_behavior(client)
    outcome = {
        "mode": "apply" if apply else "report-only",
        **plan.aggregate(),
        "native_stop_tiers": sorted(managed_tiers),
        "global_stop_behavior_compliant": global_safe,
        "reconciled_count": 0,
        "removed_after_native_stop_count": 0,
        "verification_failure_count": 0,
        "mutation_performed": False,
        "direct_torrent_state_mutation": False,
        "native_stop_may_follow": bool(plan.changes),
        "torrent_content_mutated": False,
    }
    if not apply:
        outcome["attention_required"] = bool(
            plan.conflicting_count or plan.invalid_record_count or plan.forced_torrent_count
        )
        return outcome
    if not global_safe:
        client.set_safe_global_share_behavior()
        outcome["mutation_performed"] = True
    _apply_changes(client, plan.changes)
    outcome["mutation_performed"] |= bool(plan.changes)
    failures, removed = _verification_failures(_torrent_rows(client), plan.changes)
    _apply_changes(client, tuple(
        ShareLimitChange(
            change.torrent_hash,
            change.tier_id,
            change.expected,
            change.previous,
            False,
        )
        for change in failures
    ))
    final_plan = build_share_limit_plan(
        config, _torrent_rows(client), native_stop_tiers=managed_tiers
    )
    final_safe, _ = _global_behavior(client)
    outcome.update(
        {
            "global_stop_behavior_compliant": final_safe,
            "reconciled_count": len(plan.changes) - len(failures),
            "removed_after_native_stop_count": removed,
            "verification_failure_count": len(failures),
            "remaining_share_limit_drift_count": len(final_plan.changes),
            "remaining_forced_torrent_count": final_plan.forced_torrent_count,
            "attention_required": bool(
                not final_safe
                or final_plan.conflicting_count
                or final_plan.invalid_record_count
                or final_plan.forced_torrent_count
                or failures
            ),
        }
    )
    return outcome
