"""Pure least-privilege policy planning with aggregate-only diagnostics."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
from typing import Any

from .config import PolicyConfig


_FOLDER_TYPES = {
    "guest": frozenset({"movies", "tvshows", "music"}),
    "household": frozenset({"movies", "tvshows", "music", "books"}),
    "restricted": frozenset({"movies", "tvshows"}),
}
_BLOCKED_UNRATED = ["Movie", "Trailer", "Series"]
_ITEM_ID_RE = re.compile(
    r"^(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12})$"
)


class PolicyError(ValueError):
    """Jellyfin state cannot be reconciled safely with the private policy."""


@dataclass(frozen=True)
class PolicyUpdate:
    user_id: str
    role: str
    original: dict[str, object]
    desired: dict[str, object]

    @property
    def changed_fields(self) -> tuple[str, ...]:
        return tuple(
            key for key, value in self.desired.items()
            if self.original.get(key) != value
        )


def _folder_ids(rows: list[object]) -> dict[str, list[str]]:
    folders: dict[str, list[str]] = {}
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise PolicyError("Jellyfin returned a malformed virtual folder")
        kind = row.get("CollectionType")
        item_id = row.get("ItemId")
        if not isinstance(kind, str) or not isinstance(item_id, str):
            raise PolicyError("Jellyfin returned an incomplete virtual folder")
        if not _ITEM_ID_RE.fullmatch(item_id) or item_id in seen_ids:
            raise PolicyError("Jellyfin returned an invalid folder identifier")
        folders.setdefault(kind, []).append(item_id)
        seen_ids.add(item_id)
    required = set().union(*_FOLDER_TYPES.values())
    if not required <= set(folders):
        raise PolicyError("required viewer collection types are unavailable")
    return folders


def _users_by_name(rows: list[object]) -> tuple[dict[str, dict[str, Any]], int]:
    users: dict[str, dict[str, Any]] = {}
    administrators = 0
    for row in rows:
        if not isinstance(row, dict):
            raise PolicyError("Jellyfin returned a malformed user")
        name = row.get("Name")
        user_id = row.get("Id")
        policy = row.get("Policy")
        if (
            not isinstance(name, str)
            or not isinstance(user_id, str)
            or not _ITEM_ID_RE.fullmatch(user_id)
            or not isinstance(policy, dict)
        ):
            raise PolicyError("Jellyfin returned an incomplete user")
        normalized = name.casefold()
        if normalized in users:
            raise PolicyError("Jellyfin returned duplicate user names")
        users[normalized] = row
        if policy.get("IsAdministrator") is True:
            administrators += 1
    if administrators != 1:
        raise PolicyError("exactly one Jellyfin administrator is required")
    return users, administrators


def _desired_policy(
    original: dict[str, object], role: str, folders: dict[str, list[str]]
) -> dict[str, object]:
    desired = dict(original)
    desired.update({
        "IsAdministrator": False,
        "EnableAllFolders": False,
        "EnabledFolders": sorted(
            item_id for kind in _FOLDER_TYPES[role] for item_id in folders[kind]
        ),
        "EnableContentDeletion": False,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": False,
        "EnableSyncTranscoding": False,
        "EnableMediaConversion": False,
        "EnablePublicSharing": False,
        "EnableRemoteAccess": False,
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": False,
        "EnableLiveTvAccess": False,
        "EnableLiveTvManagement": False,
        "EnableAllChannels": False,
        "EnabledChannels": [],
        "EnableCollectionManagement": False,
        "EnableSubtitleManagement": False,
        "EnableLyricManagement": False,
        "EnableMediaPlayback": True,
        "EnablePlaybackRemuxing": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": True,
    })
    if role == "restricted":
        desired["MaxParentalRating"] = 10
        desired["BlockUnratedItems"] = list(_BLOCKED_UNRATED)
    else:
        desired["MaxParentalRating"] = None
        desired["BlockUnratedItems"] = []
    return desired


def build_plan(
    config: PolicyConfig, users: list[object], folders: list[object]
) -> list[PolicyUpdate]:
    folder_ids = _folder_ids(folders)
    server_users, _ = _users_by_name(users)
    configured_names = {rule.name.casefold() for rule in config.users}
    non_admin_names = {
        name for name, row in server_users.items()
        if row["Policy"].get("IsAdministrator") is not True
    }
    if configured_names != non_admin_names:
        raise PolicyError(
            "private policy must enumerate every and only non-administrator account"
        )
    updates: list[PolicyUpdate] = []
    for rule in config.users:
        row = server_users[rule.name.casefold()]
        policy = row["Policy"]
        if policy.get("IsAdministrator") is True:
            raise PolicyError("administrator accounts cannot receive viewer roles")
        original = dict(policy)
        updates.append(PolicyUpdate(
            user_id=str(row["Id"]),
            role=rule.role,
            original=original,
            desired=_desired_policy(original, rule.role, folder_ids),
        ))
    return updates


def public_summary(plan: list[PolicyUpdate]) -> dict[str, object]:
    roles = Counter(update.role for update in plan)
    fields = Counter(
        field for update in plan for field in update.changed_fields
    )
    return {
        "mode": "aggregate-only",
        "configured_non_admin_count": len(plan),
        "role_counts": dict(sorted(roles.items())),
        "accounts_requiring_change": sum(bool(row.changed_fields) for row in plan),
        "field_change_counts": dict(sorted(fields.items())),
        "account_names_persisted": False,
        "user_ids_persisted": False,
    }
