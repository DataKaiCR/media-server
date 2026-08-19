"""Conservative, privacy-preserving default policy-tag classification."""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from .client import ClientError
from .config import EvidenceConfig, TierConfig
from .tag_client import QBittorrentTagClient


_HASH_RE = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")
_METADATA_STATES = {"forcedMetaDL", "metaDL"}
_BATCH_SIZE = 256


@dataclass(frozen=True)
class Assignment:
    """An in-memory assignment; the secret identifier must never be rendered."""

    torrent_hash: str = field(repr=False)
    tier_id: str
    tag: str


@dataclass(frozen=True)
class ClassificationPlan:
    assignments: tuple[Assignment, ...]
    torrent_count: int
    already_classified_count: int
    conflicting_policy_tag_count: int
    unclassified_count: int
    deferred_metadata_count: int
    invalid_record_count: int
    assignment_counts: dict[str, int]

    def aggregate(self) -> dict[str, Any]:
        return {
            "torrent_count": self.torrent_count,
            "already_classified_count": self.already_classified_count,
            "conflicting_policy_tag_count": self.conflicting_policy_tag_count,
            "unclassified_before_count": self.unclassified_count,
            "deferred_metadata_count": self.deferred_metadata_count,
            "invalid_record_count": self.invalid_record_count,
            "eligible_assignment_count": len(self.assignments),
            "assignment_counts": dict(sorted(self.assignment_counts.items())),
        }


def _tags(value: object) -> set[str] | None:
    if not isinstance(value, str):
        return None
    return {tag.strip() for tag in value.split(",") if tag.strip()}


def _positive_size(torrent: dict[str, Any]) -> bool:
    for key in ("total_size", "size"):
        value = torrent.get(key)
        if isinstance(value, bool):
            continue
        try:
            if int(value) > 0:
                return True
        except (TypeError, ValueError, OverflowError):
            continue
    return False


def _tier_map(config: EvidenceConfig) -> dict[str, TierConfig]:
    return {tier.tier_id: tier for tier in config.tiers}


def build_classification_plan(
    config: EvidenceConfig,
    torrents: list[object],
    *,
    public_default_tier: str = "standard",
    private_default_tier: str = "contributor",
) -> ClassificationPlan:
    """Plan baseline tags without retaining names, trackers, or identifiers."""

    tiers = _tier_map(config)
    if public_default_tier not in tiers:
        raise ValueError("public default tier is not configured")
    if private_default_tier not in tiers:
        raise ValueError("private default tier is not configured")
    policy_tags = {tier.tag for tier in config.tiers}
    assignments: list[Assignment] = []
    assignment_counts = {public_default_tier: 0, private_default_tier: 0}
    classified = conflicts = unclassified = deferred = invalid = 0

    for torrent in torrents:
        if not isinstance(torrent, dict):
            invalid += 1
            continue
        tags = _tags(torrent.get("tags"))
        if tags is None:
            invalid += 1
            continue
        matches = tags & policy_tags
        if len(matches) == 1:
            classified += 1
            continue
        if len(matches) > 1:
            conflicts += 1
            continue
        unclassified += 1

        state = torrent.get("state")
        private = torrent.get("private")
        torrent_hash = torrent.get("hash")
        if state in _METADATA_STATES or not _positive_size(torrent) or private is None:
            deferred += 1
            continue
        if not isinstance(state, str) or not isinstance(private, bool):
            invalid += 1
            continue
        if not isinstance(torrent_hash, str) or not _HASH_RE.fullmatch(torrent_hash):
            invalid += 1
            continue

        tier_id = private_default_tier if private else public_default_tier
        tier = tiers[tier_id]
        assignments.append(Assignment(torrent_hash.lower(), tier_id, tier.tag))
        assignment_counts[tier_id] = assignment_counts.get(tier_id, 0) + 1

    return ClassificationPlan(
        assignments=tuple(assignments),
        torrent_count=len(torrents),
        already_classified_count=classified,
        conflicting_policy_tag_count=conflicts,
        unclassified_count=unclassified,
        deferred_metadata_count=deferred,
        invalid_record_count=invalid,
        assignment_counts=assignment_counts,
    )


def _torrent_rows(client: QBittorrentTagClient) -> list[object]:
    torrents = client.get_json("torrents/info")
    if not isinstance(torrents, list):
        raise ClientError("qBittorrent returned an unexpected torrent response shape")
    return torrents


def _chunks(values: tuple[str, ...]) -> tuple[tuple[str, ...], ...]:
    return tuple(
        values[index : index + _BATCH_SIZE]
        for index in range(0, len(values), _BATCH_SIZE)
    )


def _new_outcome(plan: ClassificationPlan, apply: bool) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "report-only",
        **plan.aggregate(),
        "applied_count": 0,
        "applied_counts": {},
        "created_policy_tag_count": 0,
        "verification_failure_count": 0,
        "mutation_performed": False,
        "torrent_content_mutated": False,
        "share_limits_mutated": False,
    }


def _ensure_required_tags(
    client: QBittorrentTagClient, assignments: tuple[Assignment, ...]
) -> int:
    configured_tags = client.get_json("torrents/tags")
    if not isinstance(configured_tags, list) or not all(
        isinstance(tag, str) for tag in configured_tags
    ):
        raise ClientError("qBittorrent returned an unexpected tag response shape")
    required = tuple(sorted({assignment.tag for assignment in assignments}))
    missing = tuple(tag for tag in required if tag not in configured_tags)
    client.create_tags(missing)
    return len(missing)


def _assignment_batches(
    assignments: tuple[Assignment, ...]
) -> tuple[tuple[tuple[str, ...], str], ...]:
    grouped: dict[tuple[str, str], list[str]] = {}
    for assignment in assignments:
        grouped.setdefault((assignment.tier_id, assignment.tag), []).append(
            assignment.torrent_hash
        )
    return tuple(
        (batch, tag)
        for (_, tag), hashes in sorted(grouped.items())
        for batch in _chunks(tuple(hashes))
    )


def _apply_batches(
    client: QBittorrentTagClient,
    batches: tuple[tuple[tuple[str, ...], str], ...],
) -> None:
    attempted: list[tuple[tuple[str, ...], str]] = []
    try:
        for batch, tag in batches:
            # A transport failure can happen after qBittorrent commits the tag.
            # Record the attempted batch first so rollback covers that ambiguity.
            attempted.append((batch, tag))
            client.add_tag(batch, tag)
    except Exception as error:
        for batch, tag in reversed(attempted):
            try:
                client.remove_tag(batch, tag)
            except Exception:
                pass
        raise ClientError(
            "qBittorrent policy-tag mutation failed and was rolled back"
        ) from error


def _verify_assignments(
    config: EvidenceConfig,
    client: QBittorrentTagClient,
    assignments: tuple[Assignment, ...],
) -> tuple[list[Assignment], dict[str, int]]:
    by_hash = {
        row.get("hash", "").lower(): row
        for row in _torrent_rows(client)
        if isinstance(row, dict) and isinstance(row.get("hash"), str)
    }
    policy_tags = {tier.tag for tier in config.tiers}
    failures: list[Assignment] = []
    counts: dict[str, int] = {}
    for assignment in assignments:
        row = by_hash.get(assignment.torrent_hash)
        row_tags = _tags(row.get("tags")) if row else None
        matches = row_tags & policy_tags if row_tags is not None else set()
        if row is None or matches != {assignment.tag}:
            failures.append(assignment)
        else:
            counts[assignment.tier_id] = counts.get(assignment.tier_id, 0) + 1
    return failures, counts


def _rollback_assignments(
    client: QBittorrentTagClient, failures: list[Assignment]
) -> None:
    grouped: dict[str, list[str]] = {}
    for assignment in failures:
        grouped.setdefault(assignment.tag, []).append(assignment.torrent_hash)
    for tag, hashes in grouped.items():
        for batch in _chunks(tuple(hashes)):
            client.remove_tag(batch, tag)


def _finish_without_apply(
    outcome: dict[str, Any], plan: ClassificationPlan
) -> dict[str, Any]:
    outcome["remaining_unclassified_count"] = plan.unclassified_count
    outcome["remaining_conflicting_policy_tag_count"] = (
        plan.conflicting_policy_tag_count
    )
    outcome["attention_required"] = bool(
        plan.conflicting_policy_tag_count or plan.invalid_record_count
    )
    return outcome


def enforce_classification(
    config: EvidenceConfig,
    *,
    apply: bool,
    public_default_tier: str = "standard",
    private_default_tier: str = "contributor",
    client: QBittorrentTagClient | None = None,
) -> dict[str, Any]:
    """Report or apply safe defaults, returning aggregate-only evidence."""

    client = client or QBittorrentTagClient(config.qbittorrent)
    client.authenticate()
    plan = build_classification_plan(
        config,
        _torrent_rows(client),
        public_default_tier=public_default_tier,
        private_default_tier=private_default_tier,
    )
    outcome = _new_outcome(plan, apply)
    if not apply or not plan.assignments:
        return _finish_without_apply(outcome, plan)

    created = _ensure_required_tags(client, plan.assignments)
    batches = _assignment_batches(plan.assignments)
    _apply_batches(client, batches)
    failures, applied_counts = _verify_assignments(config, client, plan.assignments)
    _rollback_assignments(client, failures)
    final_plan = build_classification_plan(
        config,
        _torrent_rows(client),
        public_default_tier=public_default_tier,
        private_default_tier=private_default_tier,
    )
    outcome.update(
        {
            "applied_count": len(plan.assignments) - len(failures),
            "applied_counts": dict(sorted(applied_counts.items())),
            "created_policy_tag_count": created,
            "verification_failure_count": len(failures),
            "mutation_performed": bool(created or batches),
            "remaining_unclassified_count": final_plan.unclassified_count,
            "remaining_conflicting_policy_tag_count": (
                final_plan.conflicting_policy_tag_count
            ),
            "attention_required": bool(
                final_plan.conflicting_policy_tag_count
                or final_plan.invalid_record_count
                or failures
            ),
        }
    )
    return outcome
