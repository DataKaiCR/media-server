"""Data model for private, report-only digital collection audits."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    relative_path: str | None = None
    related_paths: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FileRecord:
    relative_path: str
    extension: str
    size: int
    modified_ns: int
    detected_format: str | None
    sha256: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CollectionReport:
    collection_id: str
    kind: str
    role: str
    root: str
    files: list[FileRecord] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        severities: dict[str, int] = {}
        codes: dict[str, int] = {}
        for finding in self.findings:
            severities[finding.severity] = severities.get(finding.severity, 0) + 1
            codes[finding.code] = codes.get(finding.code, 0) + 1
        return {
            "collection_id": self.collection_id,
            "kind": self.kind,
            "role": self.role,
            "file_count": len(self.files),
            "total_bytes": sum(record.size for record in self.files),
            "finding_count": len(self.findings),
            "findings_by_severity": dict(sorted(severities.items())),
            "findings_by_code": dict(sorted(codes.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "collection_id": self.collection_id,
            "kind": self.kind,
            "role": self.role,
            "root": self.root,
            "summary": self.summary(),
            "files": [record.to_dict() for record in self.files],
            "findings": [finding.to_dict() for finding in self.findings],
        }
