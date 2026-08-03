"""Conservative audiovisual layout, relationship, and redundancy evidence."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import PurePosixPath
import re
from typing import Any

from .audiovisual import (
    ARTWORK_EXTENSIONS,
    MEDIA_METADATA_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
)
from .config import AudiovisualAnalysisConfig, CollectionConfig
from .model import CollectionReport, FileRecord, Finding


_EXTRA_COMPONENTS = {
    "behind the scenes", "deleted scenes", "extras", "featurettes",
    "interviews", "samples", "shorts", "trailers",
}
_GENERIC_ARTWORK_RE = re.compile(
    r"^(?:banner|clearart|clearlogo|disc|fanart|folder|landscape|logo|poster|thumb|tvshow)"
    r"(?:[-_. ]?\d+)?$",
    re.IGNORECASE,
)
_SEASON_ARTWORK_RE = re.compile(r"^season[-_. ]?\d{1,3}(?:[-_. ](?:banner|fanart|poster|thumb))?$", re.IGNORECASE)
_EPISODE_RE = re.compile(r"(?<![a-z0-9])s(?P<season>\d{1,3})e(?P<episodes>\d{1,4}(?:e\d{1,4})*)", re.IGNORECASE)
_LANGUAGE_ALIASES = {
    "ea": "es-419", "en": "en", "eng": "en", "english": "en",
    "es": "es", "es-419": "es-419", "es-la": "es-419", "es-mx": "es-mx",
    "spa": "es", "spanish": "es", "pt": "pt", "pt-br": "pt-br",
}
_EDITION_MARKERS = {
    "alternate cut": "alternate-cut",
    "black and chrome": "black-and-chrome",
    "director's cut": "directors-cut",
    "directors cut": "directors-cut",
    "extended": "extended",
    "final cut": "final-cut",
    "noir edition": "noir-edition",
    "special edition": "special-edition",
    "theatrical": "theatrical-cut",
    "unrated": "unrated",
}
_SUBTITLE_DIRECTORIES = {"subs", "subtitle", "subtitles"}


def _path(record: FileRecord) -> PurePosixPath:
    return PurePosixPath(record.relative_path)


def _is_extra(record: FileRecord) -> bool:
    path = _path(record)
    components = {component.casefold() for component in path.parts[:-1]}
    stem = path.stem.casefold()
    return bool(components & _EXTRA_COMPONENTS) or stem in {"sample", "trailer"} or stem.endswith("-sample")


def _media_metadata(record: FileRecord) -> dict[str, Any]:
    audiovisual = record.metadata.get("audiovisual")
    if not isinstance(audiovisual, dict):
        return {}
    media = audiovisual.get("media")
    return media if isinstance(media, dict) else {}


def _subtitle_metadata(record: FileRecord) -> dict[str, Any]:
    audiovisual = record.metadata.get("audiovisual")
    if not isinstance(audiovisual, dict):
        return {}
    subtitle = audiovisual.get("subtitle")
    return subtitle if isinstance(subtitle, dict) else {}


def _scope(record: FileRecord, layout: str) -> str:
    path = _path(record)
    if layout in {"movies", "series"}:
        return path.parts[0].casefold() if len(path.parts) > 1 else "."
    return path.parent.as_posix().casefold()


def _episode_keys(record: FileRecord) -> tuple[str, ...]:
    match = _EPISODE_RE.search(_path(record).stem)
    if not match:
        return ()
    season = int(match.group("season"))
    episodes = [int(value) for value in match.group("episodes").casefold().split("e")]
    return tuple(f"s{season:03d}e{episode:04d}" for episode in episodes)


def _codec_resolution_evidence(records: list[FileRecord]) -> dict[str, Any]:
    codecs: set[str] = set()
    resolutions: set[str] = set()
    for record in records:
        streams = _media_metadata(record).get("streams")
        if not isinstance(streams, list):
            continue
        for stream in streams:
            if not isinstance(stream, dict) or stream.get("type") != "video":
                continue
            if isinstance(stream.get("codec"), str):
                codecs.add(stream["codec"])
            width, height = stream.get("width"), stream.get("height")
            if isinstance(width, int) and isinstance(height, int):
                resolutions.add(f"{width}x{height}")
    return {
        "count": len(records),
        "total_bytes": sum(record.size for record in records),
        "distinct_video_codecs": sorted(codecs),
        "distinct_resolutions": sorted(resolutions),
        "automatic_delete": False,
    }


def _edition_evidence(records: list[FileRecord]) -> list[str]:
    markers: set[str] = set()
    for record in records:
        stem = _path(record).stem.casefold()
        markers.update(label for phrase, label in _EDITION_MARKERS.items() if phrase in stem)
    return sorted(markers)


def _append_redundancy(report: CollectionReport, records: list[FileRecord]) -> None:
    if len(records) < 2:
        return
    hashes = {record.sha256 for record in records if record.sha256}
    if len(hashes) == 1 and all(record.sha256 for record in records):
        return
    edition_markers = _edition_evidence(records)
    evidence = _codec_resolution_evidence(records)
    if edition_markers:
        evidence.update({
            "edition_markers": edition_markers,
            "automatic_collapse": False,
        })
        report.findings.append(
            Finding(
                "distinct-edition-group", "info",
                "Multiple primary videos carry explicit alternate edition or cut evidence",
                related_paths=tuple(sorted(record.relative_path for record in records)),
                evidence=evidence,
            )
        )
        return
    report.findings.append(
        Finding(
            "possible-redundant-encode-group", "warning",
            "Multiple primary video candidates may represent redundant encodes; manual review is required",
            related_paths=tuple(sorted(record.relative_path for record in records)),
            evidence=evidence,
        )
    )


def add_redundant_encode_groups(
    report: CollectionReport, config: CollectionConfig
) -> None:
    videos = [
        record for record in report.files
        if record.extension in VIDEO_EXTENSIONS and not _is_extra(record)
    ]
    layout = config.media_layout or "mixed"
    if layout == "movies":
        groups: dict[str, list[FileRecord]] = defaultdict(list)
        for record in videos:
            groups[_path(record).parent.as_posix().casefold()].append(record)
        for records in groups.values():
            _append_redundancy(report, records)
        return

    episode_groups: dict[tuple[str, str], list[FileRecord]] = defaultdict(list)
    unkeyed: dict[str, list[FileRecord]] = defaultdict(list)
    for record in videos:
        keys = _episode_keys(record)
        if keys:
            show = _path(record).parts[0].casefold()
            for key in keys:
                episode_groups[(show, key)].append(record)
        elif layout == "mixed":
            unkeyed[_path(record).parent.as_posix().casefold()].append(record)
    for records in [*episode_groups.values(), *unkeyed.values()]:
        _append_redundancy(report, records)


def add_layout_findings(report: CollectionReport, config: CollectionConfig) -> None:
    layout = config.media_layout or "mixed"
    videos = [record for record in report.files if record.extension in VIDEO_EXTENSIONS and not _is_extra(record)]
    by_scope: dict[str, list[FileRecord]] = defaultdict(list)
    for record in report.files:
        by_scope[_scope(record, layout)].append(record)
    video_scopes = {_scope(record, layout) for record in videos}

    for record in videos:
        path = _path(record)
        if len(path.parts) == 1:
            report.findings.append(
                Finding(
                    "media-at-collection-root", "warning",
                    "Primary media is stored directly at the collection root",
                    relative_path=record.relative_path,
                    evidence={"layout": layout},
                )
            )
        expected_depth = 2 if layout == "movies" else 3
        if len(path.parts) > expected_depth and not _is_extra(record):
            report.findings.append(
                Finding(
                    "unexpected-media-depth", "info",
                    "Primary media is nested more deeply than the configured layout expects",
                    relative_path=record.relative_path,
                    evidence={"layout": layout, "depth": len(path.parts)},
                )
            )
        if layout == "series" and not _episode_keys(record):
            report.findings.append(
                Finding(
                    "episode-pattern-missing", "info",
                    "Series video filename has no conservative season/episode token",
                    relative_path=record.relative_path,
                )
            )

    relevant = SUBTITLE_EXTENSIONS | ARTWORK_EXTENSIONS | MEDIA_METADATA_EXTENSIONS
    for scope, records in by_scope.items():
        if scope in video_scopes or not any(record.extension in relevant for record in records):
            continue
        report.findings.append(
            Finding(
                "audiovisual-scope-without-video", "warning",
                "A media scope contains sidecars or artwork but no primary video",
                related_paths=tuple(sorted(record.relative_path for record in records if record.extension in relevant)),
                evidence={"layout": layout, "automatic_action": False},
            )
        )


def _generic_artwork(stem: str) -> bool:
    return bool(_GENERIC_ARTWORK_RE.fullmatch(stem) or _SEASON_ARTWORK_RE.fullmatch(stem))


def add_media_sidecar_findings(
    report: CollectionReport, config: CollectionConfig
) -> None:
    layout = config.media_layout or "mixed"
    videos = [record for record in report.files if record.extension in VIDEO_EXTENSIONS]
    by_scope: dict[str, list[FileRecord]] = defaultdict(list)
    for record in videos:
        by_scope[_scope(record, layout)].append(record)
    for record in report.files:
        if record.extension not in ARTWORK_EXTENSIONS | MEDIA_METADATA_EXTENSIONS:
            continue
        candidates = by_scope.get(_scope(record, layout), [])
        if not candidates:
            code = "orphan-media-artwork" if record.extension in ARTWORK_EXTENSIONS else "orphan-media-metadata"
            report.findings.append(
                Finding(
                    code, "warning",
                    "Media sidecar has no primary video in its configured scope",
                    relative_path=record.relative_path,
                    evidence={"layout": layout},
                )
            )
            continue
        if record.extension not in ARTWORK_EXTENSIONS:
            continue
        artwork_path = _path(record)
        if _generic_artwork(artwork_path.stem):
            continue
        matching = [
            candidate for candidate in candidates
            if _path(candidate).parent == artwork_path.parent
            and _path(candidate).stem.casefold() == artwork_path.stem.casefold()
        ]
        if _EPISODE_RE.search(artwork_path.stem) and not matching:
            report.findings.append(
                Finding(
                    "unmatched-episode-artwork", "info",
                    "Episode-specific artwork has no same-stem video candidate",
                    relative_path=record.relative_path,
                    evidence={"automatic_action": False},
                )
            )


def _subtitle_language(subtitle: FileRecord, matched: FileRecord) -> str | None:
    provenance = _subtitle_metadata(subtitle).get("provenance")
    if isinstance(provenance, dict) and isinstance(provenance.get("target_language"), str):
        return provenance["target_language"]
    subtitle_stem = _path(subtitle).stem
    media_stem = _path(matched).stem
    suffix = (
        subtitle_stem[len(media_stem):].strip("._- ")
        if subtitle_stem.casefold().startswith(media_stem.casefold())
        else subtitle_stem
    )
    tokens = re.split(r"[._ -]+", suffix.casefold())
    for token in reversed(tokens):
        if token in _LANGUAGE_ALIASES:
            return _LANGUAGE_ALIASES[token]
    return None


def _subtitle_candidates(
    subtitle: FileRecord, videos: list[FileRecord], layout: str
) -> list[FileRecord]:
    path = _path(subtitle)
    stem = path.stem.casefold()
    direct: list[FileRecord] = []
    for video in videos:
        video_path = _path(video)
        video_stem = video_path.stem.casefold()
        if video_path.parent == path.parent and (
            stem == video_stem or stem.startswith(video_stem + ".")
        ):
            direct.append(video)
    if direct or layout != "movies":
        return direct
    scope_directory = path.parent
    if scope_directory.name.casefold() in _SUBTITLE_DIRECTORIES:
        scope_directory = scope_directory.parent
    scope_videos = [
        video for video in videos
        if _path(video).parent == scope_directory and not _is_extra(video)
    ]
    return scope_videos if len(scope_videos) == 1 else []


def _attach_subtitle_metadata(
    report: CollectionReport,
    subtitle: FileRecord,
    matched: FileRecord,
    index: int,
) -> dict[str, Any]:
    language = _subtitle_language(subtitle, matched)
    metadata = dict(subtitle.metadata)
    audiovisual = dict(metadata.get("audiovisual") or {})
    subtitle_metadata = dict(audiovisual.get("subtitle") or {})
    subtitle_metadata.update({
        "matched_media_relative_path": matched.relative_path,
        "filename_language": language,
    })
    audiovisual["subtitle"] = subtitle_metadata
    metadata["audiovisual"] = audiovisual
    report.files[index] = replace(subtitle, metadata=metadata)
    if language is None:
        report.findings.append(
            Finding(
                "subtitle-language-unidentified", "info",
                "External subtitle language cannot be inferred from bounded provenance or filename evidence",
                relative_path=subtitle.relative_path,
            )
        )
    return subtitle_metadata


def _add_subtitle_runtime_findings(
    report: CollectionReport,
    subtitle: FileRecord,
    matched: FileRecord,
    metadata: dict[str, Any],
    settings: AudiovisualAnalysisConfig,
) -> None:
    runtime = _media_metadata(matched).get("duration_seconds")
    last_end = metadata.get("last_end_seconds")
    if not isinstance(runtime, (int, float)) or not isinstance(last_end, (int, float)):
        return
    if metadata.get("format") == "vobsub-index":
        return
    evidence = {"runtime_seconds": runtime, "last_end_seconds": last_end}
    if last_end > runtime + settings.subtitle_runtime_tolerance_seconds:
        report.findings.append(
            Finding(
                "subtitle-beyond-media-runtime", "error",
                "Subtitle timing extends beyond the matched media runtime",
                relative_path=subtitle.relative_path,
                related_paths=(matched.relative_path,), evidence=evidence,
            )
        )
    elif runtime >= 1200 and last_end < runtime * 0.5:
        report.findings.append(
            Finding(
                "subtitle-possibly-truncated", "warning",
                "Subtitle timing ends before the midpoint of long-form matched media",
                relative_path=subtitle.relative_path,
                related_paths=(matched.relative_path,), evidence=evidence,
            )
        )


def add_subtitle_relationships(
    report: CollectionReport,
    config: CollectionConfig,
    settings: AudiovisualAnalysisConfig,
) -> None:
    videos = [record for record in report.files if record.extension in VIDEO_EXTENSIONS]
    indexes = {record.relative_path: index for index, record in enumerate(report.files)}
    for subtitle in [record for record in report.files if record.extension in SUBTITLE_EXTENSIONS]:
        candidates = _subtitle_candidates(subtitle, videos, config.media_layout or "mixed")
        if len(candidates) != 1:
            code = "unmatched-external-subtitle" if not candidates else "ambiguous-external-subtitle"
            report.findings.append(
                Finding(
                    code, "warning",
                    "External subtitle does not have exactly one conservative media match",
                    relative_path=subtitle.relative_path,
                    related_paths=tuple(sorted(candidate.relative_path for candidate in candidates)),
                    evidence={"automatic_action": False},
                )
            )
            continue
        matched = candidates[0]
        metadata = _attach_subtitle_metadata(
            report, subtitle, matched, indexes[subtitle.relative_path]
        )
        _add_subtitle_runtime_findings(
            report, subtitle, matched, metadata, settings
        )


def add_embedded_subtitle_findings(report: CollectionReport) -> None:
    for record in report.files:
        streams = _media_metadata(record).get("streams")
        if not isinstance(streams, list):
            continue
        missing = sum(
            1 for stream in streams
            if isinstance(stream, dict) and stream.get("type") == "subtitle" and not stream.get("language")
        )
        if missing:
            report.findings.append(
                Finding(
                    "embedded-subtitle-language-missing", "info",
                    "One or more embedded subtitle streams have no language evidence",
                    relative_path=record.relative_path,
                    evidence={"stream_count": missing},
                )
            )


def add_audiovisual_findings(
    report: CollectionReport,
    config: CollectionConfig,
    settings: AudiovisualAnalysisConfig,
) -> None:
    if report.kind != "audiovisual" or not settings.enabled:
        return
    add_layout_findings(report, config)
    add_redundant_encode_groups(report, config)
    add_media_sidecar_findings(report, config)
    add_subtitle_relationships(report, config, settings)
    add_embedded_subtitle_findings(report)
