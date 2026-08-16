"""CLI for private, report-only acquisition reconciliation."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path

from .client import JellyseerrClient, RadarrClient
from .config import load_config
from .report import build_report, previous_report, publish_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile approved Jellyseerr movie requests with read-only Radarr evidence"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="private mode-0600 TOML configuration"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    config.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config.report_dir, 0o700)
    lock_path = config.report_dir / ".audit.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another acquisition reconciliation audit is already running") from error
        requests_by_instance = {
            service.service_id: JellyseerrClient(service).approved_movie_requests(
                config.max_requests_per_instance,
                config.page_size,
            )
            for service in config.jellyseerr
        }
        snapshot = RadarrClient(config.radarr).snapshot(config)
        document = build_report(
            config,
            requests_by_instance,
            snapshot,
            previous=previous_report(config.report_dir),
        )
        destination, digest = publish_report(config.report_dir, document)
        print(
            json.dumps(
                {
                    "report": destination.name,
                    "sha256": digest,
                    "approved_movie_request_count": document["summary"][
                        "approved_movie_request_count"
                    ],
                    "unique_movie_count": document["summary"]["unique_movie_count"],
                    "actionable_finding_count": document["summary"][
                        "actionable_finding_count"
                    ],
                    "category_counts": document["summary"]["category_counts"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        os.close(lock_descriptor)
