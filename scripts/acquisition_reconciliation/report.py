"""Private acquisition evidence construction and atomic publication."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .config import ReconciliationConfig


SCHEMA = "media-server.acquisition-reconciliation/v1"
_REPORT_NAME_RE = re.compile(
    r"^acquisition-reconciliation-\d{8}T\d{6}\.\d{6}Z\.json$"
)
_ACTIONABLE_CATEGORIES = {
    "radarr_missing",
    "unmonitored_missing",
    "file_missing_after_import",
    "grabbed_without_file_or_queue",
    "stale_no_grab",
    "never_searched",
}
_QUEUE_STATES = {
    "completed",
    "delay",
    "downloading",
    "failed",
    "paused",
    "queued",
    "warning",
}
_HISTORY_EVENTS = {
    "downloadFailed",
    "downloadFolderImported",
    "downloadIgnored",
    "grabbed",
    "movieFileDeleted",
    "movieFileRenamed",
}


@dataclass(frozen=True)
class EvidenceContext:
    now: dt.datetime
    recent_search_hours: int


def _integer(value: object, minimum: int = 0) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if result >= minimum else None


def _private_text(value: object, maximum: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(character if character >= " " else " " for character in value).strip()
    return cleaned[:maximum] or None


def _datetime(value: object) -> dt.datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _timestamp(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _request_key(media: dict[str, Any]) -> tuple[str, int] | None:
    radarr_id = _integer(media.get("externalServiceId"), 1)
    if radarr_id is not None:
        return "radarr", radarr_id
    tmdb_id = _integer(media.get("tmdbId"), 1)
    return ("tmdb", tmdb_id) if tmdb_id is not None else None


def _merge_requests(
    requests_by_instance: dict[str, list[dict[str, Any]]],
) -> tuple[dict[tuple[str, int], dict[str, Any]], dict[str, int]]:
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    malformed: dict[str, int] = {}
    for instance_id, requests in requests_by_instance.items():
        malformed[instance_id] = 0
        for request in requests:
            media = request.get("media")
            if not isinstance(media, dict):
                malformed[instance_id] += 1
                continue
            key = _request_key(media)
            if key is None:
                malformed[instance_id] += 1
                continue
            requested_at = _datetime(request.get("createdAt"))
            entry = merged.setdefault(
                key,
                {
                    "radarr_id": _integer(media.get("externalServiceId"), 1),
                    "tmdb_id": _integer(media.get("tmdbId"), 1),
                    "source_instances": set(),
                    "request_count": 0,
                    "requested_at": requested_at,
                },
            )
            entry["source_instances"].add(instance_id)
            entry["request_count"] += 1
            if requested_at is not None and (
                entry["requested_at"] is None or requested_at < entry["requested_at"]
            ):
                entry["requested_at"] = requested_at
    return merged, malformed


def _radarr_indexes(snapshot: dict[str, Any]) -> tuple[dict[int, dict], dict[int, dict], dict[int, list], dict[int, list]]:
    movies_by_id: dict[int, dict] = {}
    movies_by_tmdb: dict[int, dict] = {}
    queue_by_movie: dict[int, list] = defaultdict(list)
    history_by_movie: dict[int, list] = defaultdict(list)
    for movie in snapshot["movies"]:
        movie_id = _integer(movie.get("id"), 1)
        tmdb_id = _integer(movie.get("tmdbId"), 1)
        if movie_id is not None:
            movies_by_id[movie_id] = movie
        if tmdb_id is not None:
            movies_by_tmdb[tmdb_id] = movie
    for row in snapshot["queue"]:
        movie_id = _integer(row.get("movieId"), 1)
        if movie_id is not None:
            queue_by_movie[movie_id].append(row)
    for row in snapshot["history"]:
        movie_id = _integer(row.get("movieId"), 1)
        if movie_id is not None:
            history_by_movie[movie_id].append(row)
    return movies_by_id, movies_by_tmdb, queue_by_movie, history_by_movie


def _queue_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for row in rows:
        state = row.get("status")
        counts[state if state in _QUEUE_STATES else "unknown"] += 1
    return {"record_count": len(rows), "status_counts": dict(sorted(counts.items()))}


def _history_evidence(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    latest: tuple[dt.datetime, str] | None = None
    for row in rows:
        raw_event = row.get("eventType")
        event = raw_event if raw_event in _HISTORY_EVENTS else "unknown"
        counts[event] += 1
        occurred_at = _datetime(row.get("date"))
        if occurred_at is not None and (latest is None or occurred_at > latest[0]):
            latest = occurred_at, event
    return {
        "record_count": len(rows),
        "event_counts": dict(sorted(counts.items())),
        "latest_event": latest[1] if latest else None,
        "latest_event_at": _timestamp(latest[0]) if latest else None,
    }


def _age_hours(now: dt.datetime, value: dt.datetime | None) -> float | None:
    if value is None:
        return None
    return max(0.0, (now - value).total_seconds() / 3600)


def _category(
    movie: dict[str, Any] | None,
    queue: list[dict[str, Any]],
    history: dict[str, Any],
    requested_at: dt.datetime | None,
    context: EvidenceContext,
) -> str:
    if movie is None:
        return "radarr_missing"
    if movie.get("hasFile") is True:
        return "available"
    if queue:
        return "active_transfer"
    if movie.get("monitored") is not True:
        return "unmonitored_missing"
    events = history["event_counts"]
    if events.get("downloadFolderImported", 0):
        return "file_missing_after_import"
    if events.get("grabbed", 0):
        return "grabbed_without_file_or_queue"
    if movie.get("isAvailable") is False:
        return "not_yet_available"
    last_search = _datetime(movie.get("lastSearchTime"))
    if last_search is not None:
        age = _age_hours(context.now, last_search)
        return (
            "recent_search_no_grab"
            if age is not None and age < context.recent_search_hours
            else "stale_no_grab"
        )
    request_age = _age_hours(context.now, requested_at)
    if request_age is None:
        return "unknown_timing_no_grab"
    return (
        "recent_request_never_searched"
        if request_age < context.recent_search_hours
        else "never_searched"
    )


def _record(
    request: dict[str, Any],
    movie: dict[str, Any] | None,
    queue: list[dict[str, Any]],
    history_rows: list[dict[str, Any]],
    context: EvidenceContext,
) -> dict[str, Any]:
    history = _history_evidence(history_rows)
    category = _category(
        movie,
        queue,
        history,
        request["requested_at"],
        context,
    )
    original_language = movie.get("originalLanguage") if movie else None
    return {
        "category": category,
        "actionable_finding": category in _ACTIONABLE_CATEGORIES,
        "title": _private_text(movie.get("title")) if movie else None,
        "year": _integer(movie.get("year"), 1800) if movie else None,
        "radarr_id": _integer(movie.get("id"), 1) if movie else request["radarr_id"],
        "tmdb_id": _integer(movie.get("tmdbId"), 1) if movie else request["tmdb_id"],
        "source_instances": sorted(request["source_instances"]),
        "source_request_count": request["request_count"],
        "requested_at": _timestamp(request["requested_at"]),
        "monitored": movie.get("monitored") is True if movie else None,
        "radarr_available": movie.get("isAvailable") is True if movie else None,
        "has_file": movie.get("hasFile") is True if movie else None,
        "last_search_at": _timestamp(_datetime(movie.get("lastSearchTime"))) if movie else None,
        "quality_profile_id": _integer(movie.get("qualityProfileId"), 1) if movie else None,
        "original_language": (
            _private_text(original_language.get("name"), 64)
            if isinstance(original_language, dict)
            else None
        ),
        "queue": _queue_evidence(queue),
        "history": history,
    }


def previous_report(report_dir: Path) -> dict[str, str] | None:
    candidates = report_dir.glob("acquisition-reconciliation-*.json") if report_dir.is_dir() else ()
    paths = sorted(
        path
        for path in candidates
        if _REPORT_NAME_RE.fullmatch(path.name) and not path.is_symlink() and path.is_file()
    )
    if not paths:
        return None
    path = paths[-1]
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _build_records(
    config: ReconciliationConfig,
    merged: dict[tuple[str, int], dict[str, Any]],
    radarr_snapshot: dict[str, Any],
    context: EvidenceContext,
) -> list[dict[str, Any]]:
    indexes = _radarr_indexes(radarr_snapshot)
    movies_by_id, movies_by_tmdb, queue_by_movie, history_by_movie = indexes
    records = []
    for request in merged.values():
        movie = movies_by_id.get(request["radarr_id"])
        if movie is None:
            movie = movies_by_tmdb.get(request["tmdb_id"])
        movie_id = _integer(movie.get("id"), 1) if movie else None
        records.append(
            _record(
                request,
                movie,
                queue_by_movie.get(movie_id, []) if movie_id is not None else [],
                history_by_movie.get(movie_id, []) if movie_id is not None else [],
                context,
            )
        )
    records.sort(key=lambda row: (row["category"], row["title"] or "", row["tmdb_id"] or 0))
    return records


def _summary(
    requests_by_instance: dict[str, list[dict[str, Any]]],
    records: list[dict[str, Any]],
    malformed: dict[str, int],
) -> dict[str, Any]:
    category_counts = Counter(row["category"] for row in records)
    approved_count = sum(len(rows) for rows in requests_by_instance.values())
    malformed_count = sum(malformed.values())
    valid_count = max(0, approved_count - malformed_count)
    return {
        "instance_count": len(requests_by_instance),
        "approved_movie_request_count": approved_count,
        "unique_movie_count": len(records),
        "duplicate_request_count": max(0, valid_count - len(records)),
        "malformed_request_count": malformed_count,
        "actionable_finding_count": sum(row["actionable_finding"] for row in records),
        "category_counts": dict(sorted(category_counts.items())),
    }


def _instances(
    config: ReconciliationConfig,
    requests_by_instance: dict[str, list[dict[str, Any]]],
    malformed: dict[str, int],
) -> list[dict[str, Any]]:
    return [
        {
            "id": service.service_id,
            "approved_movie_request_count": len(requests_by_instance.get(service.service_id, [])),
            "malformed_request_count": malformed.get(service.service_id, 0),
        }
        for service in config.jellyseerr
    ]


def build_report(
    config: ReconciliationConfig,
    requests_by_instance: dict[str, list[dict[str, Any]]],
    radarr_snapshot: dict[str, Any],
    generated_at: dt.datetime | None = None,
    previous: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = (generated_at or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    context = EvidenceContext(now=now, recent_search_hours=config.recent_search_hours)
    merged, malformed = _merge_requests(requests_by_instance)
    records = _build_records(config, merged, radarr_snapshot, context)
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "mode": "report-only",
        "config_sha256": config.config_sha256,
        "previous_report": previous,
        "summary": _summary(requests_by_instance, records, malformed),
        "instances": _instances(config, requests_by_instance, malformed),
        "records": records,
        "privacy": {
            "movie_titles_persisted_in_private_report": True,
            "requester_identities_persisted": False,
            "usernames_or_email_addresses_persisted": False,
            "authentication_tokens_persisted": False,
            "download_ids_persisted": False,
            "release_titles_persisted": False,
            "media_paths_persisted": False,
        },
        "authority": {
            "release_availability_queried": False,
            "no_result_means_no_acceptable_release": False,
            "jellyseerr_and_radarr_remain_authoritative": True,
        },
        "mutation": {
            "requests_approved_or_removed": False,
            "searches_or_grabs_started": False,
            "profiles_or_monitoring_changed": False,
            "files_or_queue_entries_changed": False,
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
    destination = report_dir / f"acquisition-reconciliation-{stamp}.json"
    rendered = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(rendered).hexdigest()
    stage: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=report_dir,
            prefix=".acquisition-reconciliation-",
            suffix=".tmp",
            delete=False,
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
