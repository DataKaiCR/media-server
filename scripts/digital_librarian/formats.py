"""Dependency-free format identification and shallow integrity checks."""

from __future__ import annotations

from pathlib import Path
import shutil
import struct
import subprocess
import zipfile

from .model import Finding


PHOTO_EXTENSIONS = {
    ".aae", ".arw", ".bmp", ".cr2", ".cr3", ".dng", ".gif", ".heic",
    ".heif", ".jpeg", ".jpg", ".mov", ".mp4", ".nef", ".orf", ".png",
    ".raf", ".rw2", ".tif", ".tiff", ".webp", ".xmp",
}
BOOK_EXTENSIONS = {
    ".azw3", ".cbr", ".cbz", ".djvu", ".doc", ".docx", ".epub", ".gif",
    ".htm", ".html", ".jpeg", ".jpg", ".m4b", ".md", ".mobi", ".mp3",
    ".nfo", ".odt", ".pdf", ".png", ".pps", ".ppt", ".pptx", ".rar",
    ".rtf", ".txt", ".webp", ".xls", ".xlsx", ".zip",
}
AV_EXTENSIONS = {
    ".avi", ".flac", ".m4a", ".m4v", ".mkv", ".mov", ".mp3", ".mp4",
    ".ogg", ".srt", ".vtt", ".wav", ".webm",
}
EXPECTED_FORMATS = {
    ".bmp": {"bmp"},
    ".cbr": {"rar"},
    ".cbz": {"zip"},
    ".doc": {"ole"},
    ".docx": {"zip"},
    ".epub": {"epub"},
    ".gif": {"gif"},
    ".heic": {"heif"},
    ".heif": {"heif"},
    ".jpeg": {"jpeg"},
    ".jpg": {"jpeg"},
    ".m4b": {"mp4"},
    ".mobi": {"mobi"},
    ".mov": {"mp4"},
    ".mp3": {"mp3"},
    ".mp4": {"mp4"},
    ".odt": {"zip"},
    ".pdf": {"pdf"},
    ".png": {"png"},
    ".ppt": {"ole"},
    ".pptx": {"zip"},
    ".rar": {"rar"},
    ".tif": {"tiff"},
    ".tiff": {"tiff"},
    ".webp": {"webp"},
    ".xls": {"ole"},
    ".xlsx": {"zip"},
    ".zip": {"zip"},
}


def supported_extensions(kind: str) -> set[str]:
    return {
        "audiovisual": AV_EXTENSIONS,
        "books": BOOK_EXTENSIONS,
        "photos": PHOTO_EXTENSIONS,
    }[kind]


def detect_format(header: bytes, extension: str) -> str | None:
    if b"%PDF-" in header[:1024]:
        return "pdf"
    if header.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if header.startswith(b"BM"):
        return "bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brands = header[8:32]
        if any(brand in brands for brand in (b"heic", b"heix", b"hevc", b"mif1")):
            return "heif"
        return "mp4"
    if header.startswith(b"PK\x03\x04"):
        return "zip"
    if header.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "ole"
    if len(header) >= 68 and header[60:68] == b"BOOKMOBI":
        return "mobi"
    if header.startswith(b"ID3") or header[:2] in {
        b"\xff\xfb", b"\xff\xf3", b"\xff\xf2",
    }:
        return "mp3"
    if extension in {".txt", ".md", ".nfo", ".htm", ".html", ".rtf", ".xmp", ".aae"}:
        return "text"
    return None


def image_dimensions(header: bytes, detected: str | None) -> tuple[int, int] | None:
    if detected == "png" and len(header) >= 24:
        return struct.unpack(">II", header[16:24])
    if detected == "gif" and len(header) >= 10:
        return struct.unpack("<HH", header[6:10])
    if detected == "bmp" and len(header) >= 26:
        return struct.unpack("<ii", header[18:26])
    if detected != "jpeg":
        return None
    position = 2
    while position + 9 < len(header):
        if header[position] != 0xFF:
            position += 1
            continue
        marker = header[position + 1]
        position += 2
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if position + 2 > len(header):
            break
        length = int.from_bytes(header[position:position + 2], "big")
        if length < 2 or position + length > len(header):
            break
        if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
            height = int.from_bytes(header[position + 3:position + 5], "big")
            width = int.from_bytes(header[position + 5:position + 7], "big")
            return width, height
        position += length
    return None


def inspect_zip(path: Path, extension: str) -> tuple[str, dict[str, object], list[Finding]]:
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            count = len(names)
            detected = "zip"
            if extension == ".epub":
                mimetype = archive.read("mimetype") if "mimetype" in names else b""
                if mimetype == b"application/epub+zip" and "META-INF/container.xml" in names:
                    detected = "epub"
                else:
                    findings.append(Finding("invalid-epub-container", "error", "EPUB container metadata is missing or invalid"))
            if count == 0:
                findings.append(Finding("empty-archive", "error", "Archive has no members"))
            return detected, {"archive_members": count}, findings
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
        return "zip", {}, [Finding("invalid-archive", "error", "Archive central directory is unreadable")]


def inspect_pdf_with_poppler(path: Path) -> Finding | None:
    executable = shutil.which("pdfinfo")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return Finding("pdf-validation-incomplete", "warning", "PDF parser could not complete validation")
    if result.returncode == 0:
        return None
    error = result.stderr.casefold()
    if "password" in error or "encrypted" in error:
        return Finding("pdf-encrypted", "info", "PDF requires a password; no circumvention is attempted")
    return Finding("pdf-parse-failed", "error", "Independent PDF parser rejected the document")


def inspect_file(path: Path, extension: str, kind: str) -> tuple[str | None, dict[str, object], list[Finding]]:
    findings: list[Finding] = []
    metadata: dict[str, object] = {}
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            header = handle.read(1024 * 1024)
            handle.seek(max(0, size - 65536))
            tail = handle.read(65536)
    except OSError:
        return None, {}, [Finding("unreadable-file", "error", "File cannot be read")]
    if size == 0:
        return None, {}, [Finding("empty-file", "error", "File is empty")]

    detected = detect_format(header, extension)
    if detected == "zip":
        detected, archive_metadata, archive_findings = inspect_zip(path, extension)
        metadata.update(archive_metadata)
        findings.extend(archive_findings)
    if detected == "pdf":
        if b"%%EOF" not in tail:
            findings.append(Finding("pdf-missing-eof", "error", "PDF has no terminal EOF marker"))
        metadata["encrypted_hint"] = b"/Encrypt" in header or b"/Encrypt" in tail
        pdf_finding = inspect_pdf_with_poppler(path)
        if pdf_finding is not None:
            findings.append(pdf_finding)
    dimensions = image_dimensions(header, detected)
    if dimensions:
        metadata.update({"width": dimensions[0], "height": dimensions[1]})
        if dimensions[0] <= 0 or dimensions[1] <= 0:
            findings.append(Finding("invalid-dimensions", "error", "Image dimensions are invalid"))
    elif detected in {"jpeg", "png", "gif", "bmp"}:
        findings.append(Finding("missing-dimensions", "warning", "Image dimensions could not be read from its header"))

    expected = EXPECTED_FORMATS.get(extension)
    if expected and detected not in expected:
        findings.append(
            Finding(
                "extension-format-mismatch",
                "error",
                "Filename extension does not match detected file format",
                evidence={"extension": extension, "detected_format": detected},
            )
        )
    if extension not in supported_extensions(kind):
        findings.append(
            Finding(
                "unsupported-extension",
                "warning",
                "Collection policy does not recognize this extension",
                evidence={"extension": extension or "<none>"},
            )
        )
    return detected, metadata, findings
