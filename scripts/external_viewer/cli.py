"""Command-line entry point for isolated external-viewer onboarding."""

from __future__ import annotations

import argparse
import getpass
import json
import os
from pathlib import Path
import sys

from jellyfin_policy.client import ClientError
from jellyfin_policy.config import ConfigError, load_config
from jellyfin_policy.policy import PolicyError
from jellyfin_policy.service import ApplyError

from .client import ExternalJellyfinClient, JellyseerrClient, OnboardingClientError
from .config import (
    OnboardingConfig,
    OnboardingConfigError,
    ViewerCredential,
    create_attempt_config,
    create_onboarding_config,
    load_onboarding_config,
    load_viewer_credential,
    viewer_credential,
)
from .service import OnboardingError, apply, preflight


def _xdg_directory(variable: str, fallback: Path) -> Path:
    value = os.environ.get(variable)
    return Path(value).expanduser() if value else fallback


_CONFIG_HOME = _xdg_directory("XDG_CONFIG_HOME", Path.home() / ".config")
_STATE_HOME = _xdg_directory("XDG_STATE_HOME", Path.home() / ".local" / "state")
DEFAULT_CONFIG_PATH = (
    _CONFIG_HOME / "cine-pelencho" / "external-viewer-onboarding.toml"
)
DEFAULT_STATE_DIR = (
    _STATE_HOME / "cine-pelencho" / "external-viewer-onboarding"
)
LEGACY_CONFIG_PATH = Path(
    "/srv/private-state/jellyfin-external/onboarding.toml"
)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Set up, preflight, or apply isolated external-viewer onboarding"
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        help=(
            "stable private environment config; defaults to "
            "~/.config/cine-pelencho/external-viewer-onboarding.toml"
        ),
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--setup",
        action="store_true",
        help="create the stable private environment config interactively",
    )
    mode.add_argument(
        "--apply",
        action="store_true",
        help="create, enforce, import, authenticate, and verify one external viewer",
    )
    return parser.parse_args(argv)


def _absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return expanded if expanded.is_absolute() else (Path.cwd() / expanded).resolve()


def _config_path(value: Path | None, *, setup: bool = False) -> Path:
    if value is not None:
        return _absolute(value)
    if not setup and not DEFAULT_CONFIG_PATH.exists() and LEGACY_CONFIG_PATH.exists():
        return LEGACY_CONFIG_PATH
    return _absolute(DEFAULT_CONFIG_PATH)


def _interactive() -> bool:
    return sys.stdin.isatty()


def _prompt_value(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ")
    return value if value else default


def _prompt_path(label: str, default: Path) -> Path:
    home = str(Path.home())
    rendered = str(default)
    if rendered == home or rendered.startswith(home + os.sep):
        rendered = "~" + rendered[len(home):]
    return _absolute(Path(_prompt_value(label, rendered)))


def _setup(path: Path) -> dict[str, object]:
    if not _interactive():
        raise OnboardingConfigError("setup requires an interactive terminal")
    policy_path = _prompt_path(
        "Jellyfin policy config",
        Path("/srv/private-state/jellyfin-external/policy.toml"),
    )
    jellyseerr_url = _prompt_value(
        "Jellyseerr loopback API URL",
        "http://127.0.0.1:15055/api/v1",
    )
    jellyseerr_key_path = _prompt_path(
        "Jellyseerr API key file",
        Path("/srv/private-state/jellyseerr-external/api-key"),
    )
    state_dir = _prompt_path("Onboarding state directory", DEFAULT_STATE_DIR)
    create_onboarding_config(
        path,
        policy_config_file=policy_path,
        jellyseerr_base_url=jellyseerr_url,
        jellyseerr_api_key_file=jellyseerr_key_path,
        state_dir=state_dir,
    )
    return {
        "configuration_created": True,
        "credentials_persisted": False,
        "next_step": "run_preflight_or_apply",
    }


def _prompt_credential() -> ViewerCredential:
    if not _interactive():
        raise OnboardingConfigError(
            "viewer credentials require an interactive terminal or credential_file"
        )
    username = input("Viewer username: ")
    password = getpass.getpass("Viewer password: ")
    confirmation = getpass.getpass("Confirm viewer password: ")
    if password != confirmation:
        raise OnboardingConfigError("viewer passwords do not match")
    return viewer_credential(username, password)


def _credential(config: OnboardingConfig) -> ViewerCredential:
    if config.credential_file is not None:
        return load_viewer_credential(config.credential_file)
    return _prompt_credential()


def _confirm_apply() -> bool:
    answer = input("Preflight passed. Apply this viewer now? [y/N]: ")
    return answer.strip().casefold() in {"y", "yes"}


def _operate(config: OnboardingConfig, args: argparse.Namespace) -> None:
    credential = _credential(config)
    policy = load_config(config.policy_config_file)
    jellyfin = ExternalJellyfinClient(policy.base_url, policy.api_key_file)
    jellyseerr = JellyseerrClient(
        config.jellyseerr_base_url, config.jellyseerr_api_key_file
    )
    if args.apply:
        result = apply(
            create_attempt_config(config), policy, credential, jellyfin, jellyseerr
        )
        print(json.dumps(result, sort_keys=True))
        return
    result = preflight(policy, credential, jellyfin, jellyseerr)
    print(json.dumps(result, sort_keys=True), flush=True)
    if _interactive() and _confirm_apply():
        result = apply(
            create_attempt_config(config), policy, credential, jellyfin, jellyseerr
        )
        print(json.dumps(result, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        config_path = _config_path(args.config, setup=args.setup)
        if args.setup:
            print(json.dumps(_setup(config_path), sort_keys=True))
            return 0
        if not config_path.exists():
            if not _interactive():
                raise OnboardingConfigError(
                    "onboarding configuration is missing; run with --setup"
                )
            _setup(config_path)
        _operate(load_onboarding_config(config_path), args)
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
