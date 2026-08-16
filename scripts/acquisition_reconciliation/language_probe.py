"""Bounded local audio-stream evidence for imported movie files."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
import re
from typing import Any

from digital_librarian.bounded import run_bounded

from .language_config import LanguageVerificationConfig, PathMapping


MAX_STREAMS = 128
_LANGUAGE_TAG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$")
_LATINO_MARKER_RE = re.compile(
    r"(?:\blatino\b|\blatin american\b|\blatam\b|\bes[-_ ]?419\b|\bespa[nñ]ol latino\b)",
    re.IGNORECASE,
)
_CASTILIAN_MARKER_RE = re.compile(
    r"(?:\bcastilian\b|\bcastellano\b|\bespa[nñ]a\b|\bspain\b)", re.IGNORECASE
)
_LATINO_REGIONS = {
    "419",
    "ar",
    "bo",
    "br",
    "bz",
    "cl",
    "co",
    "cr",
    "cu",
    "do",
    "ec",
    "gt",
    "hn",
    "mx",
    "ni",
    "pa",
    "pe",
    "pr",
    "py",
    "sv",
    "us",
    "uy",
    "ve",
}


LANGUAGE_ALIASES = {
    "Arabic": {"ar", "ara"},
    "Bengali": {"bn", "ben"},
    "Bosnian": {"bs", "bos"},
    "Bulgarian": {"bg", "bul"},
    "Catalan": {"ca", "cat"},
    "Chinese": {"zh", "zho", "chi"},
    "Croatian": {"hr", "hrv"},
    "Czech": {"cs", "ces", "cze"},
    "Danish": {"da", "dan"},
    "Dutch": {"nl", "nld", "dut"},
    "English": {"en", "eng"},
    "Estonian": {"et", "est"},
    "Finnish": {"fi", "fin"},
    "Flemish": {"nl", "nld", "dut"},
    "French": {"fr", "fra", "fre"},
    "German": {"de", "deu", "ger"},
    "Greek": {"el", "ell", "gre"},
    "Hebrew": {"he", "heb"},
    "Hindi": {"hi", "hin"},
    "Hungarian": {"hu", "hun"},
    "Icelandic": {"is", "isl", "ice"},
    "Indonesian": {"id", "ind"},
    "Italian": {"it", "ita"},
    "Japanese": {"ja", "jpn"},
    "Kannada": {"kn", "kan"},
    "Korean": {"ko", "kor"},
    "Latvian": {"lv", "lav"},
    "Lithuanian": {"lt", "lit"},
    "Macedonian": {"mk", "mkd", "mac"},
    "Malayalam": {"ml", "mal"},
    "Norwegian": {"no", "nor"},
    "Persian": {"fa", "fas", "per"},
    "Polish": {"pl", "pol"},
    "Portuguese": {"pt", "por"},
    "Portuguese (Brazil)": {"pt", "por", "pt-br"},
    "Romanian": {"ro", "ron", "rum"},
    "Russian": {"ru", "rus"},
    "Serbian": {"sr", "srp"},
    "Slovak": {"sk", "slk", "slo"},
    "Slovenian": {"sl", "slv"},
    "Spanish": {"es", "spa"},
    "Spanish (Latino)": {"es-419"},
    "Swedish": {"sv", "swe"},
    "Tamil": {"ta", "tam"},
    "Telugu": {"te", "tel"},
    "Thai": {"th", "tha"},
    "Turkish": {"tr", "tur"},
    "Ukrainian": {"uk", "ukr"},
    "Vietnamese": {"vi", "vie"},
}


def _language_tag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold().replace("_", "-")
    return normalized if _LANGUAGE_TAG_RE.fullmatch(normalized) else None


def canonical_language(tag: str | None) -> str | None:
    if tag is None:
        return None
    for name, aliases in LANGUAGE_ALIASES.items():
        if tag in aliases:
            return name
        if any(tag.startswith(alias + "-") for alias in aliases if len(alias) == 2):
            return name
    return None


def _marker_text(tags: object) -> str:
    if not isinstance(tags, dict):
        return ""
    values = []
    for key in ("title", "handler_name"):
        value = tags.get(key)
        if isinstance(value, str):
            values.append(value[:256])
    return " ".join(values)


def _latino_region_tag(tag: str | None) -> bool:
    if tag is None:
        return False
    parts = tag.split("-")
    return parts[0] in {"es", "spa"} and any(part in _LATINO_REGIONS for part in parts[1:])


def _bounded_integer(value: object, maximum: int) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.isascii() and value.isdigit():
        result = int(value)
    else:
        return None
    return result if 0 <= result <= maximum else None


def _stream_evidence(stream: object) -> dict[str, Any] | None:
    if not isinstance(stream, dict) or stream.get("codec_type") != "audio":
        return None
    tags = stream.get("tags")
    language = _language_tag(tags.get("language") if isinstance(tags, dict) else None)
    marker_text = _marker_text(tags)
    explicit_latino = _latino_region_tag(language) or bool(_LATINO_MARKER_RE.search(marker_text))
    explicit_castilian = bool(_CASTILIAN_MARKER_RE.search(marker_text)) and not explicit_latino
    disposition = stream.get("disposition")
    return {
        "index": _bounded_integer(stream.get("index"), 4096),
        "language_tag": language,
        "canonical_language": canonical_language(language),
        "channels": _bounded_integer(stream.get("channels"), 128),
        "default": bool(disposition.get("default")) if isinstance(disposition, dict) else False,
        "explicit_latino_marker": explicit_latino,
        "explicit_castilian_marker": explicit_castilian,
    }


def map_media_path(radarr_path: object, mappings: tuple[PathMapping, ...]) -> Path:
    if not isinstance(radarr_path, str) or not radarr_path.startswith("/"):
        raise ValueError("Radarr media path is not an absolute POSIX path")
    source = PurePosixPath(radarr_path)
    for mapping in mappings:
        try:
            relative = source.relative_to(mapping.radarr_prefix)
        except ValueError:
            continue
        candidate = mapping.host_root.joinpath(*relative.parts)
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("mapped media path is not a regular non-symlink file")
        resolved = candidate.resolve()
        try:
            resolved.relative_to(mapping.host_root)
        except ValueError as error:
            raise ValueError("mapped media path escapes the configured host root") from error
        return resolved
    raise ValueError("Radarr media path has no configured host mapping")


def probe_audio(path: Path, config: LanguageVerificationConfig) -> dict[str, Any]:
    result = run_bounded(
        [
            str(config.ffprobe_command),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_entries",
            "stream=index,codec_type,channels:stream_tags=language,title,handler_name:"
            "stream_disposition=default",
            str(path),
        ],
        config.parser_timeout_seconds,
        config.max_parser_output_bytes,
        config.max_parser_memory_bytes,
    )
    if result.timed_out:
        return {"status": "timeout", "audio_stream_count": 0, "audio_streams": []}
    if result.output_limited:
        return {"status": "output_limit", "audio_stream_count": 0, "audio_streams": []}
    if result.unavailable:
        return {"status": "unavailable", "audio_stream_count": 0, "audio_streams": []}
    if result.returncode != 0:
        return {"status": "invalid_media", "audio_stream_count": 0, "audio_streams": []}
    try:
        payload = json.loads(result.stdout)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"status": "invalid_output", "audio_stream_count": 0, "audio_streams": []}
    streams = payload.get("streams") if isinstance(payload, dict) else None
    if not isinstance(streams, list):
        return {"status": "invalid_output", "audio_stream_count": 0, "audio_streams": []}
    if len(streams) > MAX_STREAMS:
        return {"status": "stream_limit", "audio_stream_count": 0, "audio_streams": []}
    audio = [evidence for stream in streams if (evidence := _stream_evidence(stream))]
    return {
        "status": "ok" if audio else "no_audio",
        "audio_stream_count": len(audio),
        "audio_streams": audio,
    }
