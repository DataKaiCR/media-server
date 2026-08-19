"""CLI for reconciling tiered qBittorrent native share limits."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sys

from .client import ClientError
from .config import ConfigError, load_config
from .share_limits import enforce_share_limits


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Map seeding-policy tiers to qBittorrent native share limits"
    )
    parser.add_argument(
        "--config", required=True, type=Path, help="private mode-0600 TOML configuration"
    )
    parser.add_argument(
        "--native-stop-tier",
        action="append",
        dest="native_stop_tiers",
        help="override a configured native-stop tier (repeatable)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply native limits; omission is aggregate report-only mode",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    native_stop_tiers = (
        tuple(args.native_stop_tiers) if args.native_stop_tiers else None
    )
    try:
        config = load_config(args.config)
        config.report_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(config.report_dir, 0o700)
        lock_descriptor = os.open(
            config.report_dir / ".share-limits.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        try:
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError("another share-limit reconciliation is active") from error
            outcome = enforce_share_limits(
                config,
                apply=args.apply,
                native_stop_tiers=native_stop_tiers,
            )
        finally:
            os.close(lock_descriptor)
    except (ClientError, ConfigError, OSError, RuntimeError, ValueError):
        print(
            json.dumps(
                {
                    "error": "native share-limit reconciliation failed",
                    "mutation_scope": "share-limits-and-safe-stop-action-only",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(outcome, sort_keys=True))
    return 2 if outcome["attention_required"] else 0
