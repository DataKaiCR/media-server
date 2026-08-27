"""Transactional orchestration for an isolated external viewer."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import tempfile
from typing import Callable, Protocol

from jellyfin_policy.config import PolicyConfig, UserRule, load_config
from jellyfin_policy.policy import build_plan
from jellyfin_policy.service import apply_plan

from .config import OnboardingConfig, ViewerCredential


_MAX_POLICY_CONFIG_BYTES = 1_048_576
_REQUEST_PERMISSION = 32
_POLICY_PRESTATE = "jellyfin-policy.pre.toml"
_RECEIPT = "external-viewer-onboarding-receipt.json"


class OnboardingError(RuntimeError):
    """External-viewer onboarding could not complete safely."""


ProgressCallback = Callable[[str, str], None]


def _emit(
    progress: ProgressCallback | None, phase: str, status: str
) -> None:
    if progress is None:
        return
    try:
        progress(phase, status)
    except Exception:
        # Presentation must never alter transactional behavior.
        pass


class JellyfinOperations(Protocol):
    def users(self) -> list[object]: ...
    def virtual_folders(self) -> list[object]: ...
    def update_policy(self, user_id: object, policy: dict[str, object]) -> None: ...
    def create_user(self, username: str, password: str) -> str: ...
    def delete_user(self, user_id: object) -> None: ...
    def authenticate_user(self, username: str, password: str) -> tuple[str, str]: ...
    def user_views(self, user_id: object, token: str) -> list[object]: ...
    def logout(self, token: str) -> None: ...
    def public_users(self) -> list[object]: ...


class JellyseerrOperations(Protocol):
    def assert_safe_defaults(self) -> None: ...
    def users(self) -> list[dict[str, object]]: ...
    def available_jellyfin_users(self) -> list[dict[str, object]]: ...
    def import_jellyfin_user(self, jellyfin_user_id: str) -> int: ...
    def delete_user(self, user_id: int) -> None: ...
    def authenticate_user(self, username: str, password: str) -> int: ...


def _read_policy_config(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            raw = handle.read(_MAX_POLICY_CONFIG_BYTES + 1)
    except OSError as error:
        raise OnboardingError("cannot read private Jellyfin policy configuration") from error
    if len(raw) > _MAX_POLICY_CONFIG_BYTES:
        raise OnboardingError("private Jellyfin policy configuration is too large")
    return raw


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_private_bytes(path: Path, value: bytes) -> None:
    stage: Path | None = None
    try:
        descriptor, stage_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        stage = Path(stage_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, path)
        stage = None
        _fsync_directory(path.parent)
    except OSError as error:
        raise OnboardingError("cannot publish private onboarding state") from error
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def _write_private_exclusive(path: Path, value: bytes) -> None:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
    except OSError as error:
        raise OnboardingError("cannot publish private onboarding state") from error


def _augmented_policy(config: PolicyConfig, username: str) -> PolicyConfig:
    return replace(config, users=(*config.users, UserRule(username, "external")))


def _policy_with_external_user(raw: bytes, username: str) -> bytes:
    escaped = json.dumps(username, ensure_ascii=False)
    return raw.rstrip() + (
        f'\n\n[[users]]\nname = {escaped}\nrole = "external"\n'
    ).encode("utf-8")


def _user_name(row: dict[str, object]) -> str | None:
    for field in ("jellyfinUsername", "username"):
        value = row.get(field)
        if isinstance(value, str) and value:
            return value
    return None


def _names(rows: list[object], field: str) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(field), str):
            raise OnboardingError("account API returned a malformed identity")
        normalized = str(row[field]).casefold()
        if normalized in result:
            raise OnboardingError("account API returned duplicate identities")
        result[normalized] = row
    return result


def _jellyseerr_names(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for row in rows:
        name = _user_name(row)
        if name is None:
            raise OnboardingError("Jellyseerr returned an incomplete identity")
        normalized = name.casefold()
        if normalized in result:
            raise OnboardingError("Jellyseerr returned duplicate identities")
        result[normalized] = row
    return result


def _baseline(
    policy: PolicyConfig,
    credential: ViewerCredential,
    jellyfin: JellyfinOperations,
    jellyseerr: JellyseerrOperations,
) -> dict[str, object]:
    if not policy.users or any(rule.role != "external" for rule in policy.users):
        raise OnboardingError(
            "onboarding requires an isolated policy containing only external viewers"
        )
    target = credential.username.casefold()
    configured = {rule.name.casefold() for rule in policy.users}
    if target in configured:
        raise OnboardingError("target viewer already exists in private policy")

    jellyfin_users = jellyfin.users()
    jellyfin_by_name = _names(jellyfin_users, "Name")
    if target in jellyfin_by_name:
        raise OnboardingError("target viewer already exists in Jellyfin")
    plan = build_plan(policy, jellyfin_users, jellyfin.virtual_folders())
    if any(update.changed_fields for update in plan):
        raise OnboardingError("existing external Jellyfin policy has drift")
    if jellyfin.public_users():
        raise OnboardingError("external Jellyfin exposes a public login profile")

    jellyseerr.assert_safe_defaults()
    request_users = jellyseerr.users()
    jellyseerr_by_name = _jellyseerr_names(request_users)
    if target in jellyseerr_by_name:
        raise OnboardingError("target viewer already exists in Jellyseerr")
    request_only = {
        name for name, row in jellyseerr_by_name.items()
        if row.get("permissions") == _REQUEST_PERMISSION
    }
    administrators = [
        row for row in request_users
        if isinstance(row.get("permissions"), int)
        and not isinstance(row.get("permissions"), bool)
        and int(row["permissions"]) & 2
    ]
    if (
        request_only != configured
        or len(administrators) != 1
        or len(request_users) != len(configured) + 1
    ):
        raise OnboardingError("external Jellyseerr identity boundary has drift")
    return {
        "status": "preflight_passed",
        "ready": True,
        "apply_required": True,
        "existing_policy_compliant": True,
        "configured_external_count": len(configured),
        "jellyfin_public_profile_count": 0,
        "jellyseerr_request_only_count": len(request_only),
        "jellyseerr_new_user_auto_provisioning": False,
        "account_names_persisted": False,
        "user_ids_persisted": False,
        "credentials_persisted": False,
    }


def preflight(
    policy: PolicyConfig,
    credential: ViewerCredential,
    jellyfin: JellyfinOperations,
    jellyseerr: JellyseerrOperations,
) -> dict[str, object]:
    return _baseline(policy, credential, jellyfin, jellyseerr)


@dataclass(frozen=True)
class _ApplyContext:
    config: OnboardingConfig
    policy: PolicyConfig
    credential: ViewerCredential
    jellyfin: JellyfinOperations
    jellyseerr: JellyseerrOperations
    baseline: dict[str, object]
    augmented: PolicyConfig
    original_policy_bytes: bytes
    updated_policy_bytes: bytes
    receipt_path: Path


@dataclass
class _MutationState:
    jellyfin_user_id: str | None = None
    jellyseerr_user_id: int | None = None
    viewer_token: str | None = None


def _prepare_apply(
    config: OnboardingConfig,
    policy: PolicyConfig,
    credential: ViewerCredential,
    jellyfin: JellyfinOperations,
    jellyseerr: JellyseerrOperations,
) -> _ApplyContext:
    baseline = _baseline(policy, credential, jellyfin, jellyseerr)
    policy_prestate = config.state_dir / _POLICY_PRESTATE
    receipt_path = config.state_dir / _RECEIPT
    if policy_prestate.exists() or receipt_path.exists():
        raise OnboardingError("onboarding state directory already contains a receipt")
    original = _read_policy_config(config.policy_config_file)
    updated = _policy_with_external_user(original, credential.username)
    augmented = _augmented_policy(policy, credential.username)
    _write_private_exclusive(policy_prestate, original)
    return _ApplyContext(
        config, policy, credential, jellyfin, jellyseerr, baseline, augmented,
        original, updated, receipt_path,
    )


def _configure_jellyfin(context: _ApplyContext, state: _MutationState) -> None:
    jellyfin = context.jellyfin
    state.jellyfin_user_id = jellyfin.create_user(
        context.credential.username, context.credential.password
    )
    users_after_create = jellyfin.users()
    plan = build_plan(
        context.augmented, users_after_create, jellyfin.virtual_folders()
    )
    changed = [row for row in plan if row.changed_fields]
    if (
        len(changed) != 1
        or changed[0].user_id != state.jellyfin_user_id
        or changed[0].role != "external"
    ):
        raise OnboardingError("new Jellyfin policy plan is not isolated")
    policy_result = apply_plan(
        jellyfin, context.augmented, users_after_create, jellyfin.virtual_folders()
    )
    if (
        policy_result.get("applied") is not True
        or policy_result.get("backup_created") is not True
        or policy_result.get("updated_account_count") != 1
    ):
        raise OnboardingError("new Jellyfin policy was not applied exactly once")
    _atomic_private_bytes(
        context.config.policy_config_file, context.updated_policy_bytes
    )
    persisted = load_config(context.config.policy_config_file)
    if persisted.users != context.augmented.users:
        raise OnboardingError("private Jellyfin policy update was not persisted")
    verified = build_plan(persisted, jellyfin.users(), jellyfin.virtual_folders())
    if any(row.changed_fields for row in verified):
        raise OnboardingError("new Jellyfin policy verification failed")
    if jellyfin.public_users():
        raise OnboardingError("new Jellyfin viewer is visible in the public selector")
    _verify_jellyfin_login(context, state)


def _verify_jellyfin_login(context: _ApplyContext, state: _MutationState) -> None:
    authenticated_id, state.viewer_token = context.jellyfin.authenticate_user(
        context.credential.username, context.credential.password
    )
    try:
        if authenticated_id != state.jellyfin_user_id:
            raise OnboardingError("new Jellyfin credential resolved to another account")
        views = context.jellyfin.user_views(
            state.jellyfin_user_id, state.viewer_token
        )
        collection_types = {
            row.get("CollectionType") for row in views if isinstance(row, dict)
        }
        if len(views) != 2 or collection_types != {"movies", "tvshows"}:
            raise OnboardingError(
                "new Jellyfin viewer has incorrect effective libraries"
            )
        context.jellyfin.logout(state.viewer_token)
        state.viewer_token = None
    finally:
        if state.viewer_token is not None:
            try:
                context.jellyfin.logout(state.viewer_token)
            except Exception:
                pass
            else:
                state.viewer_token = None


def _configure_jellyseerr(context: _ApplyContext, state: _MutationState) -> None:
    available = [
        row for row in context.jellyseerr.available_jellyfin_users()
        if str(row.get("username", "")).casefold()
        == context.credential.username.casefold()
    ]
    if len(available) != 1 or available[0].get("id") != state.jellyfin_user_id:
        raise OnboardingError("Jellyseerr import target is not exact")
    if state.jellyfin_user_id is None:
        raise OnboardingError("Jellyfin account creation was not acknowledged")
    state.jellyseerr_user_id = context.jellyseerr.import_jellyfin_user(
        state.jellyfin_user_id
    )
    imported = [
        row for row in context.jellyseerr.users()
        if row.get("id") == state.jellyseerr_user_id
    ]
    if (
        len(imported) != 1
        or imported[0].get("permissions") != _REQUEST_PERMISSION
        or imported[0].get("userType") != 3
    ):
        raise OnboardingError("new Jellyseerr user is not request-only")
    permission = context.jellyseerr.authenticate_user(
        context.credential.username, context.credential.password
    )
    if permission != _REQUEST_PERMISSION:
        raise OnboardingError("new Jellyseerr login is not request-only")


def _publish_result(context: _ApplyContext) -> dict[str, object]:
    result = {
        **context.baseline,
        "status": "completed",
        "ready": False,
        "apply_required": False,
        "applied": True,
        "policy_backup_created": True,
        "policy_accounts_updated": 1,
        "configured_external_count": len(context.augmented.users),
        "jellyfin_authentication": "pass",
        "jellyfin_hidden_profile": True,
        "jellyfin_effective_collection_types": ["movies", "tvshows"],
        "jellyfin_public_profile_count": 0,
        "jellyseerr_authentication": "pass",
        "jellyseerr_permission": _REQUEST_PERMISSION,
        "jellyseerr_request_only_count": len(context.augmented.users),
        "rollback_required": False,
        "credential_storage": context.config.credential_storage,
        "credentials_persisted": context.config.credential_storage == "bitwarden",
        "account_names_persisted": context.config.credential_storage == "bitwarden",
    }
    rendered = (json.dumps(result, sort_keys=True) + "\n").encode("utf-8")
    _write_private_exclusive(context.receipt_path, rendered)
    return result


def _rollback(context: _ApplyContext, state: _MutationState) -> None:
    errors = 0
    if state.viewer_token is not None:
        try:
            context.jellyfin.logout(state.viewer_token)
        except Exception:
            errors += 1
    # Delete only IDs acknowledged by the APIs. Guessing an identity after an
    # ambiguous transport failure could remove an account created concurrently.
    if state.jellyseerr_user_id is not None:
        try:
            context.jellyseerr.delete_user(state.jellyseerr_user_id)
        except Exception:
            errors += 1
    if state.jellyfin_user_id is not None:
        try:
            context.jellyfin.delete_user(state.jellyfin_user_id)
        except Exception:
            errors += 1
    try:
        current = _read_policy_config(context.config.policy_config_file)
        if current != context.original_policy_bytes:
            _atomic_private_bytes(
                context.config.policy_config_file, context.original_policy_bytes
            )
    except Exception:
        errors += 1
    try:
        restored = load_config(context.config.policy_config_file)
        if restored.users != context.policy.users:
            errors += 1
        _baseline(
            restored, context.credential, context.jellyfin, context.jellyseerr
        )
    except Exception:
        errors += 1
    if errors:
        raise OnboardingError(
            "external viewer onboarding failed and rollback was incomplete"
        )


def apply(
    config: OnboardingConfig,
    policy: PolicyConfig,
    credential: ViewerCredential,
    jellyfin: JellyfinOperations,
    jellyseerr: JellyseerrOperations,
    *,
    progress: ProgressCallback | None = None,
) -> dict[str, object]:
    if config.credential_storage not in {"bitwarden", "memory"}:
        raise OnboardingError("credential storage result is invalid")
    _emit(progress, "rollback_state", "start")
    try:
        context = _prepare_apply(
            config, policy, credential, jellyfin, jellyseerr
        )
    except Exception:
        _emit(progress, "rollback_state", "failed")
        raise
    _emit(progress, "rollback_state", "done")
    state = _MutationState()
    phase = "jellyfin"
    try:
        _emit(progress, phase, "start")
        _configure_jellyfin(context, state)
        _emit(progress, phase, "done")
        phase = "jellyseerr"
        _emit(progress, phase, "start")
        _configure_jellyseerr(context, state)
        _emit(progress, phase, "done")
        phase = "receipt"
        _emit(progress, phase, "start")
        result = _publish_result(context)
        _emit(progress, phase, "done")
        return result
    except Exception as error:
        _emit(progress, phase, "failed")
        _emit(progress, "rollback", "start")
        try:
            _rollback(context, state)
        except OnboardingError:
            _emit(progress, "rollback", "failed")
            raise
        _emit(progress, "rollback", "done")
        raise OnboardingError(
            "external viewer onboarding failed and was rolled back"
        ) from error
