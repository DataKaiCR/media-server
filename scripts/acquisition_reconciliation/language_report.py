"""Private post-import language evidence and atomic publication."""

from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from .language_config import LanguageVerificationConfig
from .language_probe import LANGUAGE_ALIASES, map_media_path, probe_audio


SCHEMA = "media-server.post-import-language-verification/v1"
_REPORT_NAME_RE = re.compile(r"^language-verification-\d{8}T\d{6}\.\d{6}Z\.json$")
_VERIFIED_CATEGORIES = {"original_verified", "latino_verified"}


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


def _request_key(media: dict[str, Any]) -> tuple[str, int] | None:
    radarr_id = _integer(media.get("externalServiceId"), 1)
    if radarr_id is not None:
        return "radarr", radarr_id
    tmdb_id = _integer(media.get("tmdbId"), 1)
    return ("tmdb", tmdb_id) if tmdb_id is not None else None


def _requested_movies(
    requests_by_instance: dict[str, list[dict[str, Any]]],
) -> dict[tuple[str, int], dict[str, Any]]:
    merged: dict[tuple[str, int], dict[str, Any]] = {}
    for instance_id, rows in requests_by_instance.items():
        for row in rows:
            media = row.get("media")
            if not isinstance(media, dict):
                continue
            key = _request_key(media)
            if key is None:
                continue
            entry = merged.setdefault(
                key,
                {
                    "radarr_id": _integer(media.get("externalServiceId"), 1),
                    "tmdb_id": _integer(media.get("tmdbId"), 1),
                    "source_instances": set(),
                    "request_count": 0,
                },
            )
            entry["source_instances"].add(instance_id)
            entry["request_count"] += 1
    return merged


def _movie_indexes(movies: list[dict[str, Any]]) -> tuple[dict[int, dict], dict[int, dict]]:
    by_id = {}
    by_tmdb = {}
    for movie in movies:
        movie_id = _integer(movie.get("id"), 1)
        tmdb_id = _integer(movie.get("tmdbId"), 1)
        if movie_id is not None:
            by_id[movie_id] = movie
        if tmdb_id is not None:
            by_tmdb[tmdb_id] = movie
    return by_id, by_tmdb


def _matches_original(tag: str | None, expected: str | None) -> bool:
    if tag is None or expected not in LANGUAGE_ALIASES:
        return False
    aliases = LANGUAGE_ALIASES[expected]
    if tag in aliases:
        return True
    return any(tag.startswith(alias + "-") for alias in aliases if len(alias) == 2)


def _language_result(
    probe: dict[str, Any], expected_original: str | None, latino_required: bool
) -> tuple[str, dict[str, Any]]:
    if probe["status"] != "ok":
        return f"probe_{probe['status']}", {
            "original_language_present": False,
            "generic_spanish_present": False,
            "unclassified_audio_present": False,
            "latino_verified": False,
        }
    streams = probe["audio_streams"]
    original_present = any(
        _matches_original(stream.get("language_tag"), expected_original) for stream in streams
    )
    spanish_present = any(stream.get("canonical_language") == "Spanish" for stream in streams)
    unclassified_present = any(stream.get("canonical_language") is None for stream in streams)
    latino_verified = any(
        stream.get("explicit_latino_marker") is True
        and stream.get("explicit_castilian_marker") is not True
        and stream.get("canonical_language") in {"Spanish", None}
        for stream in streams
    )
    evidence = {
        "original_language_present": original_present,
        "generic_spanish_present": spanish_present,
        "unclassified_audio_present": unclassified_present,
        "latino_verified": latino_verified,
    }
    if latino_required:
        if latino_verified:
            return "latino_verified", evidence
        if spanish_present:
            return "generic_spanish_unverified", evidence
        return ("latino_unverified" if unclassified_present else "latino_missing"), evidence
    if expected_original not in LANGUAGE_ALIASES:
        return "unsupported_original_language", evidence
    if original_present:
        return "original_verified", evidence
    return ("original_unverified" if unclassified_present else "original_missing"), evidence


def _file_evidence(path: Path) -> tuple[os.stat_result, dict[str, int]]:
    state = path.stat(follow_symlinks=False)
    return state, {
        "size_bytes": state.st_size,
        "mtime_ns": state.st_mtime_ns,
        "device": state.st_dev,
        "inode": state.st_ino,
    }


def _record_base(
    config: LanguageVerificationConfig,
    request: dict[str, Any],
    movie: dict[str, Any],
) -> tuple[dict[str, Any], str | None, bool, dict[str, Any] | None]:
    movie_file = movie.get("movieFile")
    original = movie.get("originalLanguage")
    expected = _private_text(original.get("name"), 64) if isinstance(original, dict) else None
    profile_id = _integer(movie.get("qualityProfileId"), 1)
    latino_required = profile_id in config.latino_profile_ids
    base = {
        "title": _private_text(movie.get("title")),
        "year": _integer(movie.get("year"), 1800),
        "radarr_id": _integer(movie.get("id"), 1),
        "tmdb_id": _integer(movie.get("tmdbId"), 1),
        "movie_file_id": (
            _integer(movie_file.get("id"), 1) if isinstance(movie_file, dict) else None
        ),
        "source_instances": sorted(request["source_instances"]),
        "source_request_count": request["request_count"],
        "quality_profile_id": profile_id,
        "policy": "latino" if latino_required else "original",
        "expected_original_language": expected,
    }
    return base, expected, latino_required, movie_file if isinstance(movie_file, dict) else None


def _probe_file(
    config: LanguageVerificationConfig, movie_file: dict[str, Any] | None
) -> tuple[str | None, dict[str, int] | None, dict[str, Any] | None]:
    try:
        path = map_media_path(
            movie_file.get("path") if movie_file is not None else None,
            config.path_mappings,
        )
        before, file_evidence = _file_evidence(path)
    except (OSError, ValueError):
        return "file_unavailable", None, None
    probe = probe_audio(path, config)
    try:
        after = path.stat(follow_symlinks=False)
    except OSError:
        return "source_changed", file_evidence, None
    identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity != after_identity:
        return "source_changed", file_evidence, None
    return None, file_evidence, probe


def _verify_movie(
    config: LanguageVerificationConfig,
    request: dict[str, Any],
    movie: dict[str, Any],
) -> dict[str, Any]:
    base, expected, latino_required, movie_file = _record_base(config, request, movie)
    error, file_evidence, probe = _probe_file(config, movie_file)
    if error is not None or probe is None:
        return {**base, "category": error, "verified": False, "file": file_evidence}
    category, language = _language_result(probe, expected, latino_required)
    return {
        **base,
        "category": category,
        "verified": category in _VERIFIED_CATEGORIES,
        "file": file_evidence,
        "probe_status": probe["status"],
        "audio_stream_count": probe["audio_stream_count"],
        "audio_streams": probe["audio_streams"],
        "language_evidence": language,
    }


def _records(
    config: LanguageVerificationConfig,
    requests: dict[tuple[str, int], dict[str, Any]],
    movies: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    by_id, by_tmdb = _movie_indexes(movies)
    records = []
    skipped = Counter()
    for request in requests.values():
        movie = by_id.get(request["radarr_id"]) or by_tmdb.get(request["tmdb_id"])
        if movie is None:
            skipped["radarr_missing"] += 1
            continue
        if movie.get("hasFile") is not True or not isinstance(movie.get("movieFile"), dict):
            skipped["not_imported"] += 1
            continue
        records.append(_verify_movie(config, request, movie))
        if len(records) > config.max_files:
            raise RuntimeError("imported request files exceeded the configured verification limit")
    records.sort(key=lambda row: (row["category"], row["title"] or "", row["tmdb_id"] or 0))
    return records, dict(sorted(skipped.items()))


def previous_language_report(report_dir: Path) -> dict[str, str] | None:
    candidates = report_dir.glob("language-verification-*.json") if report_dir.is_dir() else ()
    paths = sorted(
        path
        for path in candidates
        if _REPORT_NAME_RE.fullmatch(path.name) and not path.is_symlink() and path.is_file()
    )
    if not paths:
        return None
    path = paths[-1]
    return {"name": path.name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def build_language_report(
    config: LanguageVerificationConfig,
    requests_by_instance: dict[str, list[dict[str, Any]]],
    movies: list[dict[str, Any]],
    generated_at: dt.datetime | None = None,
    previous: dict[str, str] | None = None,
) -> dict[str, Any]:
    now = (generated_at or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    requests = _requested_movies(requests_by_instance)
    records, skipped = _records(config, requests, movies)
    categories = Counter(record["category"] for record in records)
    return {
        "schema": SCHEMA,
        "generated_at": now.isoformat(),
        "mode": "report-only",
        "config_sha256": config.config_sha256,
        "previous_report": previous,
        "summary": {
            "approved_movie_request_count": sum(len(rows) for rows in requests_by_instance.values()),
            "unique_requested_movie_count": len(requests),
            "verified_file_count": sum(record["verified"] for record in records),
            "review_finding_count": sum(not record["verified"] for record in records),
            "category_counts": dict(sorted(categories.items())),
            "skipped_counts": skipped,
        },
        "records": records,
        "privacy": {
            "movie_titles_persisted_in_private_report": True,
            "media_paths_persisted": False,
            "raw_stream_titles_persisted": False,
            "dialogue_or_subtitle_text_persisted": False,
            "requester_identities_persisted": False,
            "authentication_tokens_persisted": False,
        },
        "authority": {
            "release_titles_are_language_proof": False,
            "generic_spanish_is_latino_proof": False,
            "explicit_stream_metadata_required_for_latino": True,
            "ffprobe_evidence_is_audio_content_identification": False,
        },
        "mutation": {
            "media_files_changed": False,
            "files_deleted_or_replaced": False,
            "profiles_or_requests_changed": False,
            "searches_or_grabs_started": False,
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


def publish_language_report(
    report_dir: Path, document: dict[str, Any]
) -> tuple[Path, str]:
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(report_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = report_dir / f"language-verification-{stamp}.json"
    rendered = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    digest = hashlib.sha256(rendered).hexdigest()
    stage: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=report_dir, prefix=".language-verification-", suffix=".tmp", delete=False
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
