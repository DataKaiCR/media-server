"""Bounded PDF metadata and text-layer quality analysis."""

from __future__ import annotations

from pathlib import Path
import shutil
import statistics

from .book_common import bibliographic_evidence, clean_metadata
from .bounded import BoundedProcessResult, run_bounded
from .config import BookAnalysisConfig
from .model import Finding


PDFINFO_FIELDS = {
    "Title", "Author", "Subject", "Keywords", "Creator", "Producer",
    "CreationDate", "ModDate", "Pages", "Encrypted", "PDF version",
}


def _parse_pdfinfo(output: bytes) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.decode("utf-8", errors="replace").splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key in PDFINFO_FIELDS and (cleaned := clean_metadata(value)):
            parsed[key] = cleaned
    return parsed


def _text_failure(result: BoundedProcessResult) -> Finding | None:
    if result.timed_out:
        return Finding(
            "pdf-text-analysis-timeout", "warning",
            "Bounded PDF text-layer analysis timed out",
        )
    if result.output_limited:
        return Finding(
            "pdf-text-output-limited", "warning",
            "PDF text-layer output exceeded the privacy-safe analysis bound",
        )
    if result.unavailable or result.returncode != 0:
        return Finding(
            "pdf-text-analysis-incomplete", "warning",
            "PDF text layer could not be analyzed",
        )
    return None


def _text_density(
    output: bytes, sampled_pages: int
) -> tuple[dict[str, object], list[Finding]]:
    text = output.decode("utf-8", errors="ignore")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    pages = (pages + [""] * sampled_pages)[:sampled_pages]
    counts = [sum(character.isalnum() for character in page) for page in pages]
    sparse = sum(count < 40 for count in counts)
    low_density = sum(count < 200 for count in counts)
    if sparse / sampled_pages >= 0.8:
        recommendation = "recommended"
    elif low_density / sampled_pages >= 0.4:
        recommendation = "review"
    else:
        recommendation = "not-indicated"
    evidence: dict[str, object] = {
        "sampled_pages": sampled_pages,
        "sample_strategy": "leading-pages",
        "sampled_sparse_text_pages": sparse,
        "sampled_low_text_pages": low_density,
        "median_alphanumeric_characters_per_page": int(statistics.median(counts)),
        "ocr_recommendation": recommendation,
    }
    if recommendation == "recommended":
        finding = Finding(
            "pdf-ocr-recommended", "warning",
            "Sampled pages have little or no text-layer evidence",
            evidence={"sampled_pages": sampled_pages, "sparse_pages": sparse},
        )
        return evidence, [finding]
    if recommendation == "review":
        finding = Finding(
            "pdf-low-text-density", "info",
            "Some sampled pages have a sparse text layer",
            evidence={
                "sampled_pages": sampled_pages,
                "low_text_pages": low_density,
            },
        )
        return evidence, [finding]
    return evidence, []


def _pdf_text_evidence(
    path: Path, page_count: int, settings: BookAnalysisConfig
) -> tuple[dict[str, object], list[Finding]]:
    executable = shutil.which("pdftotext")
    if executable is None or not settings.pdf_text_layer or page_count < 1:
        return {}, []
    sampled_pages = min(page_count, settings.pdf_sample_pages)
    result = run_bounded(
        [
            executable,
            "-f", "1",
            "-l", str(sampled_pages),
            "-enc", "UTF-8",
            str(path), "-",
        ],
        settings.parser_timeout_seconds,
        settings.max_parser_output_bytes,
        settings.max_parser_memory_bytes,
    )
    if failure := _text_failure(result):
        return {}, [failure]
    return _text_density(result.stdout, sampled_pages)


def _validation_failure(
    result: BoundedProcessResult, encrypted: bool
) -> Finding | None:
    if result.timed_out:
        return Finding(
            "pdf-validation-incomplete", "warning",
            "Bounded PDF parser timed out",
        )
    if result.output_limited:
        return Finding(
            "pdf-validation-incomplete", "warning",
            "PDF parser output exceeded its analysis bound",
        )
    if encrypted:
        return Finding(
            "pdf-encrypted", "info",
            "PDF is encrypted; no circumvention is attempted",
        )
    if result.unavailable or result.returncode != 0:
        return Finding(
            "pdf-parse-failed", "error",
            "Independent PDF parser rejected the document",
        )
    return None


def _metadata(
    path: Path, parsed: dict[str, str], encrypted: bool, page_count: int
) -> tuple[dict[str, object], dict[str, object]]:
    title = parsed.get("Title", "")
    author = parsed.get("Author", "")
    pdf_metadata: dict[str, object] = {
        "page_count": page_count,
        "encrypted": encrypted,
        "version": parsed.get("PDF version"),
        "metadata_presence": {
            "title": bool(title),
            "author": bool(author),
            "creation_date": "CreationDate" in parsed,
            "modified_date": "ModDate" in parsed,
        },
    }
    metadata: dict[str, object] = {
        "pdf": pdf_metadata,
        "bibliographic": bibliographic_evidence(
            titles=[title],
            creators=[author],
            identifier_values=[
                parsed.get("Subject", ""), parsed.get("Keywords", "")
            ],
            filename=path.stem,
        ),
    }
    return metadata, pdf_metadata


def analyze_pdf(
    path: Path, settings: BookAnalysisConfig
) -> tuple[dict[str, object], list[Finding]]:
    executable = shutil.which("pdfinfo")
    if executable is None:
        return {}, []
    result = run_bounded(
        [executable, str(path)],
        settings.parser_timeout_seconds,
        settings.max_parser_output_bytes,
        settings.max_parser_memory_bytes,
    )
    parsed = _parse_pdfinfo(result.stdout)
    error = result.stderr.decode("utf-8", errors="ignore").casefold()
    encrypted = (
        parsed.get("Encrypted", "").casefold().startswith("yes")
        or "password" in error
        or "encrypted" in error
    )
    failure = _validation_failure(result, encrypted)
    if failure and failure.code not in {"pdf-encrypted"}:
        return {}, [failure]
    try:
        page_count = max(0, int(parsed.get("Pages", "0")))
    except ValueError:
        page_count = 0
    metadata, pdf_metadata = _metadata(path, parsed, encrypted, page_count)
    findings = [failure] if failure is not None else []
    if not encrypted and result.returncode == 0:
        text_evidence, text_findings = _pdf_text_evidence(
            path, page_count, settings
        )
        if text_evidence:
            pdf_metadata["text_layer"] = text_evidence
        findings.extend(text_findings)
    return metadata, findings
