from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .errors import SkillsPapaError, ValidationError
from .frontmatter import parse_skill_markdown
from .vercel import (
    DEFAULT_VERCEL_PACKAGE,
    append_vercel_subdir,
    read_vercel_lock,
)


@dataclass
class ResolvedSkill:
    path: Path
    origin: dict[str, Any]


def _is_ignored(path: Path) -> bool:
    return any(
        part in {".git", ".venv", "node_modules", "__pycache__", "dist", "build"}
        for part in path.parts
    )


def discover_skills(root: Path) -> dict[str, Path]:
    root = root.resolve()
    candidates: dict[str, Path] = {}
    skill_files = []
    if (root / "SKILL.md").is_file():
        skill_files.append(root / "SKILL.md")
    skill_files.extend(path for path in root.rglob("SKILL.md") if path.parent != root)
    for skill_file in sorted(set(skill_files)):
        relative = skill_file.relative_to(root)
        if _is_ignored(relative) or len(relative.parts) > 8:
            continue
        try:
            frontmatter, _ = parse_skill_markdown(skill_file.read_bytes())
        except ValidationError:
            continue
        name = str(frontmatter["name"])
        previous = candidates.get(name)
        if previous is not None and previous != skill_file.parent:
            raise ValidationError(
                f"source contains duplicate skill name {name!r}: {previous} and {skill_file.parent}"
            )
        candidates[name] = skill_file.parent
    return candidates


def _select_skill(root: Path, skill: str | None) -> Path:
    candidates = discover_skills(root)
    if not candidates:
        raise ValidationError(f"no valid SKILL.md found under {root}")
    if skill is not None:
        if skill not in candidates:
            available = ", ".join(sorted(candidates))
            raise ValidationError(f"skill {skill!r} not found; available: {available}")
        return candidates[skill]
    if len(candidates) != 1:
        available = ", ".join(sorted(candidates))
        raise ValidationError(
            f"source contains multiple skills; pass --skill. Available: {available}"
        )
    return next(iter(candidates.values()))


def _github_source(source: str) -> tuple[str, str | None, str | None]:
    if source.startswith(("git@", "ssh://", "git://")):
        return source, None, None
    parsed = urlparse(source)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc.lower() == "github.com":
            parts = [part for part in parsed.path.split("/") if part]
            if len(parts) >= 2:
                clone_url = (
                    f"https://github.com/{parts[0]}/{parts[1].removesuffix('.git')}.git"
                )
                if len(parts) >= 4 and parts[2] == "tree":
                    revision = parts[3]
                    subdir = "/".join(parts[4:]) or None
                    return clone_url, revision, subdir
                return clone_url, None, None
        return source, None, None
    parts = source.split("/")
    if len(parts) == 2 and all(parts):
        return f"https://github.com/{parts[0]}/{parts[1]}.git", None, None
    raise ValidationError(f"not a local path or supported git source: {source}")


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 300,
) -> str:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise SkillsPapaError(
            f"command timed out after {timeout}s: {' '.join(command[:3])}"
        ) from exc
    if completed.returncode != 0:
        output = completed.stdout.strip()
        raise SkillsPapaError(
            f"command failed ({' '.join(command[:3])}): {output[-2000:]}"
        )
    return completed.stdout


@contextmanager
def resolve_source(
    source: str,
    *,
    skill: str | None = None,
    subdir: str | None = None,
    fetcher: str = "vercel",
    vercel_package: str = DEFAULT_VERCEL_PACKAGE,
) -> Iterator[ResolvedSkill]:
    local = Path(source).expanduser()
    if local.exists():
        root = local.resolve()
        if subdir:
            root = (root / subdir).resolve()
        selected = _select_skill(root, skill)
        yield ResolvedSkill(
            selected,
            {"type": "local", "path": str(selected), "source": str(local.resolve())},
        )
        return

    with tempfile.TemporaryDirectory(prefix="skills-papa-fetch-") as temporary:
        temporary_root = Path(temporary)
        if fetcher in {"vercel", "skills-cli"}:
            if shutil.which("npx") is None:
                raise SkillsPapaError(
                    "the Vercel skills provider requires npx; install Node.js or use "
                    "--fetcher native for a Git-compatible source"
                )
            source_argument = append_vercel_subdir(source, subdir)
            command = [
                "npx",
                "--yes",
                vercel_package,
                "add",
                source_argument,
                "--agent",
                "universal",
                "--copy",
                "--yes",
                "--full-depth",
            ]
            if skill:
                command.extend(["--skill", skill])
            environment = dict(os.environ)
            environment["DISABLE_TELEMETRY"] = "1"
            environment["DO_NOT_TRACK"] = "1"
            _run(command, cwd=temporary_root, env=environment)
            installed = temporary_root / ".agents/skills"
            selected = _select_skill(installed, skill)
            lock_path = temporary_root / "skills-lock.json"
            origin: dict[str, Any]
            if lock_path.is_file():
                lock = read_vercel_lock(lock_path, scope="project")
                origin = lock["origins"].get(selected.name, {})
            else:
                origin = {}
            if not origin:
                origin = {
                    "type": "vercel-skills",
                    "source": source_argument,
                    "source_id": source,
                    "source_type": "unknown",
                    "skill": selected.name,
                    "fetcher": "vercel-labs/skills",
                }
            origin["package"] = vercel_package
            yield ResolvedSkill(
                selected,
                origin,
            )
            return
        if fetcher != "native":
            raise ValidationError(f"unsupported fetcher: {fetcher}")
        clone_url, revision, url_subdir = _github_source(source)
        clone_root = temporary_root / "repo"
        command = ["git", "clone", "--depth", "1"]
        if revision:
            command.extend(["--branch", revision])
        command.extend([clone_url, str(clone_root)])
        _run(command)
        commit = _run(["git", "rev-parse", "HEAD"], cwd=clone_root).strip()
        selected_root = clone_root
        effective_subdir = subdir or url_subdir
        if effective_subdir:
            selected_root = (clone_root / effective_subdir).resolve()
            try:
                selected_root.relative_to(clone_root.resolve())
            except ValueError as exc:
                raise ValidationError(
                    f"subdir escapes cloned repository: {effective_subdir}"
                ) from exc
        selected = _select_skill(selected_root, skill)
        yield ResolvedSkill(
            selected,
            {
                "type": "git",
                "url": clone_url,
                "source": source,
                "revision": commit,
                "requested_revision": revision,
                "subdir": selected.relative_to(clone_root).as_posix(),
                "skill": skill,
            },
        )
