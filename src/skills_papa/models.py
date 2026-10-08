from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "0.3.3"
SCHEMA_VERSION = 1

SUPPORTED_TARGETS = ("codex", "claude", "opencode", "universal")
DEFAULT_ENABLED_TARGETS = ("codex", "claude", "opencode")


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    size: int
    mode: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> FileRecord:
        return cls(
            path=str(value["path"]),
            sha256=str(value["sha256"]),
            size=int(value["size"]),
            mode=int(value["mode"]),
        )


@dataclass
class PendingManifest:
    name: str
    description: str
    version: str | None
    content_id: str
    files: list[FileRecord]
    source_paths: dict[str, Path]
    origin: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    def as_manifest_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "content_id": self.content_id,
            "files": [record.as_dict() for record in self.files],
        }


@dataclass
class AdapterResult:
    files: dict[str, bytes]
    modes: dict[str, int]
    warnings: list[str] = field(default_factory=list)


@dataclass
class TargetSpec:
    name: str
    root: Path
    protected_names: set[str] = field(default_factory=set)


@dataclass
class Change:
    action: str
    target: str | None
    skill: str
    path: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "action": self.action,
            "skill": self.skill,
        }
        if self.target is not None:
            value["target"] = self.target
        if self.path is not None:
            value["path"] = self.path
        if self.detail is not None:
            value["detail"] = self.detail
        return value
