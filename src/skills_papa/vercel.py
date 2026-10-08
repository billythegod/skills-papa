from __future__ import annotations

import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .errors import ValidationError
from .util import validate_skill_name

DEFAULT_VERCEL_PACKAGE = "skills@1.5.23"
MAX_LOCK_BYTES = 10 * 1024 * 1024
_PACKAGE_PATTERN = re.compile(
    r"skills(?:@(?:latest|\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?))?\Z"
)


def vercel_package_from_config(config: dict[str, Any]) -> str:
    providers = config.get("providers", {})
    provider = providers.get("vercel_skills", {}) if isinstance(providers, dict) else {}
    if not isinstance(provider, dict):
        raise ValidationError("config.providers.vercel_skills must be a mapping")
    if provider.get("telemetry", False) is not False:
        raise ValidationError(
            "config.providers.vercel_skills.telemetry must remain false"
        )
    package = provider.get("package", DEFAULT_VERCEL_PACKAGE)
    if not isinstance(package, str) or not _PACKAGE_PATTERN.fullmatch(package):
        raise ValidationError(
            "config.providers.vercel_skills.package must be 'skills', "
            "'skills@latest', or a pinned skills@X.Y.Z version"
        )
    return package


def default_vercel_lock_path(scope: str, *, cwd: Path | None = None) -> Path:
    if scope == "project":
        return (cwd or Path.cwd()) / "skills-lock.json"
    if scope != "global":
        raise ValidationError("Vercel lock scope must be 'global' or 'project'")
    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state:
        return Path(xdg_state).expanduser() / "skills/.skill-lock.json"
    return Path.home() / ".agents/.skill-lock.json"


def _text_field(
    entry: dict[str, Any], key: str, *, required: bool = False
) -> str | None:
    value = entry.get(key)
    if value is None and not required:
        return None
    if isinstance(value, str) and not value.strip() and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"Vercel lock entry has invalid {key}")
    value = value.strip()
    if len(value) > 4096 or any(character in value for character in "\x00\r\n"):
        raise ValidationError(f"Vercel lock entry has unsafe {key}")
    parsed = urlsplit(value)
    if parsed.scheme in {"http", "https"} and (
        parsed.username is not None or parsed.password is not None
    ):
        raise ValidationError(
            f"Vercel lock entry {key} contains URL credentials; refusing to persist it"
        )
    return value


def _is_bare_shorthand(source: str) -> bool:
    return (
        ":" not in source and not source.startswith(".") and not source.startswith("/")
    )


def _supports_appended_subpath(source: str) -> bool:
    if source.startswith(("git@", "ssh://")) or source.endswith(".git"):
        return False
    if source.startswith(("http://", "https://")):
        try:
            return urlsplit(source).hostname in {"github.com", "gitlab.com"}
        except ValueError:
            return False
    return True


def _skill_folder(skill_path: str) -> str:
    normalized = skill_path.replace("\\", "/")
    path = PurePosixPath(normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ValidationError("Vercel lock skillPath must stay within its source")
    parts = list(path.parts)
    if parts and parts[-1].lower() == "skill.md":
        parts.pop()
    return "/".join(parts)


def _format_source(source: str, ref: str | None) -> str:
    return f"{source}#{ref}" if ref else source


def _entry_source(entry: dict[str, Any], lock_dir: Path) -> str:
    source_type = _text_field(entry, "sourceType", required=True)
    source = _text_field(entry, "source", required=True)
    source_url = _text_field(entry, "sourceUrl")
    ref = _text_field(entry, "ref")
    skill_path = _text_field(entry, "skillPath")
    assert source_type is not None and source is not None

    if source_type == "local" and not Path(source).expanduser().is_absolute():
        source = str((lock_dir / source).resolve())
    if source_url is not None and source_type == "local":
        source_url_path = Path(source_url).expanduser()
        if not source_url_path.is_absolute():
            source_url = str((lock_dir / source_url_path).resolve())

    def usable_source() -> str:
        if source_url:
            return source_url
        if source_type in {"git", "gitlab"} and _is_bare_shorthand(source):
            raise ValidationError(
                "Vercel lock entry lost its original Git host; sourceUrl is required"
            )
        return source

    if not skill_path:
        selected = (
            (source_url or source) if source_type == "github" else usable_source()
        )
        return _format_source(selected, ref)

    selected = source if source_type == "github" else usable_source()
    if not _supports_appended_subpath(selected):
        return _format_source(selected, ref)
    folder = _skill_folder(skill_path)
    selected = f"{selected.rstrip('/')}/{folder}" if folder else selected
    return _format_source(selected, ref)


def origin_from_vercel_entry(
    name: str, entry: dict[str, Any], *, lock_dir: Path
) -> dict[str, Any]:
    validate_skill_name(name)
    source_type = _text_field(entry, "sourceType", required=True)
    source_id = _text_field(entry, "source", required=True)
    assert source_type is not None and source_id is not None
    origin: dict[str, Any] = {
        "type": "vercel-skills",
        "fetcher": "vercel-labs/skills",
        "source": _entry_source(entry, lock_dir),
        "source_id": source_id,
        "source_type": source_type,
        "skill": name,
    }
    for source_key, origin_key in (
        ("sourceUrl", "source_url"),
        ("ref", "ref"),
        ("skillPath", "skill_path"),
        ("skillFolderHash", "upstream_hash"),
        ("computedHash", "upstream_hash"),
        ("wellKnownDigest", "well_known_digest"),
    ):
        value = _text_field(entry, source_key)
        if value is not None:
            origin[origin_key] = value
    return origin


def read_vercel_lock(path: Path, *, scope: str) -> dict[str, Any]:
    if scope not in {"global", "project"}:
        raise ValidationError("Vercel lock scope must be 'global' or 'project'")
    path = path.expanduser()
    try:
        file_stat = path.stat()
    except OSError as exc:
        raise ValidationError(f"cannot read Vercel lock file {path}: {exc}") from exc
    if not path.is_file() or file_stat.st_size > MAX_LOCK_BYTES:
        raise ValidationError(
            f"Vercel lock must be a regular JSON file no larger than {MAX_LOCK_BYTES} bytes"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(f"invalid Vercel lock file {path}: {exc}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("version"), int):
        raise ValidationError("Vercel lock must contain an integer version")
    raw_skills = value.get("skills")
    if not isinstance(raw_skills, dict):
        raise ValidationError("Vercel lock must contain a skills mapping")

    origins: dict[str, dict[str, Any]] = {}
    invalid: list[dict[str, str]] = []
    for raw_name, raw_entry in sorted(
        raw_skills.items(), key=lambda item: str(item[0])
    ):
        name = str(raw_name)
        try:
            if not isinstance(raw_entry, dict):
                raise ValidationError("lock entry must be a mapping")
            origins[name] = origin_from_vercel_entry(
                name, raw_entry, lock_dir=path.parent.resolve()
            )
        except ValidationError as exc:
            invalid.append({"skill": name, "error": str(exc)})
    return {
        "path": str(path.resolve()),
        "scope": scope,
        "version": value["version"],
        "origins": origins,
        "invalid": invalid,
    }


def append_vercel_subdir(source: str, subdir: str | None) -> str:
    if not subdir:
        return source
    folder = _skill_folder(subdir)
    if not folder:
        return source
    base, separator, ref = source.partition("#")
    if not _supports_appended_subpath(base):
        raise ValidationError(
            "--subdir with the Vercel provider requires owner/repo or a GitHub/GitLab URL; "
            "encode the subdirectory in the source URL or use --fetcher native"
        )
    updated = f"{base.rstrip('/')}/{folder}"
    return f"{updated}#{ref}" if separator else updated
