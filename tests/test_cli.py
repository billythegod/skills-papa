from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from conftest import make_skill


def invoke(project_root: Path, *arguments: object) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(project_root / "src")
    return subprocess.run(
        [sys.executable, "-m", "skills_papa", "--json", *map(str, arguments)],
        cwd=project_root,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )


def test_cli_end_to_end(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    state = tmp_path / "state"
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    opencode = tmp_path / "opencode"
    universal = tmp_path / "universal"
    skill = make_skill(tmp_path / "input")
    init = invoke(
        project_root,
        "--home",
        state,
        "init",
        "--target",
        f"codex={codex}",
        "--target",
        f"claude={claude}",
        "--target",
        f"opencode={opencode}",
        "--target",
        f"universal={universal}",
        "--apply",
    )
    assert init.returncode == 0, init.stderr
    added = invoke(
        project_root,
        "--home",
        state,
        "add",
        skill,
        "--target",
        "codex",
        "--apply",
        "--sync",
    )
    assert added.returncode == 0, added.stderr
    payload = json.loads(added.stdout)
    assert payload["sync"]["transaction"].startswith("sync-")
    assert (codex / "sample-skill/SKILL.md").is_file()
    doctor = invoke(project_root, "--home", state, "doctor")
    assert doctor.returncode == 0, doctor.stderr
    assert json.loads(doctor.stdout)["ok"] is True


def test_cli_upgrade_source_override_and_all_check(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    state = tmp_path / "state"
    codex = tmp_path / "codex"
    source = make_skill(
        tmp_path / "source",
        body="# One\n",
        extra_frontmatter="metadata:\n  version: 1.0.0\n",
    )
    initialized = invoke(
        project_root,
        "--home",
        state,
        "init",
        "--target",
        f"codex={codex}",
        "--apply",
    )
    assert initialized.returncode == 0, initialized.stderr
    added = invoke(
        project_root,
        "--home",
        state,
        "add",
        source,
        "--target",
        "codex",
        "--apply",
        "--sync",
    )
    assert added.returncode == 0, added.stderr

    replacement = make_skill(
        tmp_path / "replacement",
        body="# Two\n",
        extra_frontmatter="metadata:\n  version: 2.0.0\n",
    )
    dry = invoke(
        project_root,
        "--home",
        state,
        "upgrade",
        "sample-skill",
        "--source",
        replacement,
    )
    assert dry.returncode == 0, dry.stderr
    dry_payload = json.loads(dry.stdout)
    assert dry_payload["status"] == "update-available"
    assert dry_payload["previous_version"] == "1.0.0"
    assert dry_payload["version"] == "2.0.0"
    assert "# One" in (codex / "sample-skill/SKILL.md").read_text()

    applied = invoke(
        project_root,
        "--home",
        state,
        "upgrade",
        "sample-skill",
        "--source",
        replacement,
        "--apply",
    )
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["transaction"].startswith("upgrade-")
    assert "# Two" in (codex / "sample-skill/SKILL.md").read_text()

    all_check = invoke(project_root, "--home", state, "upgrade", "--all")
    assert all_check.returncode == 0, all_check.stderr
    payload = json.loads(all_check.stdout)
    assert payload["counts"] == {
        "checked": 1,
        "current": 1,
        "redeploy_required": 0,
        "unavailable": 0,
        "update_available": 0,
    }
    refused = invoke(project_root, "--home", state, "upgrade", "--all", "--apply")
    assert refused.returncode == 2
    assert "check-only" in json.loads(refused.stdout)["error"]["message"]


def test_cli_imports_vercel_lock_without_touching_deployment(tmp_path: Path) -> None:
    project_root = Path(__file__).parents[1]
    state = tmp_path / "state"
    codex = tmp_path / "codex"
    claude = tmp_path / "claude"
    opencode = tmp_path / "opencode"
    universal = tmp_path / "universal"
    make_skill(codex)
    initialized = invoke(
        project_root,
        "--home",
        state,
        "init",
        "--target",
        f"codex={codex}",
        "--target",
        f"claude={claude}",
        "--target",
        f"opencode={opencode}",
        "--target",
        f"universal={universal}",
        "--apply",
    )
    assert initialized.returncode == 0, initialized.stderr
    migrated = invoke(project_root, "--home", state, "migrate", "--apply")
    assert migrated.returncode == 0, migrated.stderr
    before = (codex / "sample-skill/SKILL.md").read_bytes()
    lock_path = tmp_path / "skills-lock.json"
    lock_path.write_text(
        json.dumps(
            {
                "version": 1,
                "skills": {
                    "sample-skill": {
                        "source": "owner/repository",
                        "sourceType": "github",
                        "skillPath": "skills/sample-skill/SKILL.md",
                        "computedHash": "a" * 64,
                    },
                    "not-installed": {
                        "source": "owner/repository",
                        "sourceType": "github",
                        "skillPath": "skills/not-installed/SKILL.md",
                        "computedHash": "b" * 64,
                    },
                    "Bad Skill": {
                        "source": "owner/repository",
                        "sourceType": "github",
                    },
                },
            }
        ),
        encoding="utf-8",
    )

    dry = invoke(
        project_root,
        "--home",
        state,
        "import-lock",
        lock_path,
        "--scope",
        "project",
    )
    assert dry.returncode == 0, dry.stderr
    payload = json.loads(dry.stdout)
    assert payload["counts"]["linked"] == 1
    assert payload["counts"]["not_registered"] == 1
    assert payload["invalid_count"] == 1
    assert (codex / "sample-skill/SKILL.md").read_bytes() == before

    refused = invoke(
        project_root,
        "--home",
        state,
        "import-lock",
        lock_path,
        "--scope",
        "project",
        "--apply",
    )
    assert refused.returncode == 2
    assert "--skip-invalid" in json.loads(refused.stdout)["error"]["message"]

    applied = invoke(
        project_root,
        "--home",
        state,
        "import-lock",
        lock_path,
        "--scope",
        "project",
        "--skip-invalid",
        "--apply",
    )
    assert applied.returncode == 0, applied.stderr
    applied_payload = json.loads(applied.stdout)
    assert applied_payload["transaction"].startswith("import-lock-")
    registry = json.loads((state / "registry.json").read_text())
    origin = registry["skills"]["sample-skill"]["origin"]
    assert origin["type"] == "vercel-skills"
    assert origin["package"] == "skills@1.5.23"
    assert (codex / "sample-skill/SKILL.md").read_bytes() == before
