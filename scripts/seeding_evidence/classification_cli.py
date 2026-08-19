"""CLI for conservative qBittorrent policy-tag classification."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

from .classification import enforce_classification
from .client import ClientError
from .config import ConfigError, load_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Assign conservative default seeding-policy tags"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="private mode-0600 TOML configuration"
    )
    parser.add_argument(
        "--public-default-tier",
        default="standard",
        help="configured tier ID for non-private torrents (default: standard)",
    )
    parser.add_argument(
        "--private-default-tier",
        default="contributor",
        help="configured tier ID for private torrents (default: contributor)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply planned default tags; omission is aggregate report-only mode",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(args.config)
        config.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(config.report_dir, 0o700)
        lock_path = config.report_dir / ".classification.lock"
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another seeding classification run is active") from error
            outcome = enforce_classification(
                config,
                apply=args.apply,
                public_default_tier=args.public_default_tier,
                private_default_tier=args.private_default_tier,
            )
        finally:
            os.close(lock_descriptor)
    except (ClientError, ConfigError, OSError, RuntimeError, ValueError):
        print(
            json.dumps(
                {
                    "error": "seeding classification failed",
                    "mutation_scope": "policy-tags-only",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(outcome, sort_keys=True))
    return 2 if outcome["attention_required"] else 0
