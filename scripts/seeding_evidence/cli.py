"""CLI for private, report-only seeding evidence."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path

from .client import QBittorrentClient
from .config import load_config
from .report import build_report, previous_report, publish_report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create aggregate-only qBittorrent seeding evidence")
    parser.add_argument("--config", required=True, type=Path, help="private mode-0600 TOML configuration")
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
            raise RuntimeError("another seeding evidence audit is already running") from error
        snapshot = QBittorrentClient(config.qbittorrent).snapshot()
        document = build_report(
            config,
            snapshot,
            previous=previous_report(config.report_dir),
        )
        destination, digest = publish_report(config.report_dir, document)
        print(json.dumps({
            "report": destination.name,
            "sha256": digest,
            "torrent_count": document["summary"]["torrent_count"],
            "unclassified_torrent_count": document["summary"]["unclassified_torrent_count"],
            "current_torrents_aggregate_ratio": document["summary"]["current_torrents_aggregate_ratio"],
        }, sort_keys=True))
        return 0
    finally:
        os.close(lock_descriptor)
