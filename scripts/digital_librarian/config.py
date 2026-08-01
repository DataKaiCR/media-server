"""Strict TOML configuration for private digital collections."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import tomllib


COLLECTION_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
COLLECTION_KINDS = {"audiovisual", "books", "photos"}


class ConfigError(ValueError):
    """The Librarian configuration is invalid or unsafe."""


@dataclass(frozen=True)
class CollectionConfig:
    collection_id: str
    kind: str
    root: Path
    exclude_globs: tuple[str, ...]


@dataclass(frozen=True)
class LibrarianConfig:
    report_dir: Path
    collections: tuple[CollectionConfig, ...]


def _absolute_directory(value: object, field_name: str, must_exist: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{field_name} must be a non-empty path string")
    path = Path(value)
    if not path.is_absolute():
        raise ConfigError(f"{field_name} must be absolute")
    if path.is_symlink():
        raise ConfigError(f"{field_name} must not be a symlink")
    if must_exist and not path.is_dir():
        raise ConfigError(f"{field_name} must be an existing directory")
    return path.resolve()


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def load_config(path: Path) -> LibrarianConfig:
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ConfigError("cannot read Librarian configuration") from error
    if raw.get("version") != 1:
        raise ConfigError("configuration version must be 1")
    report_dir = _absolute_directory(raw.get("report_dir"), "report_dir", False)
    rows = raw.get("collections")
    if not isinstance(rows, list) or not rows:
        raise ConfigError("at least one collection is required")

    collections: list[CollectionConfig] = []
    seen_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("each collection must be a TOML table")
        collection_id = row.get("id")
        if not isinstance(collection_id, str) or not COLLECTION_ID_RE.fullmatch(collection_id):
            raise ConfigError("collection id must be a lowercase public-safe identifier")
        if collection_id in seen_ids:
            raise ConfigError(f"duplicate collection id: {collection_id}")
        kind = row.get("kind")
        if kind not in COLLECTION_KINDS:
            raise ConfigError(f"unsupported collection kind: {kind}")
        root = _absolute_directory(row.get("root"), f"collection {collection_id} root", True)
        excludes = row.get("exclude_globs", [])
        if not isinstance(excludes, list) or not all(
            isinstance(pattern, str) and pattern for pattern in excludes
        ):
            raise ConfigError(f"collection {collection_id} exclude_globs must be strings")
        collections.append(CollectionConfig(collection_id, kind, root, tuple(excludes)))
        seen_ids.add(collection_id)

    for index, collection in enumerate(collections):
        if _is_within(report_dir, collection.root):
            raise ConfigError("report_dir must be outside every collection")
        for other in collections[index + 1:]:
            if _is_within(collection.root, other.root) or _is_within(other.root, collection.root):
                raise ConfigError("collection roots must not overlap")
    return LibrarianConfig(report_dir, tuple(collections))
