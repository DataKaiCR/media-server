"""Conservative relationship and curation evidence for photo collections."""

from __future__ import annotations

from collections import defaultdict
import datetime as dt
from pathlib import PurePosixPath
from typing import Any

from .config import PhotoAnalysisConfig
from .model import CollectionReport, FileRecord, Finding


RAW_EXTENSIONS = {".arw", ".cr2", ".cr3", ".dng", ".nef", ".orf", ".raf", ".rw2"}
RENDERED_EXTENSIONS = {".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MOTION_EXTENSIONS = {".mov", ".mp4"}


def _photo(record: FileRecord) -> dict[str, Any]:
    metadata = record.metadata.get("photo", {})
    return metadata if isinstance(metadata, dict) else {}


def add_photo_metadata_findings(report: CollectionReport) -> None:
    if report.kind != "photos":
        return
    image_records = [record for record in report.files if _photo(record)]
    missing_time = [
        record.relative_path
        for record in image_records
        if not _photo(record).get("capture_time")
    ]
    malformed_time = [
        record.relative_path
        for record in image_records
        if not _photo(record).get("capture_time_valid", True)
    ]
    timezone_missing = [
        record.relative_path
        for record in image_records
        if _photo(record).get("capture_time")
        and not _photo(record).get("timezone_offset")
    ]
    timezone_invalid = [
        record.relative_path
        for record in image_records
        if not _photo(record).get("timezone_offset_valid", True)
    ]
    grouped = (
        (
            "photo-capture-time-missing", "info",
            "Images have no embedded original capture time", missing_time,
        ),
        (
            "photo-capture-time-invalid", "warning",
            "Images have malformed original capture times", malformed_time,
        ),
        (
            "photo-timezone-missing", "info",
            "Capture times have no embedded UTC offset", timezone_missing,
        ),
        (
            "photo-timezone-invalid", "warning",
            "Images have malformed embedded UTC offsets", timezone_invalid,
        ),
    )
    for code, severity, message, paths in grouped:
        if paths:
            report.findings.append(
                Finding(
                    code, severity, message,
                    related_paths=tuple(sorted(paths)),
                    evidence={"count": len(paths), "automatic_metadata_write": False},
                )
            )


def add_photo_pairs(report: CollectionReport) -> None:
    if report.kind != "photos":
        return
    groups: dict[tuple[str, str], list[FileRecord]] = defaultdict(list)
    for record in report.files:
        path = PurePosixPath(record.relative_path)
        groups[(path.parent.as_posix().casefold(), path.stem.casefold())].append(record)
    for records in groups.values():
        raw = [record for record in records if record.extension in RAW_EXTENSIONS]
        rendered = [
            record for record in records if record.extension in RENDERED_EXTENSIONS
        ]
        motion = [record for record in records if record.extension in MOTION_EXTENSIONS]
        if raw and rendered:
            report.findings.append(
                Finding(
                    "raw-rendered-pair", "info",
                    "RAW and rendered assets share a stem; both remain authoritative",
                    related_paths=tuple(sorted(
                        record.relative_path for record in raw + rendered
                    )),
                    evidence={"automatic_collapse": False},
                )
            )
        if motion and rendered:
            report.findings.append(
                Finding(
                    "live-photo-pair", "info",
                    "Still and motion assets share a stem; both remain authoritative",
                    related_paths=tuple(sorted(
                        record.relative_path for record in motion + rendered
                    )),
                    evidence={"automatic_collapse": False},
                )
            )
        if raw and not rendered:
            report.findings.append(
                Finding(
                    "raw-without-rendered-companion", "info",
                    "RAW asset has no same-stem rendered companion",
                    related_paths=tuple(sorted(
                        record.relative_path for record in raw
                    )),
                )
            )


class DisjointSet:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[right_root] = left_root


def _segments(value: int, count: int) -> list[tuple[int, int]]:
    base, extra = divmod(64, count)
    result = []
    shift = 0
    for index in range(count):
        width = base + int(index < extra)
        result.append((index, (value >> shift) & ((1 << width) - 1)))
        shift += width
    return result


def _compatible_dimensions(left: FileRecord, right: FileRecord) -> bool:
    left_width, left_height = left.metadata.get("width"), left.metadata.get("height")
    right_width, right_height = right.metadata.get("width"), right.metadata.get("height")
    if not all(isinstance(value, int) and value > 0 for value in (
        left_width, left_height, right_width, right_height
    )):
        return True
    left_ratio = left_width / left_height
    right_ratio = right_width / right_height
    return abs(left_ratio - right_ratio) / max(left_ratio, right_ratio) <= 0.03


def _fingerprint(record: FileRecord) -> int | None:
    fingerprint = _photo(record).get("visual_fingerprint", {})
    if not isinstance(fingerprint, dict):
        return None
    value = fingerprint.get("value")
    try:
        return int(str(value), 16) if value is not None else None
    except ValueError:
        return None


def add_perceptual_duplicate_groups(
    report: CollectionReport, settings: PhotoAnalysisConfig
) -> None:
    if report.kind != "photos" or not settings.perceptual_duplicates:
        return
    records = [record for record in report.files if _fingerprint(record) is not None]
    if len(records) < 2:
        return
    fingerprints = [_fingerprint(record) for record in records]
    union = DisjointSet(len(records))
    buckets: dict[tuple[int, int], list[int]] = defaultdict(list)
    edges: list[tuple[int, int, int]] = []
    segment_count = settings.near_duplicate_distance + 1
    for index, fingerprint in enumerate(fingerprints):
        assert fingerprint is not None
        candidates: set[int] = set()
        keys = _segments(fingerprint, segment_count)
        for key in keys:
            candidates.update(buckets[key])
        for candidate in candidates:
            other = fingerprints[candidate]
            assert other is not None
            distance = (fingerprint ^ other).bit_count()
            exact_content = (
                records[index].sha256 is not None
                and records[index].sha256 == records[candidate].sha256
            )
            if (
                distance <= settings.near_duplicate_distance
                and not exact_content
                and _compatible_dimensions(records[index], records[candidate])
            ):
                union.union(index, candidate)
                edges.append((index, candidate, distance))
        for key in keys:
            buckets[key].append(index)
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        groups[union.find(index)].append(index)
    for indexes in groups.values():
        if len(indexes) < 2:
            continue
        index_set = set(indexes)
        distances = [
            distance for left, right, distance in edges
            if left in index_set and right in index_set
        ]
        report.findings.append(
            Finding(
                "perceptual-duplicate-group", "warning",
                "Local visual fingerprints are similar; no deletion is proposed",
                related_paths=tuple(sorted(
                    records[index].relative_path for index in indexes
                )),
                evidence={
                    "algorithm": "dhash-64-v1",
                    "threshold": settings.near_duplicate_distance,
                    "maximum_link_distance": max(distances, default=0),
                    "count": len(indexes),
                    "automatic_delete": False,
                },
            )
        )


def _capture_datetime(record: FileRecord) -> dt.datetime | None:
    value = _photo(record).get("capture_time")
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _quality_rank(record: FileRecord) -> tuple[float, float, str]:
    quality = _photo(record).get("quality", {})
    if not isinstance(quality, dict):
        return (0.0, 0.0, record.relative_path)
    return (
        -float(quality.get("edge_strength", 0.0)),
        -float(quality.get("entropy_bits", 0.0)),
        record.relative_path,
    )


def add_burst_groups(
    report: CollectionReport, settings: PhotoAnalysisConfig
) -> None:
    if report.kind != "photos":
        return
    by_directory: dict[str, list[FileRecord]] = defaultdict(list)
    for record in report.files:
        if _capture_datetime(record) is not None:
            directory = PurePosixPath(record.relative_path).parent.as_posix()
            by_directory[directory].append(record)
    for records in by_directory.values():
        ordered = sorted(records, key=lambda record: _capture_datetime(record) or dt.datetime.min)
        burst: list[FileRecord] = []
        for record in ordered:
            if burst:
                previous = _capture_datetime(burst[-1])
                current = _capture_datetime(record)
                assert previous is not None and current is not None
                first = _capture_datetime(burst[0])
                assert first is not None
                gap = (current - previous).total_seconds()
                span = (current - first).total_seconds()
                if (
                    gap > settings.burst_window_seconds
                    or span > settings.burst_max_span_seconds
                ):
                    _append_burst(report, burst, settings)
                    burst = []
            burst.append(record)
        _append_burst(report, burst, settings)


def _append_burst(
    report: CollectionReport,
    burst: list[FileRecord],
    settings: PhotoAnalysisConfig,
) -> None:
    if len(burst) < 3:
        return
    ranked = sorted(burst, key=_quality_rank)
    report.findings.append(
        Finding(
            "possible-photo-burst", "info",
            "Capture times suggest a burst; review order uses bounded quality signals",
            related_paths=tuple(record.relative_path for record in ranked),
            evidence={
                "count": len(ranked),
                "maximum_gap_seconds": settings.burst_window_seconds,
                "maximum_span_seconds": settings.burst_max_span_seconds,
                "related_paths_order": "review-order-only",
                "automatic_selection": False,
            },
        )
    )
