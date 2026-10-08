from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_skill

from skills_papa import fetch
from skills_papa.errors import ValidationError
from skills_papa.fetch import resolve_source
from skills_papa.vercel import (
    DEFAULT_VERCEL_PACKAGE,
    default_vercel_lock_path,
    read_vercel_lock,
    vercel_package_from_config,
)


def write_lock(path: Path, skills: dict, *, version: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": version, "skills": skills}), encoding="utf-8"
    )


def test_vercel_provider_downloads_only_to_temporary_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], Path, dict[str, str]]] = []
    monkeypatch.setattr(fetch.shutil, "which", lambda command: "/usr/bin/npx")

    def fake_run(command, *, cwd=None, env=None, timeout=300):
        assert cwd is not None and env is not None
        calls.append((command, cwd, env))
        make_skill(cwd / ".agents/skills")
        write_lock(
            cwd / "skills-lock.json",
            {
                "sample-skill": {
                    "source": "owner/repository",
                    "sourceUrl": "https://github.com/owner/repository.git",
                    "sourceType": "github",
                    "ref": "main",
                    "skillPath": "skills/sample-skill/SKILL.md",
                    "computedHash": "a" * 64,
                }
            },
        )
        return "installed"

    monkeypatch.setattr(fetch, "_run", fake_run)
    with resolve_source(
        "owner/repository",
        skill="sample-skill",
        fetcher="vercel",
        vercel_package=DEFAULT_VERCEL_PACKAGE,
    ) as resolved:
        assert resolved.path.name == "sample-skill"
        assert resolved.origin == {
            "type": "vercel-skills",
            "fetcher": "vercel-labs/skills",
            "source": "owner/repository/skills/sample-skill#main",
            "source_id": "owner/repository",
            "source_type": "github",
            "skill": "sample-skill",
            "source_url": "https://github.com/owner/repository.git",
            "ref": "main",
            "skill_path": "skills/sample-skill/SKILL.md",
            "upstream_hash": "a" * 64,
            "package": DEFAULT_VERCEL_PACKAGE,
        }
    command, temporary_root, environment = calls[0]
    assert command[:4] == ["npx", "--yes", DEFAULT_VERCEL_PACKAGE, "add"]
    assert command[4] == "owner/repository"
    assert command[-2:] == ["--skill", "sample-skill"]
    assert {"--agent", "--copy", "--yes", "--full-depth"} <= set(command)
    assert environment["DISABLE_TELEMETRY"] == "1"
    assert environment["DO_NOT_TRACK"] == "1"
    assert not temporary_root.exists()


def test_read_vercel_lock_normalizes_sources_and_skips_invalid_entries(
    tmp_path: Path,
) -> None:
    lock_path = tmp_path / "project/skills-lock.json"
    write_lock(
        lock_path,
        {
            "sample-skill": {
                "source": "owner/repository",
                "sourceType": "github",
                "ref": "v2",
                "skillPath": "skills/sample-skill/SKILL.md",
                "computedHash": "b" * 64,
            },
            "local-skill": {
                "source": "../local-skill",
                "sourceType": "local",
                "computedHash": "c" * 64,
            },
            "Bad Skill": {
                "source": "owner/repository",
                "sourceType": "github",
                "computedHash": "d" * 64,
            },
            "lost-host": {
                "source": "owner/repository",
                "sourceType": "git",
                "computedHash": "e" * 64,
            },
            "absolute-path": {
                "source": "owner/repository",
                "sourceType": "github",
                "skillPath": "/etc/SKILL.md",
            },
        },
    )
    lock = read_vercel_lock(lock_path, scope="project")
    assert lock["origins"]["sample-skill"]["source"] == (
        "owner/repository/skills/sample-skill#v2"
    )
    assert lock["origins"]["local-skill"]["source"] == str(
        (lock_path.parent / "../local-skill").resolve()
    )
    assert {item["skill"] for item in lock["invalid"]} == {
        "Bad Skill",
        "absolute-path",
        "lost-host",
    }


def test_vercel_lock_rejects_credential_bearing_source(tmp_path: Path) -> None:
    lock_path = tmp_path / "skills-lock.json"
    write_lock(
        lock_path,
        {
            "sample-skill": {
                "source": "https://token@example.com/repository.git",
                "sourceType": "git",
                "computedHash": "f" * 64,
            }
        },
    )
    lock = read_vercel_lock(lock_path, scope="project")
    assert lock["origins"] == {}
    assert "credentials" in lock["invalid"][0]["error"]


def test_vercel_defaults_are_pinned_and_xdg_aware(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert default_vercel_lock_path("global") == (
        tmp_path / "state/skills/.skill-lock.json"
    )
    assert default_vercel_lock_path("project", cwd=tmp_path) == (
        tmp_path / "skills-lock.json"
    )
    assert vercel_package_from_config({}) == DEFAULT_VERCEL_PACKAGE
    with pytest.raises(ValidationError, match="must be"):
        vercel_package_from_config(
            {"providers": {"vercel_skills": {"package": "evil-package"}}}
        )
    with pytest.raises(ValidationError, match="must remain false"):
        vercel_package_from_config(
            {"providers": {"vercel_skills": {"telemetry": True}}}
        )
