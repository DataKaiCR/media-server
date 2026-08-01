#!/usr/bin/env python3
"""Mark generated subtitles without modifying subtitle content.

Bazarr invokes this after every download. Whisper output receives user xattrs and
an append-only JSONL manifest entry. A later human-provider download clears the
xattrs, so an in-place replacement cannot remain mislabeled as generated.
"""

from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path

XATTRS = {
    "user.media_server.generated": b"true",
    "user.media_server.subtitle_source": b"whisperai",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clear_generated_markers(path: Path) -> None:
    for name in XATTRS:
        try:
            os.removexattr(path, name)
        except OSError:
            pass
    try:
        os.removexattr(path, "user.media_server.subtitle_sha256")
    except OSError:
        pass


def mark_generated(path: Path, manifest: Path, score: str) -> None:
    digest = sha256(path)
    for name, value in XATTRS.items():
        os.setxattr(path, name, value)
    os.setxattr(path, "user.media_server.subtitle_sha256", digest.encode("ascii"))

    record = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "path": str(path),
        "provider": "whisperai",
        "score": score,
        "sha256": digest,
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    with manifest.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps(record, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        fcntl.flock(handle, fcntl.LOCK_UN)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--subtitle", type=Path, required=True)
    parser.add_argument("--score", default="")
    parser.add_argument("--manifest", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.subtitle.is_file():
        raise FileNotFoundError(args.subtitle)

    if args.provider == "whisperai":
        mark_generated(args.subtitle, args.manifest, args.score)
        print("marked generated subtitle")
    else:
        clear_generated_markers(args.subtitle)
        print("cleared generated markers for human subtitle")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
