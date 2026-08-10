#!/usr/bin/env python3
"""Audit or backup-first enforce private Jellyfin viewer policies."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from jellyfin_policy.client import ClientError, JellyfinClient
from jellyfin_policy.config import ConfigError, load_config
from jellyfin_policy.policy import PolicyError, build_plan, public_summary
from jellyfin_policy.service import ApplyError, apply_plan


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit aggregate-only Jellyfin viewer policy drift"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true",
        help="backup, apply, verify, and roll back failures",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_config(args.config)
        client = JellyfinClient(config.base_url, config.api_key_file)
        users = client.users()
        folders = client.virtual_folders()
        if args.apply:
            result = apply_plan(client, config, users, folders)
        else:
            plan = build_plan(config, users, folders)
            result = public_summary(plan)
            result["compliant"] = not any(row.changed_fields for row in plan)
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("compliant", True) else 2
    except (ApplyError, ClientError, ConfigError, PolicyError) as error:
        print(f"policy operation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
