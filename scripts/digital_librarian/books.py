"""Bibliographic coordination and conservative book grouping."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from pathlib import Path, PurePosixPath
import re
import unicodedata

from .book_common import bibliographic_evidence
from .config import BookAnalysisConfig
from .epub_analysis import analyze_epub
from .mobi_analysis import analyze_mobi
from .model import CollectionReport, FileRecord, Finding
from .pdf_analysis import analyze_pdf


DOCUMENT_EXTENSIONS = {".azw3", ".epub", ".mobi", ".pdf"}
COVER_EXTENSIONS = {".gif", ".jpeg", ".jpg", ".png", ".webp"}


def analyze_book(
    path: Path,
    extension: str,
    detected_format: str | None,
    settings: BookAnalysisConfig,
) -> tuple[dict[str, object], list[Finding]]:
    if detected_format == "pdf":
        return analyze_pdf(path, settings)
    if detected_format == "epub":
        return analyze_epub(path)
    if detected_format == "mobi" or extension in {".azw3", ".mobi"}:
        return analyze_mobi(path)
    if extension in DOCUMENT_EXTENSIONS:
        return {
            "bibliographic": bibliographic_evidence(filename=path.stem)
        }, []
    return {}, []


def _normalized_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.sub(r"[^\w]+", " ", normalized).split())


def _book_records(report: CollectionReport) -> list[FileRecord]:
    return [
        record
        for record in report.files
        if record.extension in DOCUMENT_EXTENSIONS
    ]


def add_external_cover_evidence(report: CollectionReport) -> None:
    if report.kind != "books":
        return
    by_directory: dict[str, list[FileRecord]] = defaultdict(list)
    for record in report.files:
        directory = PurePosixPath(record.relative_path).parent.as_posix()
        by_directory[directory].append(record)
    for index, record in enumerate(report.files):
        if record.extension not in DOCUMENT_EXTENSIONS:
            continue
        path = PurePosixPath(record.relative_path)
        candidates = []
        for candidate in by_directory[path.parent.as_posix()]:
            candidate_path = PurePosixPath(candidate.relative_path)
            if candidate.extension not in COVER_EXTENSIONS:
                continue
            if (
                candidate_path.stem.casefold() == path.stem.casefold()
                or candidate_path.stem.casefold() in {"cover", "folder"}
            ):
                candidates.append(candidate.relative_path)
        if candidates:
            metadata = dict(record.metadata)
            cover = dict(metadata.get("cover_evidence", {}))
            cover["external_candidates"] = sorted(candidates)[:8]
            metadata["cover_evidence"] = cover
            report.files[index] = replace(record, metadata=metadata)


def _bibliography(record: FileRecord) -> dict[str, object]:
    evidence = record.metadata.get("bibliographic", {})
    return evidence if isinstance(evidence, dict) else {}


def _group_inputs(
    records: list[FileRecord],
) -> tuple[
    dict[str, list[FileRecord]],
    dict[tuple[str, str], list[FileRecord]],
    dict[str, list[FileRecord]],
]:
    isbn_groups: dict[str, list[FileRecord]] = defaultdict(list)
    work_groups: dict[tuple[str, str], list[FileRecord]] = defaultdict(list)
    series_groups: dict[str, list[FileRecord]] = defaultdict(list)
    for record in records:
        bibliography = _bibliography(record)
        for identifier in bibliography.get("identifiers", []):
            if (
                isinstance(identifier, dict)
                and identifier.get("scheme") == "isbn-13"
            ):
                isbn_groups[str(identifier.get("value"))].append(record)
        titles = bibliography.get("titles", [])
        creators = bibliography.get("creators", [])
        if isinstance(titles, list) and isinstance(creators, list) and titles and creators:
            key = (_normalized_text(str(titles[0])), _normalized_text(str(creators[0])))
            if len(key[0]) >= 4 and len(key[1]) >= 3:
                work_groups[key].append(record)
        if series := _normalized_text(str(bibliography.get("series", ""))):
            series_groups[series].append(record)
    return isbn_groups, work_groups, series_groups


def add_bibliographic_groups(report: CollectionReport) -> None:
    if report.kind != "books":
        return
    isbn_groups, work_groups, series_groups = _group_inputs(
        _book_records(report)
    )
    isbn_path_sets: set[frozenset[str]] = set()
    for isbn, members in sorted(isbn_groups.items()):
        paths = tuple(sorted({record.relative_path for record in members}))
        if len(paths) < 2:
            continue
        isbn_path_sets.add(frozenset(paths))
        report.findings.append(
            Finding(
                "probable-edition-group", "info",
                "Files share a validated ISBN; every format remains distinct",
                related_paths=paths,
                evidence={
                    "basis": "isbn-13",
                    "identifier": isbn,
                    "confidence": "high",
                    "formats": sorted({record.extension for record in members}),
                    "automatic_collapse": False,
                },
            )
        )
    for _, members in sorted(work_groups.items()):
        paths = tuple(sorted({record.relative_path for record in members}))
        if len(paths) < 2 or frozenset(paths) in isbn_path_sets:
            continue
        report.findings.append(
            Finding(
                "possible-work-group", "info",
                "Embedded title and creator metadata match; edition identity remains uncertain",
                related_paths=paths,
                evidence={
                    "basis": "normalized-embedded-title-creator",
                    "confidence": "medium",
                    "formats": sorted({record.extension for record in members}),
                    "automatic_collapse": False,
                },
            )
        )
    for _, members in sorted(series_groups.items()):
        paths = tuple(sorted({record.relative_path for record in members}))
        if len(paths) < 2:
            continue
        volumes = [
            str(_bibliography(record)["volume"])
            for record in members
            if _bibliography(record).get("volume")
        ]
        report.findings.append(
            Finding(
                "series-group", "info",
                "Embedded metadata identifies members of one series",
                related_paths=paths,
                evidence={
                    "volumes": sorted(set(volumes)),
                    "automatic_reorder": False,
                },
            )
        )
