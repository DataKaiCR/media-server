"""Aggregate-only evidence construction and private publication."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
from typing import Any

from .config import EvidenceConfig, TierConfig


SCHEMA = "media-server.seeding-evidence/v1"


def _integer(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, result)


def _known_nonnegative_integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _ratio(value: object) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result >= 0 else 0.0


def _tags(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return {tag.strip() for tag in value.split(",") if tag.strip()}


def _seed_time_bucket(days: float) -> str:
    if days < 3:
        return "under-3-days"
    if days < 14:
        return "3-to-13-days"
    if days < 30:
        return "14-to-29-days"
    if days < 90:
        return "30-to-89-days"
    return "90-days-or-more"


def _ratio_bucket(ratio: float) -> str:
    if ratio < 1:
        return "under-1"
    if ratio < 3:
        return "1-to-under-3"
    if ratio < 5:
        return "3-to-under-5"
    if ratio < 20:
        return "5-to-under-20"
    return "20-or-more"


def _tier_template(tier: TierConfig) -> dict[str, Any]:
    return {
        "id": tier.tier_id,
        "tag": tier.tag,
        "minimum_days": tier.minimum_days,
        "target_ratio": tier.target_ratio,
        "review_after_days": tier.review_after_days,
        "protected": tier.protected,
        "torrent_count": 0,
        "payload_bytes": 0,
        "uploaded_bytes": 0,
        "policy_threshold_met_count": 0,
        "below_time_floor_count": 0,
        "below_ratio_target_count": 0,
        "review_due_count": 0,
    }


def _port_alignment(config: EvidenceConfig, preferences: dict[str, Any]) -> dict[str, Any]:
    path = config.qbittorrent.forwarded_port_file
    if path is None:
        return {"forwarded_port_evidence_configured": False, "client_port_matches": None}
    try:
        forwarded = int(path.read_text(encoding="ascii").strip())
    except (OSError, UnicodeDecodeError, ValueError):
        return {
            "forwarded_port_evidence_configured": True,
            "forwarded_port_state_valid": False,
            "client_port_matches": None,
        }
    valid = 1 <= forwarded <= 65535
    listen = _integer(preferences.get("listen_port"), -1)
    return {
        "forwarded_port_evidence_configured": True,
        "forwarded_port_state_valid": valid,
        "client_port_matches": valid and listen == forwarded,
        "proves_external_connectability": False,
    }


def previous_report(report_dir: Path) -> dict[str, str] | None:
    paths = sorted(report_dir.glob("seeding-evidence-*.json")) if report_dir.is_dir() else []
    if not paths:
        return None
    path = paths[-1]
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _new_aggregate(config: EvidenceConfig) -> dict[str, Any]:
    return {
        "uploaded": 0,
        "downloaded": 0,
        "complete_payload": 0,
        "ratios": [],
        "seed_times": [],
        "states": {},
        "ratio_buckets": {key: 0 for key in (
            "under-1", "1-to-under-3", "3-to-under-5", "5-to-under-20", "20-or-more"
        )},
        "time_buckets": {key: 0 for key in (
            "under-3-days", "3-to-13-days", "14-to-29-days", "30-to-89-days", "90-days-or-more"
        )},
        "unclassified": 0,
        "conflicting": 0,
        "low_swarm": 0,
        "tiers": {tier.tier_id: _tier_template(tier) for tier in config.tiers},
    }


def _record_torrent(
    aggregate: dict[str, Any], torrent: dict[str, Any], policy_tags: dict[str, TierConfig]
) -> None:
    uploaded = _integer(torrent.get("uploaded"))
    downloaded = _integer(torrent.get("downloaded"))
    payload = _integer(torrent.get("total_size") or torrent.get("size"))
    ratio = _ratio(torrent.get("ratio"))
    seed_days = _integer(torrent.get("seeding_time")) / 86400
    aggregate["uploaded"] += uploaded
    aggregate["downloaded"] += downloaded
    aggregate["ratios"].append(ratio)
    aggregate["seed_times"].append(seed_days)
    if _ratio(torrent.get("progress")) >= 1:
        aggregate["complete_payload"] += payload
    state = torrent.get("state") if isinstance(torrent.get("state"), str) else "unknown"
    aggregate["states"][state] = aggregate["states"].get(state, 0) + 1
    aggregate["ratio_buckets"][_ratio_bucket(ratio)] += 1
    aggregate["time_buckets"][_seed_time_bucket(seed_days)] += 1
    known_seeders = _known_nonnegative_integer(torrent.get("num_complete"))
    if known_seeders is not None and known_seeders <= 5:
        aggregate["low_swarm"] += 1

    matches = [policy_tags[tag] for tag in _tags(torrent.get("tags")) if tag in policy_tags]
    if not matches:
        aggregate["unclassified"] += 1
        return
    if len(matches) > 1:
        aggregate["conflicting"] += 1
        return
    tier = matches[0]
    tier_totals = aggregate["tiers"][tier.tier_id]
    tier_totals["torrent_count"] += 1
    tier_totals["payload_bytes"] += payload
    tier_totals["uploaded_bytes"] += uploaded
    time_met = seed_days >= tier.minimum_days
    ratio_met = tier.target_ratio is None or ratio >= tier.target_ratio
    if not time_met:
        tier_totals["below_time_floor_count"] += 1
    if not ratio_met:
        tier_totals["below_ratio_target_count"] += 1
    if time_met and ratio_met and not tier.protected:
        tier_totals["policy_threshold_met_count"] += 1
    if seed_days >= tier.review_after_days and not (time_met and ratio_met):
        tier_totals["review_due_count"] += 1


def _aggregate_torrents(config: EvidenceConfig, torrents: list[object]) -> dict[str, Any]:
    aggregate = _new_aggregate(config)
    policy_tags = {tier.tag: tier for tier in config.tiers}
    for torrent in torrents:
        if isinstance(torrent, dict):
            _record_torrent(aggregate, torrent, policy_tags)
    return aggregate


def _summary(torrent_count: int, aggregate: dict[str, Any], server_state: dict[str, Any]) -> dict[str, Any]:
    current_ratio = (
        aggregate["uploaded"] / aggregate["downloaded"] if aggregate["downloaded"] else None
    )
    alltime_uploaded = _integer(server_state.get("alltime_ul"))
    alltime_downloaded = _integer(server_state.get("alltime_dl"))
    alltime_ratio = alltime_uploaded / alltime_downloaded if alltime_downloaded else None
    return {
        "torrent_count": torrent_count,
        "complete_payload_bytes": aggregate["complete_payload"],
        "current_torrents_uploaded_bytes": aggregate["uploaded"],
        "current_torrents_downloaded_bytes": aggregate["downloaded"],
        "current_torrents_aggregate_ratio": round(current_ratio, 4) if current_ratio is not None else None,
        "client_alltime_uploaded_bytes": alltime_uploaded,
        "client_alltime_downloaded_bytes": alltime_downloaded,
        "client_alltime_ratio": round(alltime_ratio, 4) if alltime_ratio is not None else None,
        "median_torrent_ratio": round(statistics.median(aggregate["ratios"]), 4) if aggregate["ratios"] else None,
        "median_seed_time_days": round(statistics.median(aggregate["seed_times"]), 4) if aggregate["seed_times"] else None,
        "state_counts": dict(sorted(aggregate["states"].items())),
        "ratio_buckets": aggregate["ratio_buckets"],
        "seed_time_buckets": aggregate["time_buckets"],
        "low_swarm_evidence_count": aggregate["low_swarm"],
        "unclassified_torrent_count": aggregate["unclassified"],
        "conflicting_policy_tag_count": aggregate["conflicting"],
    }


def build_report(
    config: EvidenceConfig,
    snapshot: dict[str, Any],
    generated_at: dt.datetime | None = None,
    previous: dict[str, str] | None = None,
) -> dict[str, Any]:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    torrents = snapshot["torrents"]
    preferences = snapshot["preferences"]
    server_state = snapshot["server_state"]
    aggregate = _aggregate_torrents(config, torrents)
    upload_limit = _integer(preferences.get("up_limit"))
    alternative_upload_limit = _integer(preferences.get("alt_up_limit"))
    return {
        "schema": SCHEMA,
        "generated_at": generated_at.astimezone(dt.timezone.utc).isoformat(),
        "mode": "report-only",
        "config_sha256": config.config_sha256,
        "previous_report": previous,
        "client": {
            "name": "qBittorrent",
            "version": snapshot["version"],
            "connection_status": server_state.get("connection_status"),
            "current_upload_rate_bytes_per_second": _integer(server_state.get("up_info_speed")),
            "configured_upload_limit_bytes_per_second": upload_limit or None,
            "configured_alternative_upload_limit_bytes_per_second": alternative_upload_limit or None,
            "alternative_speed_limits_active": bool(server_state.get("use_alt_speed_limits")),
            "port_forwarding": _port_alignment(config, preferences),
        },
        "summary": _summary(len(torrents), aggregate, server_state),
        "tiers": [aggregate["tiers"][tier.tier_id] for tier in config.tiers],
        "privacy": {
            "torrent_names_persisted": False,
            "infohashes_persisted": False,
            "tracker_domains_persisted": False,
            "announce_urls_persisted": False,
            "passkeys_persisted": False,
            "per_torrent_records_persisted": False,
        },
        "authority": {
            "local_client_evidence_is_tracker_proof": False,
            "tracker_profile_remains_authoritative": True,
        },
        "mutation": {
            "torrent_tags_changed": False,
            "share_limits_changed": False,
            "torrents_paused_or_deleted": False,
            "mutation_endpoints_available": False,
        },
        "proposed_actions": [],
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_report(report_dir: Path, document: dict[str, Any]) -> tuple[Path, str]:
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(report_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = report_dir / f"seeding-evidence-{stamp}.json"
    rendered = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(rendered).hexdigest()
    stage: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=report_dir, prefix=".seeding-evidence-", suffix=".tmp", delete=False
        ) as handle:
            stage = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(stage, 0o600)
        os.replace(stage, destination)
        stage = None
        _fsync_directory(report_dir)
        return destination, digest
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
