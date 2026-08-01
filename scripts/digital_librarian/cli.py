"""Command-line entry point for report-only collection audits."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path

from .config import ConfigError, load_config
from .report import publish_report, report_document
from .scanner import audit_collection


class AuditAlreadyRunning(RuntimeError):
    """Another audit owns the report-directory lock."""


def config_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit private digital collections without modifying them")
    parser.add_argument("--config", type=Path, required=True)
    return parser


def run(config_path: Path) -> dict[str, object]:
    config_hash = config_sha256(config_path)
    config = load_config(config_path)
    if config_sha256(config_path) != config_hash:
        raise ConfigError("configuration changed while the audit was starting")
    config.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config.report_dir, 0o700)
    lock_path = config.report_dir / ".audit.lock"
    with lock_path.open("a+b") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise AuditAlreadyRunning("another Librarian audit is running") from error
        reports = [
            audit_collection(
                collection, config.book_analysis, config.photo_analysis
            )
            for collection in config.collections
        ]
        document = report_document(
            reports, config_hash, config.book_analysis, config.photo_analysis
        )
        destination, digest = publish_report(config.report_dir, document)
        fcntl.flock(lock, fcntl.LOCK_UN)
    return {
        "status": "reported",
        "mode": "report-only",
        "report_file": destination.name,
        "report_sha256": digest,
        "collections": [report.summary() for report in reports],
    }


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = run(args.config)
    except (AuditAlreadyRunning, ConfigError, OSError) as error:
        raise SystemExit(f"Librarian audit rejected: {error}") from error
    print(json.dumps(result, sort_keys=True))
    return 0
