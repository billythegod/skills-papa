from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from .errors import StateError, ValidationError

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def transaction_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-{os.urandom(4).hex()}"


def validate_skill_name(name: str) -> str:
    if not isinstance(name, str) or not SKILL_NAME_RE.fullmatch(name):
        raise ValidationError(
            f"invalid skill name {name!r}; use lowercase letters, digits, and single hyphens"
        )
    if len(name) > 64:
        raise ValidationError(f"skill name is too long ({len(name)} > 64): {name}")
    return name


def validate_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValidationError(f"unsafe relative path in skill: {value!r}")
    if "\x00" in value:
        raise ValidationError("NUL byte in skill path")
    return path.as_posix()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def content_id(records: Iterable[tuple[str, str, int]]) -> str:
    digest = hashlib.sha256()
    for relative, file_hash, mode in sorted(records):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\0")
        digest.update(str(mode).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def tree_hash(root: Path) -> str:
    records: list[tuple[str, str, int]] = []
    if not root.is_dir():
        raise StateError(f"expected directory does not exist: {root}")
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            target = os.readlink(path)
            records.append(
                (
                    path.relative_to(root).as_posix(),
                    sha256_bytes(target.encode()),
                    0o120000,
                )
            )
        elif path.is_file():
            mode = stat.S_IMODE(path.stat().st_mode)
            records.append((path.relative_to(root).as_posix(), sha256_file(path), mode))
    return content_id(records)


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StateError(f"cannot read JSON state {path}: {exc}") from exc


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def ensure_exact_child(root: Path, name: str) -> Path:
    validate_skill_name(name)
    root_resolved = root.expanduser().resolve()
    candidate = root_resolved / name
    if candidate.parent != root_resolved:
        raise ValidationError(f"refusing path outside target root: {candidate}")
    return candidate


def immutable_mode(mode: int) -> int:
    normalized = mode & 0o777
    if normalized == 0:
        normalized = 0o644
    return normalized & ~0o222


def materialize_shared_file(source: Path, destination: Path, mode: int) -> str:
    """Create an independent or immutable shared file and return the strategy used."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Darwin":
        completed = subprocess.run(
            ["cp", "-c", str(source), str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            destination.chmod(immutable_mode(mode))
            return "apfs-clone"
    elif system == "Linux":
        completed = subprocess.run(
            ["cp", "--reflink=auto", "--", str(source), str(destination)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode == 0:
            destination.chmod(immutable_mode(mode))
            return "reflink-or-copy"
    if os.environ.get("SKILLS_PAPA_ALLOW_HARDLINKS") == "1":
        try:
            os.link(source, destination, follow_symlinks=False)
            destination.chmod(immutable_mode(mode))
            return "hardlink"
        except OSError:
            pass
    shutil.copy2(source, destination, follow_symlinks=False)
    destination.chmod(immutable_mode(mode))
    return "copy"
