"""Structural and heuristic checks for translated subtitle text."""

from __future__ import annotations

from collections import Counter
import json
import re
from typing import Iterable

from .errors import ValidationError
from .srt import Cue


WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
TAG_RE = re.compile(r"<[^>]+>|\{\\[^}]+\}")
ENGLISH_MARKERS = {
    "about", "after", "again", "all", "and", "are", "because", "been",
    "before", "being", "but", "can", "could", "did", "didn't", "does",
    "don't", "from", "get", "give", "going", "good", "got", "had", "has",
    "have", "he", "her", "here", "him", "his", "how", "i'm", "if", "into",
    "is", "isn't", "it", "it's", "just", "know", "let", "like", "look",
    "make", "my", "need", "not", "now", "of", "okay", "one", "out",
    "right", "said", "she", "should", "some", "that", "that's", "the",
    "their", "them", "then", "there", "they", "this", "time", "to", "up",
    "want", "was", "we", "we're", "well", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "you're", "your",
}
SPANISH_MARKERS = {
    "ahora", "algo", "alguien", "antes", "aquí", "bien", "cada", "como",
    "cómo", "con", "cuando", "cuándo", "de", "decir", "dónde", "el", "ella",
    "ellos", "en", "entonces", "eres", "es", "esa", "ese", "eso", "esta",
    "está", "estaba", "este", "esto", "hacer", "hasta", "hay", "la", "las",
    "le", "les", "lo", "los", "más", "mi", "mientras", "muy", "necesito",
    "nos", "nunca", "para", "pero", "por", "porque", "puede", "qué", "quien",
    "quién", "se", "ser", "si", "sí", "sin", "solo", "también", "te", "tengo",
    "tiene", "todo", "tu", "una", "uno", "usted", "ustedes", "vamos", "verdad",
    "ya", "yo",
}
BANNED_CASTILIAN = {
    "vosotros", "vosotras", "sois", "estáis", "tenéis", "habéis", "hacéis",
    "podéis", "queréis", "sabéis", "debéis", "venid", "mirad", "decid",
    "escuchad",
}
COMMON_CAPITALIZED_WORDS = {
    "A", "An", "And", "Are", "At", "But", "Can", "Could", "Did", "Do",
    "Does", "Don't", "Dr", "For", "From", "God", "God's", "Good", "He", "Hello",
    "Her", "Hey", "Hi",
    "His", "How", "I", "If", "I'll", "I'm", "I've", "In", "Is", "It",
    "It's", "Leave", "Let's", "Listen", "Look", "Me", "Miss", "Mr", "Mrs",
    "Ms", "My", "No", "Now", "Okay", "Our", "Please", "Right", "She",
    "Should", "Sit",
    "So", "Take", "That", "The", "Their", "Then", "There", "They", "This",
    "To", "We", "We're", "Well", "What", "What's", "When", "Where", "Who",
    "Who's", "Whose", "Why", "Will", "With", "Would", "Yes", "You", "You're",
    "Your",
}
COMMON_CAPITALIZED_WORDS = {word.casefold() for word in COMMON_CAPITALIZED_WORDS}
CAPITALIZED_RE = re.compile(r"\b[A-Z][A-Za-zÀ-ÖØ-öø-ÿ'’.-]*\b")


def response_schema(expected_count: int) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "translations": {
                "type": "array",
                "minItems": expected_count,
                "maxItems": expected_count,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "text": {"type": "string", "minLength": 1},
                    },
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["translations"],
        "additionalProperties": False,
    }


def formatting_tokens(text: str) -> Counter[str]:
    return Counter(TAG_RE.findall(text))


def outer_formatting_parts(text: str) -> tuple[str, str, str] | None:
    source_tokens = TAG_RE.findall(text)
    if not source_tokens:
        return None
    leading_match = re.match(r"^(?P<tags>(?:<[^>]+>|\{\\[^}]+\})+)", text)
    trailing_match = re.search(r"(?P<tags>(?:<[^>]+>)+)$", text)
    leading = leading_match.group("tags") if leading_match else ""
    trailing = trailing_match.group("tags") if trailing_match else ""
    if TAG_RE.findall(leading + trailing) != source_tokens:
        return None
    body = text[len(leading):len(text) - len(trailing) if trailing else None]
    return leading, body, trailing


def strip_outer_formatting(text: str) -> str:
    parts = outer_formatting_parts(text)
    return parts[1] if parts is not None else text


def restore_outer_formatting(source: str, translated: str) -> str:
    parts = outer_formatting_parts(source)
    if parts is None or TAG_RE.search(translated):
        return translated
    leading, _, trailing = parts
    return leading + translated + trailing


def protected_names(text: str) -> list[tuple[int, int, str]]:
    tokens = [match for match in CAPITALIZED_RE.finditer(text) if match.group() != "I"]
    selected: list[tuple[int, int, str]] = []
    consumed: set[int] = set()
    for index in range(len(tokens) - 1):
        if index in consumed or index + 1 in consumed:
            continue
        left, right = tokens[index], tokens[index + 1]
        if (
            text[left.end():right.start()].strip() == ""
            and not {left.group().casefold(), right.group().casefold()}
            & COMMON_CAPITALIZED_WORDS
            and not (left.group().isupper() and right.group().isupper())
        ):
            start = left.start()
            end = right.end()
            cursor = index + 1
            while cursor + 1 < len(tokens):
                following = tokens[cursor + 1]
                if (
                    text[tokens[cursor].end():following.start()].strip() == ""
                    and following.group().casefold() not in COMMON_CAPITALIZED_WORDS
                ):
                    end = following.end()
                    consumed.add(cursor + 1)
                    cursor += 1
                else:
                    break
            selected.append((start, end, text[start:end]))
            consumed.update({index, index + 1})
    for index, token in enumerate(tokens):
        if index in consumed or token.group().casefold() in COMMON_CAPITALIZED_WORDS:
            continue
        prefix = text[:token.start()]
        sentence_prefix = re.split(r"[.?!\n]", prefix)[-1]
        at_sentence_start = not any(char.isalpha() for char in sentence_prefix)
        if not at_sentence_start and not token.group().isupper():
            selected.append((token.start(), token.end(), token.group()))
    selected.sort()
    return selected


def protect_proper_names(text: str) -> tuple[str, list[tuple[str, str]]]:
    names = protected_names(text)
    replacements = [(f"__PN{index}__", name) for index, (_, _, name) in enumerate(names)]
    protected = text
    for (start, end, _), (placeholder, _) in reversed(list(zip(names, replacements))):
        protected = protected[:start] + placeholder + protected[end:]
    return protected, replacements


def restore_proper_names(source: str, translated: str) -> str:
    _, replacements = protect_proper_names(source)
    restored = translated
    for placeholder, name in replacements:
        if restored.count(placeholder) != 1:
            raise ValidationError("translation changed or omitted a protected proper name")
        restored = restored.replace(placeholder, name)
    return restored


def validate_cue_translation(source: Cue, translated: str) -> str:
    text = translated.replace("\r\n", "\n").replace("\r", "\n").strip()
    source_without_outer_tags = strip_outer_formatting(source.text)
    text = restore_proper_names(source_without_outer_tags, text)
    text = restore_outer_formatting(source.text, text)
    if not text:
        raise ValidationError(f"translation for cue {source.index} is empty")
    if "-->" in text or re.search(r"\d{2}:\d{2}:\d{2},\d{3}", text):
        raise ValidationError(f"translation for cue {source.index} contains a timestamp")
    if formatting_tokens(source.text) != formatting_tokens(text):
        raise ValidationError(f"translation for cue {source.index} changed formatting tags")
    for opening, closing in (("[", "]"), ("(", ")")):
        source_wrapped = source.text.strip().startswith(opening) and source.text.strip().endswith(closing)
        translated_wrapped = text.startswith(opening) and text.endswith(closing)
        if source_wrapped != translated_wrapped:
            raise ValidationError(f"translation for cue {source.index} changed effect delimiters")
    if source.text.count("♪") != text.count("♪"):
        raise ValidationError(f"translation for cue {source.index} changed music-note markers")
    translated_words = {word.casefold() for word in WORD_RE.findall(text)}
    if translated_words & BANNED_CASTILIAN:
        raise ValidationError(
            f"translation for cue {source.index} uses Castilian second-person plural wording"
        )
    return text


def parse_translation_response(content: str, sources: tuple[Cue, ...]) -> list[str]:
    try:
        data = json.loads(content)
    except json.JSONDecodeError as error:
        raise ValidationError("model response is not valid JSON") from error
    if not isinstance(data, dict) or set(data) != {"translations"}:
        raise ValidationError("model response has an unexpected top-level structure")
    rows = data["translations"]
    if not isinstance(rows, list) or len(rows) != len(sources):
        raise ValidationError("model response changed the cue count")

    expected_ids = [cue.index for cue in sources]
    actual_ids: list[int] = []
    translated: list[str] = []
    for row, source in zip(rows, sources):
        if not isinstance(row, dict) or set(row) != {"id", "text"}:
            raise ValidationError("model response has a malformed translation row")
        if isinstance(row["id"], bool) or not isinstance(row["id"], int):
            raise ValidationError("model response has a non-integer cue id")
        if not isinstance(row["text"], str):
            raise ValidationError("model response has non-text content")
        actual_ids.append(row["id"])
        translated.append(validate_cue_translation(source, row["text"]))
    if actual_ids != expected_ids:
        raise ValidationError("model response changed, duplicated, or reordered cue ids")
    return translated


def words(text: str) -> list[str]:
    return [word.casefold().replace("’", "'") for word in WORD_RE.findall(text)]


def marker_count(tokens: Iterable[str], markers: set[str]) -> int:
    return sum(1 for token in tokens if token in markers)


def validate_source_language(cues: list[Cue]) -> None:
    tokens = words("\n".join(cue.text for cue in cues))
    if len(tokens) < 100:
        return
    english = marker_count(tokens, ENGLISH_MARKERS)
    spanish = marker_count(tokens, SPANISH_MARKERS)
    if english < 12 or english <= spanish * 1.25:
        raise ValidationError("source subtitle does not appear to be English")


def validate_translation_quality(source: list[Cue], translated: list[Cue]) -> None:
    if len(source) != len(translated) or not translated:
        raise ValidationError("translated cue count differs from source")
    if [(c.index, c.timestamp) for c in source] != [
        (c.index, c.timestamp) for c in translated
    ]:
        raise ValidationError("translated cue identifiers or timestamps changed")

    source_letters = sum(char.isalpha() for cue in source for char in cue.text)
    target_letters = sum(char.isalpha() for cue in translated for char in cue.text)
    if source_letters == 0 or target_letters == 0:
        raise ValidationError("subtitle lacks translatable text")
    ratio = target_letters / source_letters
    if ratio < 0.45:
        raise ValidationError("translation appears truncated")
    if ratio > 2.5:
        raise ValidationError("translation is suspiciously expanded")

    meaningful_chars = 0
    unchanged_chars = 0
    for original, result in zip(source, translated):
        source_tokens = words(original.text)
        normalized_source = " ".join(source_tokens)
        normalized_result = " ".join(words(result.text))
        if len(source_tokens) >= 3 and marker_count(source_tokens, ENGLISH_MARKERS) > 0:
            weight = max(1, len(normalized_source))
            meaningful_chars += weight
            if normalized_source == normalized_result:
                unchanged_chars += weight
    if meaningful_chars and unchanged_chars / meaningful_chars > 0.45:
        raise ValidationError("too much source dialogue remains untranslated")

    source_tokens = words("\n".join(cue.text for cue in source))
    target_tokens = words("\n".join(cue.text for cue in translated))
    source_english = marker_count(source_tokens, ENGLISH_MARKERS)
    source_spanish = marker_count(source_tokens, SPANISH_MARKERS)
    if source_english >= 12 and source_english > source_spanish * 1.25:
        target_english = marker_count(target_tokens, ENGLISH_MARKERS)
        target_spanish = marker_count(target_tokens, SPANISH_MARKERS)
        if target_spanish < 8 or target_spanish <= target_english:
            raise ValidationError("output does not appear to be Spanish")
