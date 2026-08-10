#!/usr/bin/env python3
"""Mark generated subtitles without modifying subtitle content.

Bazarr invokes this after every download. Whisper output receives user xattrs and
an append-only JSONL manifest entry. A later human-provider download clears the
xattrs, so an in-place replacement cannot remain mislabeled as generated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat

WHISPER_XATTRS = {
    "user.media_server.generated": b"true",
    "user.media_server.subtitle_source": b"whisperai",
}
GENERATED_XATTRS = {
    *WHISPER_XATTRS,
    "user.media_server.subtitle_sha256",
    "user.media_server.source_sha256",
    "user.media_server.translation_model",
    "user.media_server.target_language",
}
_MISSING_XATTR_ERRORS = {
    errno.ENODATA,
    getattr(errno, "ENOATTR", errno.ENODATA),
}


def _regular_nonsymlink(path: Path, field: str) -> os.stat_result:
    try:
        state = os.lstat(path)
    except OSError as error:
        raise ValueError(f"{field} must be an existing regular file") from error
    if stat.S_ISLNK(state.st_mode) or not stat.S_ISREG(state.st_mode):
        raise ValueError(f"{field} must be an existing regular non-symlink file")
    return state


def sha256(path: Path) -> str:
    expected = _regular_nonsymlink(path, "subtitle")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as handle:
        before = os.fstat(handle.fileno())
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(handle.fileno())
    final = os.lstat(path)
    identity = lambda row: (row.st_dev, row.st_ino, row.st_size, row.st_mtime_ns)
    if identity(expected) != identity(before) or identity(before) != identity(after):
        raise RuntimeError("subtitle changed while it was being marked")
    if identity(after) != identity(final) or stat.S_ISLNK(final.st_mode):
        raise RuntimeError("subtitle changed while it was being marked")
    return digest.hexdigest()


def _xattr_snapshot(path: Path) -> dict[str, bytes]:
    snapshot: dict[str, bytes] = {}
    for name in GENERATED_XATTRS:
        try:
            snapshot[name] = os.getxattr(path, name, follow_symlinks=False)
        except OSError as error:
            if error.errno not in _MISSING_XATTR_ERRORS:
                raise
    return snapshot


def _restore_xattrs(path: Path, snapshot: dict[str, bytes]) -> None:
    for name in GENERATED_XATTRS:
        try:
            os.removexattr(path, name, follow_symlinks=False)
        except OSError as error:
            if error.errno not in _MISSING_XATTR_ERRORS:
                raise
    for name, value in snapshot.items():
        os.setxattr(path, name, value, follow_symlinks=False)


def clear_generated_markers(path: Path) -> None:
    _regular_nonsymlink(path, "subtitle")
    _restore_xattrs(path, {})


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _append_manifest(manifest: Path, record: dict[str, str]) -> None:
    if manifest.is_symlink() or (manifest.exists() and not manifest.is_file()):
        raise ValueError("manifest must be a regular non-symlink file")
    manifest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(manifest.parent, 0o700)
    flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(manifest, flags, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0, os.SEEK_END)
        original_size = handle.tell()
        try:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        except Exception:
            handle.seek(original_size)
            handle.truncate()
            handle.flush()
            os.fsync(handle.fileno())
            raise
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    _fsync_directory(manifest.parent)


def mark_generated(path: Path, manifest: Path, score: str) -> None:
    digest = sha256(path)
    snapshot = _xattr_snapshot(path)
    record = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "path": str(path),
        "provider": "whisperai",
        "score": score,
        "sha256": digest,
    }
    try:
        for name, value in WHISPER_XATTRS.items():
            os.setxattr(path, name, value, follow_symlinks=False)
        os.setxattr(
            path,
            "user.media_server.subtitle_sha256",
            digest.encode("ascii"),
            follow_symlinks=False,
        )
        _append_manifest(manifest, record)
    except Exception:
        _restore_xattrs(path, snapshot)
        raise


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--subtitle", type=Path, required=True)
    parser.add_argument("--score", default="")
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.provider == "whisperai":
        mark_generated(args.subtitle, args.manifest, args.score)
        print("marked generated subtitle")
    else:
        clear_generated_markers(args.subtitle)
        print("cleared generated markers for human subtitle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
