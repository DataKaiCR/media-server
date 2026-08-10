"""Format identification plus collection-specific integrity checks."""

from __future__ import annotations

from pathlib import Path
import struct
import zipfile

from .audiovisual import (
    ARTWORK_EXTENSIONS,
    AUDIO_EXTENSIONS,
    MEDIA_METADATA_EXTENSIONS,
    SUBTITLE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    analyze_audiovisual,
)
from .books import analyze_book
from .config import AudiovisualAnalysisConfig, BookAnalysisConfig, PhotoAnalysisConfig
from .model import Finding
from .photo_analysis import analyze_photo


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
AV_EXTENSIONS = (
    VIDEO_EXTENSIONS | AUDIO_EXTENSIONS | SUBTITLE_EXTENSIONS
    | ARTWORK_EXTENSIONS | MEDIA_METADATA_EXTENSIONS
)
EXPECTED_FORMATS = {
    ".aac": {"aac"},
    ".ass": {"ass"},
    ".avi": {"avi"},
    ".azw3": {"mobi"},
    ".bmp": {"bmp"},
    ".cbr": {"rar"},
    ".cbz": {"zip"},
    ".doc": {"ole"},
    ".docx": {"zip"},
    ".epub": {"epub"},
    ".flac": {"flac"},
    ".flv": {"flv"},
    ".gif": {"gif"},
    ".heic": {"heif"},
    ".heif": {"heif"},
    ".idx": {"vobsub-index"},
    ".jpeg": {"jpeg"},
    ".jpg": {"jpeg"},
    ".m4a": {"mp4"},
    ".m4b": {"mp4"},
    ".m2ts": {"mpegts"},
    ".m4v": {"mp4"},
    ".mka": {"matroska"},
    ".mkv": {"matroska"},
    ".mobi": {"mobi"},
    ".mov": {"mp4"},
    ".mpeg": {"mpeg-program-stream"},
    ".mpg": {"mpeg-program-stream"},
    ".mp3": {"mp3"},
    ".mp4": {"mp4"},
    ".ogg": {"ogg"},
    ".odt": {"zip"},
    ".opus": {"ogg"},
    ".pdf": {"pdf"},
    ".png": {"png"},
    ".ppt": {"ole"},
    ".pptx": {"zip"},
    ".rar": {"rar"},
    ".srt": {"srt"},
    ".ssa": {"ass"},
    ".tif": {"tiff"},
    ".tiff": {"tiff"},
    ".ts": {"mpegts"},
    ".vob": {"mpeg-program-stream"},
    ".vtt": {"webvtt"},
    ".wav": {"wav"},
    ".webm": {"matroska"},
    ".wmv": {"asf"},
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
    if header.startswith(b"RIFF") and header[8:12] == b"AVI ":
        return "avi"
    if header.startswith(b"RIFF") and header[8:12] == b"WAVE":
        return "wav"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "matroska"
    if header.startswith(b"fLaC"):
        return "flac"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"FLV"):
        return "flv"
    if header.startswith(b"\x00\x00\x01\xba"):
        return "mpeg-program-stream"
    if header.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11\xa6\xd9\x00\xaa\x00\x62\xce\x6c"):
        return "asf"
    if len(header) >= 377 and header[0] == header[188] == header[376] == 0x47:
        return "mpegts"
    if len(header) >= 389 and header[4] == header[196] == header[388] == 0x47:
        return "mpegts"
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
    if header[:2] and header[0] == 0xFF and header[1] & 0xF6 == 0xF0:
        return "aac"
    if extension == ".srt" and b"-->" in header:
        return "srt"
    if extension == ".vtt" and header.lstrip(b"\xef\xbb\xbf").startswith(b"WEBVTT"):
        return "webvtt"
    if extension in {".ass", ".ssa"} and b"[Script Info]" in header[:4096]:
        return "ass"
    if extension == ".idx" and b"VobSub index file" in header[:4096]:
        return "vobsub-index"
    if extension in {".txt", ".md", ".nfo", ".htm", ".html", ".rtf", ".xmp", ".aae", ".svg"}:
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


def _epub_mimetype(
    archive: zipfile.ZipFile, names: set[str]
) -> bytes:
    if "mimetype" not in names:
        return b""
    info = archive.getinfo("mimetype")
    if info.file_size > 256:
        return b""
    with archive.open(info) as handle:
        return handle.read(257)


def inspect_zip(path: Path, extension: str) -> tuple[str, dict[str, object], list[Finding]]:
    findings: list[Finding] = []
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            count = len(names)
            detected = "zip"
            if extension == ".epub":
                mimetype = _epub_mimetype(archive, names)
                if mimetype == b"application/epub+zip" and "META-INF/container.xml" in names:
                    detected = "epub"
                else:
                    findings.append(Finding("invalid-epub-container", "error", "EPUB container metadata is missing or invalid"))
            if count == 0:
                findings.append(Finding("empty-archive", "error", "Archive has no members"))
            return detected, {"archive_members": count}, findings
    except (OSError, KeyError, zipfile.BadZipFile, RuntimeError):
        return "zip", {}, [Finding("invalid-archive", "error", "Archive central directory is unreadable")]


AnalysisConfig = AudiovisualAnalysisConfig | BookAnalysisConfig | PhotoAnalysisConfig


def collection_analysis(
    path: Path, extension: str, detected: str | None,
    kind: str, analysis: AnalysisConfig | None,
) -> tuple[dict[str, object], list[Finding]]:
    if kind == "audiovisual":
        settings = (
            analysis if isinstance(analysis, AudiovisualAnalysisConfig)
            else AudiovisualAnalysisConfig()
        )
        metadata, findings = analyze_audiovisual(path, extension, settings)
        return ({"audiovisual": metadata} if metadata else {}), findings
    if kind == "books":
        settings = (
            analysis if isinstance(analysis, BookAnalysisConfig)
            else BookAnalysisConfig()
        )
        return analyze_book(path, extension, detected, settings)
    if kind == "photos":
        settings = (
            analysis if isinstance(analysis, PhotoAnalysisConfig)
            else PhotoAnalysisConfig()
        )
        return analyze_photo(path, detected, settings)
    return {}, []


def inspect_file(
    path: Path,
    extension: str,
    kind: str,
    analysis: AnalysisConfig | None = None,
) -> tuple[str | None, dict[str, object], list[Finding]]:
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
    collection_metadata, collection_findings = collection_analysis(
        path, extension, detected, kind, analysis
    )
    metadata.update(collection_metadata)
    findings.extend(collection_findings)
    return detected, metadata, findings
