"""Command-line entry point for isolated external-viewer onboarding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from jellyfin_policy.client import ClientError
from jellyfin_policy.config import ConfigError, load_config
from jellyfin_policy.policy import PolicyError
from jellyfin_policy.service import ApplyError

from .client import ExternalJellyfinClient, JellyseerrClient, OnboardingClientError
from .config import (
    OnboardingConfigError,
    load_onboarding_config,
    load_viewer_credential,
)
from .service import OnboardingError, apply, preflight


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preflight or apply aggregate-only isolated external-viewer onboarding"
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create, enforce, import, authenticate, and verify one external viewer",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config = load_onboarding_config(args.config)
        credential = load_viewer_credential(config.credential_file)
        policy = load_config(config.policy_config_file)
        jellyfin = ExternalJellyfinClient(
            policy.base_url, policy.api_key_file
        )
        jellyseerr = JellyseerrClient(
            config.jellyseerr_base_url, config.jellyseerr_api_key_file
        )
        result = (
            apply(config, policy, credential, jellyfin, jellyseerr)
            if args.apply
            else preflight(policy, credential, jellyfin, jellyseerr)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (
        ApplyError,
        ClientError,
        ConfigError,
        OnboardingClientError,
        OnboardingConfigError,
        OnboardingError,
        PolicyError,
    ) as error:
        print(f"external viewer onboarding failed: {error}", file=sys.stderr)
        return 1
