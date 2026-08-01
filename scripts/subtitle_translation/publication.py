"""Backup-first atomic publication and provenance recording."""

from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile

from .errors import ValidationError
from .srt import Cue, parse_srt, render_srt
from .validation import validate_translation_quality


PROVENANCE_XATTRS = {
    "user.media_server.generated",
    "user.media_server.subtitle_source",
    "user.media_server.subtitle_sha256",
    "user.media_server.source_sha256",
    "user.media_server.translation_model",
    "user.media_server.target_language",
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def set_provenance_xattrs(
    path: Path, *, source_hash: str, output_hash: str, model: str
) -> None:
    values = {
        "user.media_server.generated": "true",
        "user.media_server.subtitle_source": "ollama-translation",
        "user.media_server.subtitle_sha256": output_hash,
        "user.media_server.source_sha256": source_hash,
        "user.media_server.translation_model": model,
        "user.media_server.target_language": "es-419",
    }
    try:
        for name, value in values.items():
            os.setxattr(path, name, value.encode("utf-8"))
    except OSError as error:
        raise ValidationError("filesystem cannot persist provenance xattrs") from error


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def verify_generated_destination(destination: Path) -> None:
    try:
        generated = os.getxattr(destination, "user.media_server.generated")
        source = os.getxattr(destination, "user.media_server.subtitle_source")
    except OSError as error:
        raise ValidationError("refusing to replace an unmarked or human subtitle") from error
    if generated != b"true" or source != b"ollama-translation":
        raise ValidationError("refusing to replace an unmarked or human subtitle")


def backup_destination(destination: Path, backup_dir: Path) -> Path:
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup = backup_dir / f"{destination.name}.{stamp}.bak"
    shutil.copy2(destination, backup)
    os.chmod(backup, 0o600)
    with backup.open("rb") as handle:
        os.fsync(handle.fileno())
    fsync_directory(backup_dir)
    return backup


def append_manifest(manifest: Path, record: dict[str, object]) -> None:
    manifest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(manifest.parent, 0o700)
    line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
    with manifest.open("a+", encoding="utf-8") as handle:
        os.chmod(manifest, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0, os.SEEK_END)
        original_size = handle.tell()
        try:
            handle.write(line)
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
    fsync_directory(manifest.parent)


def restore_after_failure(destination: Path, backup: Path | None) -> None:
    if backup is None:
        destination.unlink(missing_ok=True)
        fsync_directory(destination.parent)
        return
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".rollback",
        delete=False,
    ) as handle:
        rollback_stage = Path(handle.name)
        with backup.open("rb") as source:
            shutil.copyfileobj(source, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        shutil.copystat(backup, rollback_stage)
        os.replace(rollback_stage, destination)
        fsync_directory(destination.parent)
    finally:
        rollback_stage.unlink(missing_ok=True)


def translation_record(
    *,
    source_path: Path,
    destination: Path,
    source_hash: str,
    output_hash: str,
    model: str,
    model_digest: str,
    prompt_version: str,
    prompt_sha256: str,
    cues: list[Cue],
    runtime_seconds: float | None,
    backup: Path | None,
    metrics: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "event": "generated-translation",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_path": str(source_path),
        "source_sha256": source_hash,
        "destination_path": str(destination),
        "sha256": output_hash,
        "provider": "ollama",
        "model": model,
        "model_digest": model_digest,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "source_language": "en",
        "target_language": "es-419",
        "cue_count": len(cues),
        "last_end_ms": cues[-1].end_ms,
        "runtime_seconds": runtime_seconds,
        "backup_path": str(backup) if backup else None,
        "chunk_metrics": metrics,
    }


def stage_translation(destination: Path, rendered: bytes) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        stage = Path(handle.name)
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(stage, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    return stage


def publish_translation(
    *,
    source_path: Path,
    expected_source_hash: str,
    source_cues: list[Cue],
    translated_cues: list[Cue],
    destination: Path,
    replace_existing: bool,
    backup_dir: Path | None,
    manifest: Path,
    model: str,
    model_digest: str,
    prompt_version: str,
    prompt_sha256: str,
    runtime_seconds: float | None,
    metrics: list[dict[str, object]],
) -> dict[str, object]:
    if source_path.resolve() == destination.resolve():
        raise ValidationError("source and destination paths must differ")
    if source_path.is_symlink() or destination.is_symlink():
        raise ValidationError("subtitle source and destination must not be symlinks")
    if destination.exists() and not replace_existing:
        raise ValidationError("destination already exists; replacement was not authorized")
    if destination.exists() and backup_dir is None:
        raise ValidationError("replacement requires a protected backup directory")
    if destination.exists():
        verify_generated_destination(destination)
    if not destination.parent.is_dir():
        raise ValidationError("destination directory does not exist")

    source_hash = file_sha256(source_path)
    if source_hash != expected_source_hash:
        raise ValidationError("source subtitle changed during translation")
    rendered = render_srt(translated_cues)
    output_hash = hashlib.sha256(rendered).hexdigest()
    backup = backup_destination(destination, backup_dir) if destination.exists() else None
    stage: Path | None = None
    published = False
    try:
        stage = stage_translation(destination, rendered)
        staged_cues = parse_srt(stage, runtime_seconds)
        validate_translation_quality(source_cues, staged_cues)
        set_provenance_xattrs(
            stage, source_hash=source_hash, output_hash=output_hash, model=model
        )
        os.replace(stage, destination)
        stage = None
        published = True
        fsync_directory(destination.parent)

        record = translation_record(
            source_path=source_path,
            destination=destination,
            source_hash=source_hash,
            output_hash=output_hash,
            model=model,
            model_digest=model_digest,
            prompt_version=prompt_version,
            prompt_sha256=prompt_sha256,
            cues=translated_cues,
            runtime_seconds=runtime_seconds,
            backup=backup,
            metrics=metrics,
        )
        append_manifest(manifest, record)
        return record
    except Exception:
        if published:
            restore_after_failure(destination, backup)
        raise
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)
