"""Read-only collection traversal and evidence generation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
import hashlib
import os
from pathlib import Path, PurePosixPath

from .books import add_bibliographic_groups, add_external_cover_evidence
from .config import BookAnalysisConfig, CollectionConfig, PhotoAnalysisConfig
from .formats import inspect_file
from .model import CollectionReport, FileRecord, Finding
from .photo_groups import (
    add_burst_groups,
    add_perceptual_duplicate_groups,
    add_photo_metadata_findings,
    add_photo_pairs,
)


SIDECAR_EXTENSIONS = {".aae", ".xmp"}


def excluded(relative_path: str, patterns: tuple[str, ...]) -> bool:
    candidate = PurePosixPath(relative_path)
    return any(candidate.match(pattern) for pattern in patterns)


def attach_path(finding: Finding, relative_path: str) -> Finding:
    return replace(finding, relative_path=relative_path)


class FileChangedError(OSError):
    """A collection file changed while evidence was being collected."""


def sha256_stable(path: Path, record: FileRecord) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        if (before.st_size, before.st_mtime_ns) != (record.size, record.modified_ns):
            raise FileChangedError("file changed before hashing")
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(handle.fileno())
    final = os.lstat(path)
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino, after.st_size, after.st_mtime_ns
    ) or before.st_ino != final.st_ino:
        raise FileChangedError("file changed during hashing")
    return digest.hexdigest()


def scan_entries(
    config: CollectionConfig,
    book_analysis: BookAnalysisConfig | None = None,
    photo_analysis: PhotoAnalysisConfig | None = None,
) -> CollectionReport:
    report = CollectionReport(
        collection_id=config.collection_id,
        kind=config.kind,
        role=config.role,
        root=str(config.root),
    )
    for directory, dirnames, filenames in os.walk(config.root, topdown=True, followlinks=False):
        current = Path(directory)
        kept_dirs: list[str] = []
        for dirname in sorted(dirnames):
            path = current / dirname
            relative = path.relative_to(config.root).as_posix()
            if excluded(relative, config.exclude_globs):
                continue
            if path.is_symlink():
                report.findings.append(
                    Finding("symlink-skipped", "info", "Symbolic link was not followed", relative)
                )
                continue
            kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in sorted(filenames):
            path = current / filename
            relative = path.relative_to(config.root).as_posix()
            if excluded(relative, config.exclude_globs):
                continue
            if path.is_symlink():
                report.findings.append(
                    Finding("symlink-skipped", "info", "Symbolic link was not followed", relative)
                )
                continue
            try:
                file_stat = path.stat()
            except OSError:
                report.findings.append(
                    Finding("unreadable-file", "error", "File metadata cannot be read", relative)
                )
                continue
            if not path.is_file():
                continue
            extension = path.suffix.casefold()
            detected, metadata, findings = inspect_file(
                path, extension, config.kind, book_analysis, photo_analysis
            )
            try:
                post_stat = os.lstat(path)
                if (file_stat.st_ino, file_stat.st_size, file_stat.st_mtime_ns) != (
                    post_stat.st_ino, post_stat.st_size, post_stat.st_mtime_ns
                ):
                    findings.append(
                        Finding("changed-during-scan", "error", "File changed during inspection")
                    )
            except OSError:
                findings.append(
                    Finding("changed-during-scan", "error", "File disappeared during inspection")
                )
            report.files.append(
                FileRecord(
                    relative_path=relative,
                    extension=extension,
                    size=file_stat.st_size,
                    modified_ns=file_stat.st_mtime_ns,
                    detected_format=detected,
                    metadata=metadata,
                )
            )
            report.findings.extend(attach_path(finding, relative) for finding in findings)
    report.files.sort(key=lambda record: record.relative_path)
    return report


def add_case_collisions(report: CollectionReport) -> None:
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for record in report.files:
        path = PurePosixPath(record.relative_path)
        groups[(path.parent.as_posix().casefold(), path.name.casefold())].append(record.relative_path)
    for paths in groups.values():
        if len(paths) > 1:
            report.findings.append(
                Finding(
                    "case-colliding-filenames",
                    "warning",
                    "Files differ only by filename case",
                    related_paths=tuple(sorted(paths)),
                    evidence={"count": len(paths)},
                )
            )


def add_orphan_sidecars(report: CollectionReport) -> None:
    if report.kind != "photos":
        return
    assets: set[tuple[str, str]] = set()
    sidecars: list[FileRecord] = []
    for record in report.files:
        path = PurePosixPath(record.relative_path)
        key = (path.parent.as_posix().casefold(), path.stem.casefold())
        if record.extension in SIDECAR_EXTENSIONS:
            sidecars.append(record)
        else:
            assets.add(key)
    for record in sidecars:
        path = PurePosixPath(record.relative_path)
        key = (path.parent.as_posix().casefold(), path.stem.casefold())
        if key not in assets:
            report.findings.append(
                Finding(
                    "orphan-photo-sidecar",
                    "warning",
                    "Photo metadata sidecar has no matching asset stem",
                    relative_path=record.relative_path,
                )
            )


def add_exact_duplicates(report: CollectionReport, root: Path) -> None:
    by_size: dict[int, list[int]] = defaultdict(list)
    for index, record in enumerate(report.files):
        if record.size > 0:
            by_size[record.size].append(index)
    for size, indexes in sorted(by_size.items()):
        if len(indexes) < 2:
            continue
        by_hash: dict[str, list[int]] = defaultdict(list)
        for index in indexes:
            record = report.files[index]
            try:
                digest = record.sha256 or sha256_stable(
                    root / record.relative_path, record
                )
            except OSError:
                report.findings.append(
                    Finding(
                        "hash-read-failed",
                        "error",
                        "File changed or became unreadable during duplicate hashing",
                        relative_path=record.relative_path,
                    )
                )
                continue
            report.files[index] = replace(record, sha256=digest)
            by_hash[digest].append(index)
        for digest, duplicate_indexes in sorted(by_hash.items()):
            if len(duplicate_indexes) < 2:
                continue
            paths = tuple(sorted(report.files[index].relative_path for index in duplicate_indexes))
            report.findings.append(
                Finding(
                    "exact-duplicate-group",
                    "warning",
                    "Files have identical content; no deletion is proposed",
                    related_paths=paths,
                    evidence={"count": len(paths), "bytes_each": size, "sha256": digest},
                )
            )


def add_intake_hashes(report: CollectionReport, root: Path) -> None:
    if report.role != "intake":
        return
    for index, record in enumerate(report.files):
        try:
            digest = sha256_stable(root / record.relative_path, record)
        except OSError:
            report.findings.append(
                Finding(
                    "intake-hash-failed", "error",
                    "Intake file changed or became unreadable during hashing",
                    relative_path=record.relative_path,
                )
            )
            continue
        metadata = dict(record.metadata)
        metadata["intake_status"] = "awaiting-review"
        report.files[index] = replace(record, sha256=digest, metadata=metadata)


def audit_collection(
    config: CollectionConfig,
    book_analysis: BookAnalysisConfig | None = None,
    photo_analysis: PhotoAnalysisConfig | None = None,
) -> CollectionReport:
    photo_settings = photo_analysis or PhotoAnalysisConfig()
    report = scan_entries(config, book_analysis, photo_settings)
    add_case_collisions(report)
    add_orphan_sidecars(report)
    add_external_cover_evidence(report)
    add_bibliographic_groups(report)
    add_intake_hashes(report, config.root)
    add_exact_duplicates(report, config.root)
    add_photo_metadata_findings(report)
    add_photo_pairs(report)
    add_perceptual_duplicate_groups(report, photo_settings)
    add_burst_groups(report, photo_settings)
    report.findings.sort(
        key=lambda finding: (
            finding.severity,
            finding.code,
            finding.relative_path or "",
            finding.related_paths,
        )
    )
    return report
