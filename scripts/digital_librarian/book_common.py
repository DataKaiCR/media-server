"""Shared bounds and bibliographic helpers for private book analysis."""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import resource
import signal
import subprocess
import tempfile


MAX_METADATA_VALUE = 512


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limited: bool = False
    unavailable: bool = False


def _process_limits(maximum_bytes: int, maximum_memory_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_bytes, maximum_bytes))
    resource.setrlimit(
        resource.RLIMIT_AS, (maximum_memory_bytes, maximum_memory_bytes)
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_bounded(
    command: list[str],
    timeout_seconds: int | float,
    maximum_bytes: int,
    maximum_memory_bytes: int = 1_073_741_824,
) -> BoundedProcessResult:
    """Run a parser with bounded time and regular-file output."""
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
                preexec_fn=lambda: _process_limits(
                    maximum_bytes, maximum_memory_bytes
                ),
            )
        except OSError:
            return BoundedProcessResult(None, b"", b"", unavailable=True)
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait()
        stdout.seek(0, os.SEEK_END)
        stdout_size = stdout.tell()
        stderr.seek(0, os.SEEK_END)
        stderr_size = stderr.tell()
        stdout.seek(0)
        stderr.seek(0)
        return BoundedProcessResult(
            returncode,
            stdout.read(maximum_bytes),
            stderr.read(maximum_bytes),
            timed_out=timed_out,
            output_limited=(
                stdout_size >= maximum_bytes
                or stderr_size >= maximum_bytes
                or returncode == -signal.SIGXFSZ
            ),
        )


def clean_metadata(value: object, maximum: int = MAX_METADATA_VALUE) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.replace("\x00", "").split())[:maximum]


def _isbn13_check_digit(first_twelve: str) -> str:
    total = sum(
        (1 if index % 2 == 0 else 3) * int(character)
        for index, character in enumerate(first_twelve)
    )
    return str((10 - total % 10) % 10)


def normalize_isbn(value: str) -> str | None:
    candidate = re.sub(r"[^0-9Xx]", "", value)
    if len(candidate) == 10:
        if not candidate[:9].isdigit() or candidate[-1] not in "0123456789Xx":
            return None
        expected = sum(
            (10 - index) * int(character)
            for index, character in enumerate(candidate[:9])
        )
        expected += 10 if candidate[-1].casefold() == "x" else int(candidate[-1])
        if expected % 11:
            return None
        first_twelve = "978" + candidate[:9]
        return first_twelve + _isbn13_check_digit(first_twelve)
    if (
        len(candidate) == 13
        and candidate.isdigit()
        and candidate[:3] in {"978", "979"}
        and candidate[-1] == _isbn13_check_digit(candidate[:12])
    ):
        return candidate
    return None


def extract_isbns(values: list[str]) -> list[str]:
    identifiers: set[str] = set()
    for value in values:
        for candidate in re.findall(
            r"(?<!\d)(?:97[89][\d\s-]{10,20}|\d[\d\s-]{7,16}[\dXx])(?!\d)",
            value,
        ):
            normalized = normalize_isbn(candidate)
            if normalized is not None:
                identifiers.add(normalized)
    return sorted(identifiers)


def bibliographic_evidence(
    *,
    titles: list[str] | None = None,
    creators: list[str] | None = None,
    languages: list[str] | None = None,
    identifier_values: list[str] | None = None,
    series: str = "",
    volume: str = "",
    asin: str = "",
    filename: str = "",
) -> dict[str, object]:
    clean_titles = [
        cleaned for value in titles or [] if (cleaned := clean_metadata(value))
    ]
    clean_creators = [
        cleaned for value in creators or [] if (cleaned := clean_metadata(value))
    ]
    clean_languages = [
        cleaned
        for value in languages or []
        if (cleaned := clean_metadata(value, 64))
    ]
    isbns = extract_isbns(
        (identifier_values or []) + ([filename] if filename else [])
    )
    identifiers: list[dict[str, str]] = [
        {"scheme": "isbn-13", "value": isbn} for isbn in isbns
    ]
    if cleaned_asin := clean_metadata(asin, 64):
        identifiers.append({"scheme": "asin", "value": cleaned_asin})
    result: dict[str, object] = {
        "titles": list(dict.fromkeys(clean_titles))[:8],
        "creators": list(dict.fromkeys(clean_creators))[:16],
        "languages": list(dict.fromkeys(clean_languages))[:8],
        "identifiers": identifiers[:16],
    }
    if cleaned_series := clean_metadata(series):
        result["series"] = cleaned_series
    if cleaned_volume := clean_metadata(volume, 64):
        result["volume"] = cleaned_volume
    return result
