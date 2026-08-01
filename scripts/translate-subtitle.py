#!/usr/bin/env python3
"""Translate a validated English SRT to neutral Latin American Spanish."""

from subtitle_translation.cli import main
from subtitle_translation.errors import SubtitleError


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SubtitleError as error:
        raise SystemExit(f"translation rejected: {error}") from error
