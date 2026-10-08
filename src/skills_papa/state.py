from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .errors import ConflictError, StateError, ValidationError
from .frontmatter import parse_skill_markdown
from .models import (
    DEFAULT_ENABLED_TARGETS,
    SCHEMA_VERSION,
    FileRecord,
    PendingManifest,
    TargetSpec,
)
from .util import (
    atomic_write_json,
    content_id,
    immutable_mode,
    read_json,
    sha256_file,
    utc_now,
    validate_relative_path,
    validate_skill_name,
)
from .vercel import DEFAULT_VERCEL_PACKAGE

IGNORED_NAMES = {
    ".DS_Store",
    ".git",
    ".hg",
    ".svn",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


def default_config() -> dict[str, Any]:
    home = Path.home()
    return {
        "schema_version": SCHEMA_VERSION,
        "default_targets": list(DEFAULT_ENABLED_TARGETS),
        "limits": {"max_files": 5000, "max_bytes": 250 * 1024 * 1024},
        "providers": {
            "vercel_skills": {
                "package": DEFAULT_VERCEL_PACKAGE,
                "telemetry": False,
            }
        },
        "owner_managed_skills": {},
        "targets": {
            "codex": {
                "root": str(home / ".codex/skills"),
                "protected_names": [".system"],
            },
            "claude": {
                "root": str(home / ".claude/skills"),
                "protected_names": [],
            },
            "opencode": {
                "root": str(home / ".config/opencode/skills"),
                "protected_names": [],
            },
            "universal": {
                "root": str(home / ".agents/skills"),
                "protected_names": [],
            },
        },
    }


def empty_registry() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": 0,
        "skills": {},
        "deployments": {},
        "updated_at": utc_now(),
    }


class State:
    def __init__(self, home: Path):
        self.home = home.expanduser().resolve()
        self.config_path = self.home / "config.json"
        self.registry_path = self.home / "registry.json"
        self.objects_dir = self.home / "objects"
        self.manifests_dir = self.home / "manifests"
        self.transactions_dir = self.home / "transactions"
        self.backups_dir = self.home / "backups"
        self.lock_path = self.home / ".lock"

    @property
    def initialized(self) -> bool:
        return self.config_path.is_file() and self.registry_path.is_file()

    def initialize(self, config: dict[str, Any] | None = None) -> None:
        if self.home.exists() and not self.home.is_dir():
            raise StateError(f"state home is not a directory: {self.home}")
        self.home.mkdir(parents=True, exist_ok=True)
        for directory in (
            self.objects_dir,
            self.manifests_dir,
            self.transactions_dir,
            self.backups_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            atomic_write_json(self.config_path, config or default_config())
        if not self.registry_path.exists():
            atomic_write_json(self.registry_path, empty_registry())

    def require_initialized(self) -> None:
        if not self.initialized:
            raise StateError(
                f"Skills Papa is not initialized at {self.home}; run 'skills-papa init --apply'"
            )

    def config(self) -> dict[str, Any]:
        self.require_initialized()
        value = read_json(self.config_path)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"unsupported or corrupt config: {self.config_path}")
        return value

    def registry(self) -> dict[str, Any]:
        self.require_initialized()
        value = read_json(self.registry_path)
        if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
            raise StateError(f"unsupported or corrupt registry: {self.registry_path}")
        if not isinstance(value.get("skills"), dict) or not isinstance(
            value.get("deployments"), dict
        ):
            raise StateError(f"registry has invalid shape: {self.registry_path}")
        return value

    def owner_managed_skills(self) -> dict[str, dict[str, str]]:
        raw = self.config().get("owner_managed_skills", {})
        if not isinstance(raw, dict):
            raise StateError("config.owner_managed_skills must be a mapping")
        result: dict[str, dict[str, str]] = {}
        for raw_name, raw_entry in raw.items():
            name = validate_skill_name(raw_name)
            if isinstance(raw_entry, str):
                owner = raw_entry.strip()
                reason = ""
            elif isinstance(raw_entry, dict):
                owner_value = raw_entry.get("owner")
                reason_value = raw_entry.get("reason", "")
                owner = owner_value.strip() if isinstance(owner_value, str) else ""
                reason = reason_value.strip() if isinstance(reason_value, str) else ""
            else:
                raise StateError(f"invalid owner-managed skill configuration: {name}")
            if not owner or len(owner) > 128 or any(c in owner for c in "\x00\r\n"):
                raise StateError(f"invalid owner for owner-managed skill: {name}")
            if len(reason) > 1024 or any(c in reason for c in "\x00\r\n"):
                raise StateError(f"invalid reason for owner-managed skill: {name}")
            result[name] = {"owner": owner, "reason": reason}
        return result

    def save_registry(self, registry: dict[str, Any]) -> None:
        current = read_json(self.registry_path, {})
        current_revision = (
            int(current.get("revision", 0)) if isinstance(current, dict) else 0
        )
        registry["revision"] = current_revision + 1
        registry["updated_at"] = utc_now()
        atomic_write_json(self.registry_path, registry)

    def target_specs(self) -> dict[str, TargetSpec]:
        config = self.config()
        result: dict[str, TargetSpec] = {}
        targets = config.get("targets")
        if not isinstance(targets, dict):
            raise StateError("config.targets must be a mapping")
        for name, target in targets.items():
            if not isinstance(target, dict) or not isinstance(target.get("root"), str):
                raise StateError(f"invalid target config: {name}")
            protected = target.get("protected_names", [])
            if not isinstance(protected, list) or not all(
                isinstance(item, str) for item in protected
            ):
                raise StateError(f"invalid protected_names for target: {name}")
            result[name] = TargetSpec(
                name=name,
                root=Path(target["root"]).expanduser(),
                protected_names=set(protected),
            )
        return result

    @contextmanager
    def exclusive_lock(self, *, allow_unfinished: bool = False) -> Iterator[None]:
        self.require_initialized()
        flags = os.O_RDWR | os.O_CREAT | os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(self.lock_path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise StateError(f"state lock is not a regular file: {self.lock_path}")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConflictError(
                    "another Skills Papa operation is already running"
                ) from exc
            if not allow_unfinished:
                unfinished = []
                for journal_path in sorted(self.transactions_dir.glob("*.json")):
                    journal = read_json(journal_path)
                    if (
                        isinstance(journal, dict)
                        and journal.get("status") == "applying"
                    ):
                        unfinished.append(str(journal.get("id") or journal_path.stem))
                if unfinished:
                    raise StateError(
                        "unfinished transaction blocks mutations; run "
                        f"'skills-papa recover {unfinished[0]} --apply'"
                    )
            os.ftruncate(descriptor, 0)
            os.write(descriptor, f"pid={os.getpid()}\n".encode("ascii"))
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def object_path(self, sha256: str, mode: int) -> Path:
        mode_key = immutable_mode(mode)
        return self.objects_dir / sha256[:2] / f"{sha256}.m{mode_key:o}"

    def manifest_path(self, name: str, manifest_id: str) -> Path:
        validate_skill_name(name)
        return self.manifests_dir / name / f"{manifest_id}.json"

    def build_pending_manifest(
        self,
        skill_dir: Path,
        *,
        origin: dict[str, Any] | None = None,
        expected_name: str | None = None,
    ) -> PendingManifest:
        skill_dir = skill_dir.expanduser().resolve()
        if not skill_dir.is_dir():
            raise ValidationError(f"skill directory does not exist: {skill_dir}")
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file() or skill_md.is_symlink():
            raise ValidationError(
                f"skill directory must contain a regular SKILL.md: {skill_dir}"
            )
        frontmatter, _ = parse_skill_markdown(skill_md.read_bytes())
        name = validate_skill_name(frontmatter["name"])
        if skill_dir.name != name:
            raise ValidationError(
                f"skill directory name {skill_dir.name!r} does not match frontmatter name {name!r}"
            )
        if expected_name is not None and name != expected_name:
            raise ValidationError(f"expected skill {expected_name!r}, found {name!r}")
        description = str(frontmatter["description"]).strip()
        metadata = frontmatter.get("metadata")
        raw_version = metadata.get("version") if isinstance(metadata, dict) else None
        if raw_version is None:
            raw_version = frontmatter.get("version")
        version = str(raw_version).strip() if raw_version is not None else None
        if version == "":
            version = None
        limits = self.config().get("limits", {})
        max_files = int(limits.get("max_files", 5000))
        max_bytes = int(limits.get("max_bytes", 250 * 1024 * 1024))
        source_paths: dict[str, Path] = {}
        records: list[FileRecord] = []
        total_bytes = 0
        for root, directory_names, filenames in os.walk(skill_dir, followlinks=False):
            root_path = Path(root)
            for directory_name in directory_names:
                candidate = root_path / directory_name
                if directory_name not in IGNORED_NAMES and candidate.is_symlink():
                    raise ValidationError(
                        f"symlinked directories are not accepted: {candidate}"
                    )
            directory_names[:] = [
                item
                for item in sorted(directory_names)
                if item not in IGNORED_NAMES and not (root_path / item).is_symlink()
            ]
            for filename in sorted(filenames):
                if filename in IGNORED_NAMES or filename.endswith((".pyc", ".pyo")):
                    continue
                path = root_path / filename
                if path.is_symlink():
                    raise ValidationError(f"symlinked files are not accepted: {path}")
                if not path.is_file():
                    continue
                relative = validate_relative_path(
                    path.relative_to(skill_dir).as_posix()
                )
                file_stat = path.stat()
                size = int(file_stat.st_size)
                total_bytes += size
                if len(records) + 1 > max_files:
                    raise ValidationError(f"skill exceeds file limit ({max_files})")
                if total_bytes > max_bytes:
                    raise ValidationError(f"skill exceeds byte limit ({max_bytes})")
                mode = 0o755 if file_stat.st_mode & stat.S_IXUSR else 0o644
                file_hash = sha256_file(path)
                records.append(FileRecord(relative, file_hash, size, mode))
                source_paths[relative] = path
        if "SKILL.md" not in source_paths:
            raise ValidationError("SKILL.md was excluded unexpectedly")
        manifest_id = content_id(
            (record.path, record.sha256, record.mode) for record in records
        )
        return PendingManifest(
            name=name,
            description=description,
            version=version,
            content_id=manifest_id,
            files=records,
            source_paths=source_paths,
            origin=origin or {"type": "local", "path": str(skill_dir)},
        )

    def persist_manifest(self, pending: PendingManifest) -> None:
        for record in pending.files:
            source = pending.source_paths[record.path]
            destination = self.object_path(record.sha256, record.mode)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                if (
                    not destination.is_file()
                    or sha256_file(destination) != record.sha256
                ):
                    raise StateError(
                        f"object store collision or corruption: {destination}"
                    )
                continue
            descriptor, temporary = tempfile.mkstemp(
                prefix=".object-", dir=destination.parent
            )
            temporary_path = Path(temporary)
            try:
                with (
                    source.open("rb") as input_handle,
                    os.fdopen(descriptor, "wb") as output_handle,
                ):
                    while chunk := input_handle.read(1024 * 1024):
                        output_handle.write(chunk)
                    output_handle.flush()
                    os.fsync(output_handle.fileno())
                if sha256_file(temporary_path) != record.sha256:
                    raise StateError(f"object changed while importing: {source}")
                temporary_path.chmod(immutable_mode(record.mode))
                os.replace(temporary_path, destination)
            finally:
                if temporary_path.exists():
                    temporary_path.unlink()
        manifest_path = self.manifest_path(pending.name, pending.content_id)
        if not manifest_path.exists():
            atomic_write_json(manifest_path, pending.as_manifest_dict())

    def load_manifest(self, name: str, manifest_id: str) -> dict[str, Any]:
        value = read_json(self.manifest_path(name, manifest_id))
        if not isinstance(value, dict) or value.get("content_id") != manifest_id:
            raise StateError(f"missing or corrupt manifest {name}@{manifest_id}")
        if not isinstance(value.get("files"), list):
            raise StateError(f"manifest file list is invalid: {name}@{manifest_id}")
        return value

    def manifest_files(
        self, name: str, manifest_id: str
    ) -> tuple[dict[str, bytes], dict[str, int]]:
        manifest = self.load_manifest(name, manifest_id)
        files: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        for raw in manifest["files"]:
            record = FileRecord.from_dict(raw)
            object_path = self.object_path(record.sha256, record.mode)
            if not object_path.is_file():
                raise StateError(
                    f"missing object {record.sha256} for {name}@{manifest_id}"
                )
            data = object_path.read_bytes()
            if len(data) != record.size:
                raise StateError(f"object size mismatch: {object_path}")
            files[record.path] = data
            modes[record.path] = record.mode
        return files, modes
