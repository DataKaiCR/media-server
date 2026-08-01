"""Private, atomic audit report publication."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
from typing import Any

from .model import CollectionReport


SCHEMA_VERSION = 1


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def report_document(reports: list[CollectionReport], config_hash: str) -> dict[str, Any]:
    summaries = [report.summary() for report in reports]
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "mode": "report-only",
        "config_sha256": config_hash,
        "capabilities": {
            "pdfinfo": {"available": shutil.which("pdfinfo") is not None},
        },
        "summary": {
            "collection_count": len(reports),
            "file_count": sum(summary["file_count"] for summary in summaries),
            "total_bytes": sum(summary["total_bytes"] for summary in summaries),
            "finding_count": sum(summary["finding_count"] for summary in summaries),
            "collections": summaries,
        },
        "collections": [report.to_dict() for report in reports],
        "proposed_actions": [],
    }


def publish_report(report_dir: Path, document: dict[str, Any]) -> tuple[Path, str]:
    report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(report_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = report_dir / f"librarian-audit-{stamp}.json"
    rendered = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    digest = hashlib.sha256(rendered).hexdigest()
    stage: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=report_dir, prefix=".librarian-audit-", suffix=".tmp", delete=False
        ) as handle:
            stage = Path(handle.name)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(stage, 0o600)
        os.replace(stage, destination)
        stage = None
        fsync_directory(report_dir)
        return destination, digest
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
