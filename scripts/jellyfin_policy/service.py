"""Backup-first Jellyfin policy application with API rollback."""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import tempfile
from typing import Protocol

from .config import PolicyConfig
from .policy import PolicyUpdate, build_plan, public_summary


class ApplyError(RuntimeError):
    """Policy application or rollback failed."""


class PolicyClient(Protocol):
    def users(self) -> list[object]: ...
    def virtual_folders(self) -> list[object]: ...
    def update_policy(self, user_id: object, policy: dict[str, object]) -> None: ...


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_backup(
    backup_dir: Path, users: list[object], folders: list[object]
) -> None:
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise ApplyError("backup directory must be a regular directory")
    os.chmod(backup_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = backup_dir / f"jellyfin-policies-{stamp}.json"
    rendered = (json.dumps(
        {"users": users, "virtual_folders": folders},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n").encode("utf-8")
    stage: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=backup_dir, prefix=".jellyfin-policies-",
            suffix=".tmp", delete=False,
        ) as handle:
            stage = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(rendered)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(stage, destination)
        stage = None
        _fsync_directory(backup_dir)
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def _rollback(client: PolicyClient, applied: list[PolicyUpdate]) -> None:
    errors = 0
    for update in reversed(applied):
        try:
            client.update_policy(update.user_id, update.original)
        except Exception:
            errors += 1
    try:
        current = {
            str(row.get("Id")): row.get("Policy")
            for row in client.users() if isinstance(row, dict)
        }
        if any(current.get(update.user_id) != update.original for update in applied):
            errors += 1
    except Exception:
        errors += 1
    if errors:
        raise ApplyError("policy application failed and rollback was incomplete")


def apply_plan(
    client: PolicyClient,
    config: PolicyConfig,
    users: list[object],
    folders: list[object],
) -> dict[str, object]:
    plan = build_plan(config, users, folders)
    pending = [update for update in plan if update.changed_fields]
    if not pending:
        result = public_summary(plan)
        result.update({"applied": False, "backup_created": False})
        return result
    try:
        _private_backup(config.backup_dir, users, folders)
    except OSError as error:
        raise ApplyError("cannot publish private policy backup") from error
    applied: list[PolicyUpdate] = []
    try:
        for update in pending:
            applied.append(update)
            client.update_policy(update.user_id, update.desired)
        verified = build_plan(
            config, client.users(), client.virtual_folders()
        )
        if any(update.changed_fields for update in verified):
            raise ApplyError("policy verification failed")
    except Exception as error:
        try:
            _rollback(client, applied)
        except ApplyError:
            raise
        raise ApplyError("policy application failed and was rolled back") from error
    result = public_summary(verified)
    result.update({
        "applied": True,
        "updated_account_count": len(applied),
        "backup_created": True,
    })
    return result
