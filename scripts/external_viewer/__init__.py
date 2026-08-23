"""Isolated external Jellyfin and Jellyseerr viewer onboarding."""

from .config import (
    OnboardingConfig,
    OnboardingConfigError,
    ViewerCredential,
    load_onboarding_config,
    load_viewer_credential,
)
from .service import OnboardingError, apply, preflight

__all__ = [
    "OnboardingConfig",
    "OnboardingConfigError",
    "OnboardingError",
    "ViewerCredential",
    "apply",
    "load_onboarding_config",
    "load_viewer_credential",
    "preflight",
]
