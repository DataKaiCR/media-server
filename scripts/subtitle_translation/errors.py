"""Error types for the governed subtitle translation pipeline."""


class SubtitleError(Exception):
    """Base error for a rejected subtitle operation."""


class ValidationError(SubtitleError):
    """Input or generated output failed a safety invariant."""


class TranslationError(SubtitleError):
    """The translation API failed or returned unusable output."""
