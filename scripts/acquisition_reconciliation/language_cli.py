"""CLI for private, report-only post-import language verification."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path

from .client import JellyseerrClient, RadarrClient
from .language_config import load_language_config
from .language_report import (
    build_language_report,
    previous_language_report,
    publish_language_report,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify imported request audio labels with bounded local ffprobe evidence"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="private mode-0600 TOML configuration"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_language_config(args.config)
    config.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(config.report_dir, 0o700)
    lock_path = config.report_dir / ".audit.lock"
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("another language verification audit is already running") from error
        requests_by_instance = {
            service.service_id: JellyseerrClient(service).approved_movie_requests(
                config.max_requests_per_instance,
                config.page_size,
            )
            for service in config.jellyseerr
        }
        movies = RadarrClient(config.radarr).movies(config.max_radarr_movies)
        document = build_language_report(
            config,
            requests_by_instance,
            movies,
            previous=previous_language_report(config.report_dir),
        )
        destination, digest = publish_language_report(config.report_dir, document)
        print(
            json.dumps(
                {
                    "report": destination.name,
                    "sha256": digest,
                    "unique_requested_movie_count": document["summary"][
                        "unique_requested_movie_count"
                    ],
                    "verified_file_count": document["summary"]["verified_file_count"],
                    "review_finding_count": document["summary"]["review_finding_count"],
                    "category_counts": document["summary"]["category_counts"],
                    "skipped_counts": document["summary"]["skipped_counts"],
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        os.close(lock_descriptor)
