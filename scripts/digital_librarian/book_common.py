"""Shared bounds and bibliographic helpers for private book analysis."""

from __future__ import annotations

import re


MAX_METADATA_VALUE = 512


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
