"""Bounded EPUB package and bibliographic analysis."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import posixpath
from urllib.parse import unquote
import xml.etree.ElementTree as ET
import zipfile

from .book_common import bibliographic_evidence, clean_metadata
from .model import Finding


MAX_EPUB_XML_BYTES = 2_097_152


class EpubStructureError(ValueError):
    """An EPUB structure cannot be parsed within safe bounds."""


@dataclass
class PackageEvidence:
    titles: list[str] = field(default_factory=list)
    creators: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    manifest: dict[str, tuple[str, str, str]] = field(default_factory=dict)
    spine_ids: list[str] = field(default_factory=list)
    collection_values: dict[str, str] = field(default_factory=dict)
    refinements: dict[str, dict[str, str]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    cover_id: str = ""
    series: str = ""
    volume: str = ""


def _safe_archive_name(name: str) -> bool:
    path = PurePosixPath(name)
    return not path.is_absolute() and ".." not in path.parts and "\\" not in name


def _read_archive_member(
    archive: zipfile.ZipFile, name: str, maximum_bytes: int = MAX_EPUB_XML_BYTES
) -> bytes:
    try:
        info = archive.getinfo(name)
    except KeyError as error:
        raise EpubStructureError("archive member is missing") from error
    if info.file_size > maximum_bytes:
        raise EpubStructureError("archive metadata member exceeds safe bound")
    data = archive.read(info)
    if len(data) > maximum_bytes:
        raise EpubStructureError("archive metadata member exceeds safe bound")
    return data


def _parse_bounded_xml(data: bytes) -> ET.Element:
    upper = data.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise EpubStructureError("EPUB metadata must not define XML entities")
    return ET.fromstring(data)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _element_text(element: ET.Element) -> str:
    return clean_metadata("".join(element.itertext()))


def _resolve_epub_path(package_path: str, href: str) -> str | None:
    decoded = unquote(href.split("#", 1)[0])
    combined = posixpath.normpath(
        posixpath.join(posixpath.dirname(package_path), decoded)
    )
    return combined if _safe_archive_name(combined) else None


def _record_meta(
    evidence: PackageEvidence, element: ET.Element, value: str
) -> None:
    name = element.attrib.get("name", "").casefold()
    content = clean_metadata(element.attrib.get("content", ""))
    named_values = {
        "cover": "cover_id",
        "calibre:series": "series",
        "calibre:series_index": "volume",
    }
    if attribute := named_values.get(name):
        setattr(evidence, attribute, content)
    property_name = element.attrib.get("property", "")
    refines = element.attrib.get("refines", "").lstrip("#")
    identifier = element.attrib.get("id", "")
    if property_name == "belongs-to-collection" and identifier:
        evidence.collection_values[identifier] = value
    if refines and property_name:
        evidence.refinements[refines][property_name] = value


def _record_package_element(
    evidence: PackageEvidence, element: ET.Element
) -> None:
    local = _local_name(element.tag)
    value = _element_text(element)
    text_lists = {
        "title": evidence.titles,
        "creator": evidence.creators,
        "language": evidence.languages,
        "identifier": evidence.identifiers,
    }
    if local in text_lists and value:
        text_lists[local].append(value)
    if local == "item":
        item_id = element.attrib.get("id", "")
        if item_id:
            evidence.manifest[item_id] = (
                element.attrib.get("href", ""),
                element.attrib.get("media-type", ""),
                element.attrib.get("properties", ""),
            )
    if local == "itemref" and element.attrib.get("idref"):
        evidence.spine_ids.append(element.attrib["idref"])
    if local == "meta":
        _record_meta(evidence, element, value)


def _package_evidence(package: ET.Element) -> PackageEvidence:
    evidence = PackageEvidence()
    for element in package.iter():
        _record_package_element(evidence, element)
    if evidence.series:
        return evidence
    for identifier, value in evidence.collection_values.items():
        refinement = evidence.refinements[identifier]
        if refinement.get("collection-type") == "series":
            evidence.series = value
            evidence.volume = refinement.get("group-position", "")
            break
    return evidence


def _structure_evidence(
    package_path: str,
    evidence: PackageEvidence,
    names: set[str],
) -> tuple[dict[str, object], list[Finding]]:
    missing_manifest_files = 0
    embedded_cover = False
    nav_present = False
    for item_id, (href, media_type, properties) in evidence.manifest.items():
        resolved = _resolve_epub_path(package_path, href)
        if resolved is None or resolved not in names:
            missing_manifest_files += 1
        property_set = set(properties.split())
        nav_present = nav_present or "nav" in property_set
        declared_cover = "cover-image" in property_set or (
            item_id == evidence.cover_id and media_type.startswith("image/")
        )
        if declared_cover:
            embedded_cover = resolved in names if resolved else False
    broken_spine = sum(
        identifier not in evidence.manifest for identifier in evidence.spine_ids
    )
    findings = _structure_findings(
        missing_manifest_files, broken_spine, embedded_cover
    )
    return {
        "manifest_items": len(evidence.manifest),
        "spine_items": len(evidence.spine_ids),
        "navigation_document": nav_present,
        "embedded_cover": embedded_cover,
    }, findings


def _structure_findings(
    missing_manifest_files: int, broken_spine: int, embedded_cover: bool
) -> list[Finding]:
    findings: list[Finding] = []
    if missing_manifest_files:
        findings.append(
            Finding(
                "epub-missing-manifest-items", "error",
                "EPUB manifest references missing members",
                evidence={"count": missing_manifest_files},
            )
        )
    if broken_spine:
        findings.append(
            Finding(
                "epub-broken-spine", "error",
                "EPUB spine references missing manifest items",
                evidence={"count": broken_spine},
            )
        )
    if not embedded_cover:
        findings.append(
            Finding(
                "epub-missing-cover", "info",
                "EPUB package has no declared embedded cover",
            )
        )
    return findings


def _archive_findings(infos: list[zipfile.ZipInfo]) -> list[Finding]:
    findings: list[Finding] = []
    names = [info.filename for info in infos]
    if len(set(names)) != len(names):
        findings.append(
            Finding(
                "epub-duplicate-members", "error",
                "EPUB contains duplicate archive member names",
            )
        )
    if any(not _safe_archive_name(name) for name in names):
        findings.append(
            Finding(
                "epub-unsafe-member-path", "error",
                "EPUB contains an unsafe archive member path",
            )
        )
    if (
        not infos
        or infos[0].filename != "mimetype"
        or infos[0].compress_type != zipfile.ZIP_STORED
    ):
        findings.append(
            Finding(
                "epub-nonconforming-mimetype", "warning",
                "EPUB mimetype must be first and uncompressed",
            )
        )
    return findings


def _package_document(
    archive: zipfile.ZipFile,
) -> tuple[str, ET.Element]:
    container = _parse_bounded_xml(
        _read_archive_member(archive, "META-INF/container.xml")
    )
    rootfiles = [
        element.attrib.get("full-path", "")
        for element in container.iter()
        if _local_name(element.tag) == "rootfile"
    ]
    package_path = next((value for value in rootfiles if value), "")
    if not package_path or not _safe_archive_name(package_path):
        raise EpubStructureError("EPUB package path is missing or unsafe")
    package = _parse_bounded_xml(_read_archive_member(archive, package_path))
    return package_path, package


def _metadata_findings(evidence: PackageEvidence) -> list[Finding]:
    findings: list[Finding] = []
    if not evidence.titles:
        findings.append(
            Finding(
                "epub-missing-title", "warning",
                "EPUB package has no title metadata",
            )
        )
    if not evidence.languages:
        findings.append(
            Finding(
                "epub-missing-language", "warning",
                "EPUB package has no language metadata",
            )
        )
    return findings


def _analyze_archive(
    path: Path, archive: zipfile.ZipFile
) -> tuple[dict[str, object], list[Finding]]:
    infos = archive.infolist()
    findings = _archive_findings(infos)
    package_path, package = _package_document(archive)
    evidence = _package_evidence(package)
    structure, structure_findings = _structure_evidence(
        package_path, evidence, {info.filename for info in infos}
    )
    findings.extend(structure_findings)
    findings.extend(_metadata_findings(evidence))
    structure.update(
        {
            "package_version": clean_metadata(
                package.attrib.get("version", ""), 32
            ),
            "uncompressed_bytes": sum(info.file_size for info in infos),
        }
    )
    bibliography = bibliographic_evidence(
        titles=evidence.titles,
        creators=evidence.creators,
        languages=evidence.languages,
        identifier_values=evidence.identifiers,
        series=evidence.series,
        volume=evidence.volume,
        filename=path.stem,
    )
    return {"epub": structure, "bibliographic": bibliography}, findings


def analyze_epub(path: Path) -> tuple[dict[str, object], list[Finding]]:
    try:
        with zipfile.ZipFile(path) as archive:
            return _analyze_archive(path, archive)
    except (
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        ET.ParseError,
        EpubStructureError,
    ):
        return {}, [
            Finding(
                "invalid-epub-package", "error",
                "EPUB package metadata is malformed or exceeds safe bounds",
            )
        ]
