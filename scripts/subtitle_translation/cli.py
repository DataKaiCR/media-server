"""Command-line entry point for governed subtitle translation."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time

from .ollama import OllamaClient, PROMPT_SHA256, PROMPT_VERSION, translate_cues
from .publication import file_sha256, publish_translation
from .srt import parse_srt


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Translate a validated English SRT to neutral Latin American Spanish"
    )
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="translategemma:12b")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--runtime-seconds", type=float)
    parser.add_argument("--replace", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--max-cues", type=int, default=24)
    parser.add_argument("--max-chars", type=int, default=3500)
    parser.add_argument("--context-cues", type=int, default=2)
    parser.add_argument("--retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--keep-alive", default="10m")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.runtime_seconds is not None and (
        not math.isfinite(args.runtime_seconds) or args.runtime_seconds <= 0
    ):
        raise SystemExit("--runtime-seconds must be finite and positive")
    if args.retries <= 0:
        raise SystemExit("--retries must be positive")

    source_cues = parse_srt(args.source, args.runtime_seconds)
    source_hash = file_sha256(args.source)
    if args.destination.exists() and not args.replace:
        raise SystemExit("destination exists; use --replace with --backup-dir to authorize repair")
    if args.destination.exists() and args.backup_dir is None:
        raise SystemExit("--backup-dir is required when replacing an existing destination")

    client = OllamaClient(args.ollama_url, args.timeout)
    started = time.monotonic()
    digest = client.model_digest(args.model)
    try:
        translated, metrics = translate_cues(
            client,
            args.model,
            source_cues,
            max_cues=args.max_cues,
            max_chars=args.max_chars,
            context_cues=args.context_cues,
            retries=args.retries,
            retry_delay=args.retry_delay,
            seed=args.seed,
            keep_alive=args.keep_alive,
            progress=lambda current, total: print(
                f"translated chunk {current}/{total}", file=sys.stderr
            ),
        )
        if client.model_digest(args.model) != digest:
            raise SystemExit("installed model changed during translation; output was not published")
    finally:
        client.unload(args.model)

    record = publish_translation(
        source_path=args.source,
        expected_source_hash=source_hash,
        source_cues=source_cues,
        translated_cues=translated,
        destination=args.destination,
        replace_existing=args.replace,
        backup_dir=args.backup_dir,
        manifest=args.manifest,
        model=args.model,
        model_digest=digest,
        prompt_version=PROMPT_VERSION,
        prompt_sha256=PROMPT_SHA256,
        runtime_seconds=args.runtime_seconds,
        metrics=metrics,
    )
    print(
        json.dumps(
            {
                "status": "published",
                "cue_count": record["cue_count"],
                "sha256": record["sha256"],
                "elapsed_seconds": round(time.monotonic() - started, 2),
            },
            sort_keys=True,
        )
    )
    return 0
