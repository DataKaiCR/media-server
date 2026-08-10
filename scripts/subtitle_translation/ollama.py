"""Chunking and deterministic structured translation through Ollama."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import ipaddress
import json
import math
import time
from typing import Callable, Protocol
import urllib.error
import urllib.parse
import urllib.request

from .errors import TranslationError, ValidationError
from .srt import Cue
from .validation import (
    parse_translation_response,
    protect_proper_names,
    response_schema,
    strip_outer_formatting,
    validate_source_language,
    validate_translation_quality,
)


PROMPT_VERSION = "neutral-latam-v4"
SYSTEM_PROMPT = """You are a professional audiovisual subtitle translator.
Translate English dialogue into concise, natural, neutral Latin American Spanish.
Infer singular versus plural and tú versus usted from surrounding cues. Use
ustedes only for plural you; never use vosotros or vosotras. Avoid Castilian-only
vocabulary and conjugations. Preserve meaning, tone, proper names, fictional
place names, titles, punctuation, line-break intent, any formatting tags that
remain in the input, music notes, and brackets around sound effects. Outer
formatting wrappers may be removed and restored by the pipeline. Tokens such as
__PN0__ are protected proper names and must be reproduced exactly. Render
bracketed sound effects as concise idiomatic Spanish (for example, [door slams]
as [portazo]). Do not add explanations, censorship, timestamps, or cue
identifiers to translated text. Return only the requested JSON with exactly one
non-empty translation for every input id, in the same order."""
PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode("utf-8")).hexdigest()
MAX_OLLAMA_RESPONSE_BYTES = 8_388_608


@dataclass(frozen=True)
class Chunk:
    start: int
    end: int
    context_before: tuple[Cue, ...]
    cues: tuple[Cue, ...]
    context_after: tuple[Cue, ...]


def make_chunks(
    cues: list[Cue], max_cues: int, max_chars: int, context_cues: int
) -> list[Chunk]:
    if max_cues <= 0 or max_chars <= 0 or context_cues < 0:
        raise ValueError("chunk settings must be positive")
    chunks: list[Chunk] = []
    start = 0
    while start < len(cues):
        end = start
        chars = 0
        while end < len(cues) and end - start < max_cues:
            candidate = len(cues[end].text)
            if end > start and chars + candidate > max_chars:
                break
            chars += candidate
            end += 1
        chunks.append(
            Chunk(
                start=start,
                end=end,
                context_before=tuple(cues[max(0, start - context_cues):start]),
                cues=tuple(cues[start:end]),
                context_after=tuple(cues[end:min(len(cues), end + context_cues)]),
            )
        )
        start = end
    return chunks


def cue_payload(cue: Cue) -> dict[str, object]:
    # Fully outer tags are deterministic presentation metadata. Keeping them
    # out of the model prompt prevents malformed tag generation; validation
    # restores the exact source wrappers afterward.
    text = strip_outer_formatting(cue.text)
    protected, _ = protect_proper_names(text)
    return {"id": cue.index, "text": protected}


def _loopback_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    try:
        parsed.port
    except ValueError as error:
        raise ValueError("Ollama URL contains an invalid port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Ollama URL must be a loopback HTTP(S) origin")
    try:
        loopback = parsed.hostname == "localhost" or ipaddress.ip_address(
            parsed.hostname
        ).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("Ollama URL must use a loopback host")
    return value.rstrip("/")


class _ReadableResponse(Protocol):
    def read(self, amount: int = -1) -> bytes: ...


def _response_json(response: _ReadableResponse) -> object:
    raw = response.read(MAX_OLLAMA_RESPONSE_BYTES + 1)
    if len(raw) > MAX_OLLAMA_RESPONSE_BYTES:
        raise TranslationError("Ollama response exceeded the safety limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise TranslationError("Ollama returned invalid JSON") from error


class OllamaClient:
    def __init__(self, base_url: str, timeout: float = 600) -> None:
        if (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or not 0 < timeout <= 3600
        ):
            raise ValueError("Ollama timeout must be finite and between 0 and 3600")
        self.base_url = _loopback_origin(base_url)
        self.timeout = timeout

    def _request(self, endpoint: str, payload: dict[str, object]) -> dict[str, object]:
        request = urllib.request.Request(
            self.base_url + endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                result = _response_json(response)
        except (urllib.error.URLError, TimeoutError) as error:
            raise TranslationError(f"Ollama request failed: {type(error).__name__}") from error
        if not isinstance(result, dict):
            raise TranslationError("Ollama returned an invalid response envelope")
        if result.get("error"):
            raise TranslationError("Ollama rejected the translation request")
        return result

    def model_digest(self, model: str) -> str:
        request = urllib.request.Request(self.base_url + "/api/tags")
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 30)) as response:
                result = _response_json(response)
        except (urllib.error.URLError, TimeoutError) as error:
            raise TranslationError("cannot query installed Ollama models") from error
        if not isinstance(result, dict) or not isinstance(result.get("models"), list):
            raise TranslationError("Ollama returned an invalid model inventory")
        for row in result["models"]:
            if not isinstance(row, dict):
                raise TranslationError("Ollama returned an invalid model inventory")
            if row.get("name") == model or row.get("model") == model:
                digest = row.get("digest")
                if (
                    isinstance(digest, str)
                    and 1 <= len(digest) <= 128
                    and all(char.isalnum() or char in ":._+-" for char in digest)
                ):
                    return digest
                raise TranslationError("Ollama returned an invalid model digest")
        raise TranslationError(f"Ollama model is not installed: {model}")

    def translate_chunk(
        self,
        model: str,
        chunk: Chunk,
        seed: int,
        keep_alive: str,
        prior_error: str | None = None,
    ) -> tuple[list[str], dict[str, object]]:
        body = {
            "context_before": [cue_payload(cue) for cue in chunk.context_before],
            "cues_to_translate": [cue_payload(cue) for cue in chunk.cues],
            "context_after": [cue_payload(cue) for cue in chunk.context_after],
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(body, ensure_ascii=False)},
        ]
        if prior_error is not None:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "A prior deterministic attempt was invalid because: "
                        f"{prior_error}. Return corrected JSON only."
                    ),
                }
            )
        payload: dict[str, object] = {
            "model": model,
            "stream": False,
            "messages": messages,
            "format": response_schema(len(chunk.cues)),
            "options": {
                "temperature": 0,
                "seed": seed,
                "num_ctx": 8192,
                "num_predict": 4096,
            },
            "keep_alive": keep_alive,
        }
        result = self._request("/api/chat", payload)
        message = result.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str):
            raise TranslationError("Ollama response has no assistant content")
        translations = parse_translation_response(content, chunk.cues)
        metrics = {
            "prompt_tokens": result.get("prompt_eval_count"),
            "output_tokens": result.get("eval_count"),
            "load_duration_ns": result.get("load_duration"),
            "eval_duration_ns": result.get("eval_duration"),
        }
        return translations, metrics

    def unload(self, model: str) -> None:
        self._request(
            "/api/generate",
            {"model": model, "prompt": "", "stream": False, "keep_alive": 0},
        )


def translate_cues(
    client: OllamaClient,
    model: str,
    cues: list[Cue],
    *,
    max_cues: int,
    max_chars: int,
    context_cues: int,
    retries: int,
    retry_delay: float,
    seed: int,
    keep_alive: str,
    progress: Callable[[int, int], None] | None = None,
) -> tuple[list[Cue], list[dict[str, object]]]:
    if retries <= 0 or retry_delay < 0 or not math.isfinite(retry_delay):
        raise ValueError("retry settings must be finite and nonnegative")
    validate_source_language(cues)
    chunks = make_chunks(cues, max_cues, max_chars, context_cues)
    translated: list[Cue] = []
    all_metrics: list[dict[str, object]] = []
    for chunk_number, chunk in enumerate(chunks, start=1):
        prior_error: str | None = None
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                texts, metrics = client.translate_chunk(
                    model, chunk, seed + attempt - 1, keep_alive, prior_error
                )
                translated.extend(
                    replace(source, text=text)
                    for source, text in zip(chunk.cues, texts)
                )
                metrics.update({"chunk": chunk_number, "attempt": attempt})
                all_metrics.append(metrics)
                last_error = None
                break
            except (TranslationError, ValidationError) as error:
                last_error = error
                prior_error = str(error)
                if attempt < retries and retry_delay:
                    time.sleep(retry_delay * attempt)
        if last_error is not None:
            raise TranslationError(
                f"chunk {chunk_number} failed validation after {retries} attempts: {last_error}"
            ) from last_error
        if progress is not None:
            progress(chunk_number, len(chunks))
    validate_translation_quality(cues, translated)
    return translated, all_metrics
