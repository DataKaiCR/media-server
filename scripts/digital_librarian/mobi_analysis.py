"""Bounded MOBI and AZW3 embedded metadata analysis."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .book_common import (
    MAX_METADATA_VALUE,
    bibliographic_evidence,
    clean_metadata,
)
from .model import Finding


MAX_MOBI_HEADER_BYTES = 4_194_304
MAX_EXTH_RECORDS = 10_000


def _mobi_text(value: bytes) -> str:
    for encoding in ("utf-8", "cp1252"):
        try:
            return clean_metadata(value.decode(encoding))
        except UnicodeDecodeError:
            continue
    return clean_metadata(value.decode("utf-8", errors="replace"))


def _mobi_header(path: Path) -> tuple[bytes, int, int, str, bool]:
    with path.open("rb") as handle:
        header = handle.read(MAX_MOBI_HEADER_BYTES)
    if len(header) < 86:
        raise ValueError
    record_offset = int.from_bytes(header[78:82], "big")
    mobi_start = record_offset + 16
    if (
        mobi_start + 132 > len(header)
        or header[mobi_start:mobi_start + 4] != b"MOBI"
    ):
        raise ValueError
    mobi_length = int.from_bytes(header[mobi_start + 4:mobi_start + 8], "big")
    full_name_offset = int.from_bytes(
        header[mobi_start + 84:mobi_start + 88], "big"
    )
    full_name_length = int.from_bytes(
        header[mobi_start + 88:mobi_start + 92], "big"
    )
    title_start = record_offset + full_name_offset
    title = _mobi_text(
        header[title_start:title_start + min(full_name_length, MAX_METADATA_VALUE)]
    )
    exth_flags = int.from_bytes(
        header[mobi_start + 128:mobi_start + 132], "big"
    )
    return header, mobi_start, mobi_length, title, bool(exth_flags & 0x40)


def _exth_records(
    header: bytes, mobi_start: int, mobi_length: int
) -> Iterator[tuple[int, str]]:
    position = mobi_start + mobi_length
    if header[position:position + 4] != b"EXTH":
        raise ValueError
    exth_length = int.from_bytes(header[position + 4:position + 8], "big")
    record_count = min(
        int.from_bytes(header[position + 8:position + 12], "big"),
        MAX_EXTH_RECORDS,
    )
    end = min(position + exth_length, len(header))
    position += 12
    for _ in range(record_count):
        if position + 8 > end:
            return
        record_type = int.from_bytes(header[position:position + 4], "big")
        record_length = int.from_bytes(
            header[position + 4:position + 8], "big"
        )
        if record_length < 8 or position + record_length > end:
            return
        maximum = position + min(record_length, 8 + MAX_METADATA_VALUE)
        yield record_type, _mobi_text(header[position + 8:maximum])
        position += record_length


def _incomplete(path: Path) -> tuple[dict[str, object], list[Finding]]:
    return {
        "bibliographic": bibliographic_evidence(filename=path.stem),
    }, [
        Finding(
            "mobi-metadata-incomplete", "warning",
            "MOBI metadata could not be read within safe bounds",
        )
    ]


def analyze_mobi(path: Path) -> tuple[dict[str, object], list[Finding]]:
    try:
        header, mobi_start, mobi_length, title, has_exth = _mobi_header(path)
        authors: list[str] = []
        identifiers: list[str] = []
        asin = ""
        records = _exth_records(header, mobi_start, mobi_length) if has_exth else []
        for record_type, value in records:
            if record_type == 100 and value:
                authors.append(value)
            if record_type == 104 and value:
                identifiers.append(value)
            if record_type in {113, 504} and value:
                asin = value
            if record_type == 503 and value:
                title = value
    except (OSError, ValueError, OverflowError):
        return _incomplete(path)
    return {
        "mobi": {"embedded_metadata": True},
        "bibliographic": bibliographic_evidence(
            titles=[title],
            creators=authors,
            identifier_values=identifiers,
            asin=asin,
            filename=path.stem,
        ),
    }, []
