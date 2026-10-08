from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml
from conftest import make_skill

from skills_papa.errors import ConflictError, StateError, ValidationError
from skills_papa.frontmatter import parse_skill_markdown
from skills_papa.manager import Manager
from skills_papa.state import State


def register(
    state: State,
    manager: Manager,
    skill: Path,
    targets: list[str] | None = None,
) -> None:
    pending = state.build_pending_manifest(skill)
    manager.apply_register(pending, targets or ["codex", "claude", "opencode"])


def test_init_dry_run_writes_nothing(tmp_path: Path) -> None:
    state = State(tmp_path / "state")
    manager = Manager(state)
    result = manager.init(apply=False)
    assert result["apply"] is False
    assert not state.home.exists()


def test_register_sync_and_doctor(managed, tmp_path: Path) -> None:
    state, manager, targets = managed
    skill = make_skill(
        tmp_path / "input",
        extra_frontmatter=(
            "disable-model-invocation: true\n"
            "argument-hint: '[task]'\n"
            "metadata:\n"
            "  opencode/autoinvoke: false\n"
            "  author: tester\n"
        ),
    )
    register(state, manager, skill)
    dry = manager.plan_sync(["sample-skill"])
    assert len(dry["changes"]) == 3
    assert not targets["codex"].exists()
    applied = manager.apply_sync(["sample-skill"])
    assert applied["transaction"].startswith("sync-")
    codex_skill = targets["codex"] / "sample-skill"
    codex_frontmatter, _ = parse_skill_markdown((codex_skill / "SKILL.md").read_bytes())
    assert "disable-model-invocation" not in codex_frontmatter
    sidecar = yaml.safe_load((codex_skill / "agents/openai.yaml").read_text())
    assert sidecar["policy"]["allow_implicit_invocation"] is False
    assert os.access(codex_skill / "scripts/run.sh", os.X_OK)
    assert manager.doctor()["ok"] is True


def test_unmanaged_conflict_requires_explicit_backup(managed, tmp_path: Path) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input")
    register(state, manager, skill, ["codex"])
    unmanaged = targets["codex"] / "sample-skill"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: Existing unmanaged skill.\n---\nold\n"
    )
    with pytest.raises(ConflictError):
        manager.plan_sync(["sample-skill"], on_conflict="error")
    applied = manager.apply_sync(["sample-skill"], on_conflict="backup")
    assert "Do the work" in (unmanaged / "SKILL.md").read_text()
    central_backup = (
        state.backups_dir / applied["transaction"] / "codex" / "sample-skill"
    )
    assert central_backup.is_dir()
    assert "Existing unmanaged" in (central_backup / "SKILL.md").read_text()
    assert not list(targets["codex"].glob("*.backup*"))
    assert not list(targets["codex"].glob("*.bak"))
    rollback = manager.rollback(applied["transaction"], apply=True)
    assert rollback["rollback_id"].startswith("rollback-")
    assert "Existing unmanaged" in (unmanaged / "SKILL.md").read_text()


def test_disable_enable_remove_and_out_of_order_rollback(
    managed, tmp_path: Path
) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input")
    register(state, manager, skill, ["opencode"])
    first = manager.apply_sync(["sample-skill"])
    manager.set_enabled("sample-skill", "opencode", False, apply=True)
    assert not (targets["opencode"] / "sample-skill").exists()
    manager.set_enabled("sample-skill", "opencode", True, apply=True)
    assert (targets["opencode"] / "sample-skill").is_dir()
    with pytest.raises(ConflictError):
        manager.rollback(first["transaction"], apply=True)
    removed = manager.remove("sample-skill", apply=True)
    assert removed["transaction"].startswith("remove-")
    assert "sample-skill" not in state.registry()["skills"]
    assert not (targets["opencode"] / "sample-skill").exists()


def test_symlinked_input_is_rejected(managed, tmp_path: Path) -> None:
    state, _, _ = managed
    skill = make_skill(tmp_path / "input")
    outside = tmp_path / "outside.txt"
    outside.write_text("secret")
    os.symlink(outside, skill / "references-link")
    with pytest.raises(ValidationError):
        state.build_pending_manifest(skill)


def test_deployed_support_file_does_not_mutate_object_store(
    managed, tmp_path: Path
) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input")
    pending = state.build_pending_manifest(skill)
    manager.apply_register(pending, ["codex"])
    manager.apply_sync(["sample-skill"])
    manifest = state.load_manifest("sample-skill", pending.content_id)
    script_record = next(
        item for item in manifest["files"] if item["path"] == "scripts/run.sh"
    )
    object_path = state.object_path(script_record["sha256"], script_record["mode"])
    deployed = targets["codex"] / "sample-skill/scripts/run.sh"
    deployed.chmod(0o755)
    deployed.write_text("#!/bin/sh\necho changed\n")
    assert object_path.read_text() == "#!/bin/sh\necho ok\n"
    assert manager.doctor()["ok"] is False


def test_recover_interrupted_transaction_blocks_other_mutations(
    managed, tmp_path: Path
) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input")
    register(state, manager, skill, ["codex"])
    applied = manager.apply_sync(["sample-skill"])
    transaction = applied["transaction"]
    journal_path = state.transactions_dir / f"{transaction}.json"
    import json

    journal = json.loads(journal_path.read_text())
    journal["status"] = "applying"
    journal_path.write_text(json.dumps(journal))
    with pytest.raises(StateError):
        manager.remove("sample-skill", apply=True)
    recovery = manager.recover(transaction, apply=True)
    assert recovery["apply"] is True
    assert not (targets["codex"] / "sample-skill").exists()
    assert state.registry()["deployments"].get("codex", {}).get("sample-skill") is None


def test_rollback_refuses_drifted_destination(managed, tmp_path: Path) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input")
    register(state, manager, skill, ["codex"])
    applied = manager.apply_sync(["sample-skill"])
    deployed = targets["codex"] / "sample-skill/SKILL.md"
    deployed.chmod(0o644)
    deployed.write_text(deployed.read_text() + "\nmanual edit\n")
    with pytest.raises(ConflictError):
        manager.rollback(applied["transaction"], apply=True)
    assert "manual edit" in deployed.read_text()


def test_recover_backup_before_replace_window(managed, tmp_path: Path) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input")
    register(state, manager, skill, ["codex"])
    destination = targets["codex"] / "sample-skill"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text(
        "---\nname: sample-skill\ndescription: Original unmanaged skill.\n---\noriginal\n"
    )
    applied = manager.apply_sync(["sample-skill"], on_conflict="backup")
    transaction = applied["transaction"]
    journal_path = state.transactions_dir / f"{transaction}.json"
    import json

    journal = json.loads(journal_path.read_text())
    operation = journal["operations"][0]
    stage = Path(operation["stage"])
    os.replace(destination, stage)
    operation["applied"] = False
    journal["status"] = "applying"
    journal_path.write_text(json.dumps(journal))
    manager.recover(transaction, apply=True)
    assert "Original unmanaged" in (destination / "SKILL.md").read_text()
    assert not stage.exists()


def rewrite_skill(skill: Path, *, version: str, body: str) -> None:
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: sample-skill\n"
        "description: A sample skill for lifecycle tests.\n"
        "metadata:\n"
        f"  version: {version}\n"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )


def test_upgrade_reports_versions_updates_all_targets_and_rolls_back(
    managed, tmp_path: Path
) -> None:
    state, manager, targets = managed
    skill = make_skill(
        tmp_path / "input",
        body="# Version one\n",
        extra_frontmatter="metadata:\n  version: 1.0.0\n",
    )
    pending_v1 = state.build_pending_manifest(
        skill, origin={"type": "local", "path": str(skill)}
    )
    manager.apply_register(pending_v1, ["codex", "claude"])
    manager.apply_sync(["sample-skill"])

    rewrite_skill(skill, version="2.0.0", body="# Version two\n")
    pending_v2 = state.build_pending_manifest(
        skill, origin={"type": "local", "path": str(skill)}
    )
    plan = manager.plan_upgrade(pending_v2)
    assert plan["status"] == "update-available"
    assert plan["previous_version"] == "1.0.0"
    assert plan["version"] == "2.0.0"
    assert plan["enabled_targets"] == ["claude", "codex"]
    assert not "Version two" in (targets["codex"] / "sample-skill/SKILL.md").read_text()

    applied = manager.apply_upgrade(pending_v2)
    assert applied["transaction"].startswith("upgrade-")
    assert state.registry()["skills"]["sample-skill"]["version"] == "2.0.0"
    for target in ("codex", "claude"):
        assert "Version two" in (targets[target] / "sample-skill/SKILL.md").read_text()
        assert (
            state.backups_dir / applied["transaction"] / target / "sample-skill"
        ).is_dir()

    manager.rollback(applied["transaction"], apply=True)
    assert state.registry()["skills"]["sample-skill"]["version"] == "1.0.0"
    for target in ("codex", "claude"):
        assert "Version one" in (targets[target] / "sample-skill/SKILL.md").read_text()
    assert manager.doctor()["ok"] is True


def test_upgrade_current_is_a_noop(managed, tmp_path: Path) -> None:
    state, manager, _ = managed
    skill = make_skill(
        tmp_path / "input", extra_frontmatter="metadata:\n  version: 1.0.0\n"
    )
    origin = {"type": "local", "path": str(skill)}
    pending = state.build_pending_manifest(skill, origin=origin)
    manager.apply_register(pending, ["codex"])
    manager.apply_sync(["sample-skill"])
    revision = state.registry()["revision"]

    plan = manager.plan_upgrade(pending)
    assert plan["status"] == "current"
    applied = manager.apply_upgrade(pending)
    assert applied["transaction"] is None
    assert state.registry()["revision"] == revision


def test_upgrade_accepts_new_origin_and_retires_migrated_variants(
    managed, tmp_path: Path
) -> None:
    state, manager, targets = managed
    make_skill(targets["codex"], body="# Codex variant\n")
    make_skill(targets["universal"], body="# Universal variant\n")
    manager.migrate(apply=True)
    entry = state.registry()["skills"]["sample-skill"]
    assert entry["target_manifests"]

    upstream = make_skill(
        tmp_path / "upstream",
        body="# Canonical upstream\n",
        extra_frontmatter="metadata:\n  version: 3.0.0\n",
    )
    pending = state.build_pending_manifest(
        upstream, origin={"type": "local", "path": str(upstream)}
    )
    applied = manager.apply_upgrade(pending)
    assert applied["retired_target_variants"]
    assert state.registry()["skills"]["sample-skill"]["target_manifests"] == {}
    assert state.registry()["skills"]["sample-skill"]["enabled_targets"] == [
        "codex",
        "universal",
    ]
    for target in ("codex", "universal"):
        assert (
            "Canonical upstream"
            in (targets[target] / "sample-skill/SKILL.md").read_text()
        )


def test_upgrade_staging_failure_leaves_registry_and_deployments_unchanged(
    managed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state, manager, targets = managed
    skill = make_skill(tmp_path / "input", body="# Old\n")
    origin = {"type": "local", "path": str(skill)}
    manager.apply_register(
        state.build_pending_manifest(skill, origin=origin),
        [
            "codex",
            "claude",
        ],
    )
    manager.apply_sync(["sample-skill"])
    registry_before = state.registry()
    rewrite_skill(skill, version="2.0.0", body="# New\n")
    pending = state.build_pending_manifest(skill, origin=origin)
    original_stage = manager._stage_skill
    calls = 0

    def fail_second_stage(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise StateError("injected staging failure")
        return original_stage(*args)

    monkeypatch.setattr(manager, "_stage_skill", fail_second_stage)
    with pytest.raises(StateError, match="injected staging failure"):
        manager.apply_upgrade(pending)
    assert state.registry() == registry_before
    for target in ("codex", "claude"):
        assert "# Old" in (targets[target] / "sample-skill/SKILL.md").read_text()


def test_import_vercel_origin_is_registry_only_and_rollback_safe(
    managed, tmp_path: Path
) -> None:
    state, manager, targets = managed
    make_skill(targets["codex"])
    manager.migrate(apply=True)
    deployed_before = (targets["codex"] / "sample-skill/SKILL.md").read_bytes()
    origin = {
        "type": "vercel-skills",
        "fetcher": "vercel-labs/skills",
        "source": "owner/repository/skills/sample-skill#main",
        "source_id": "owner/repository",
        "source_type": "github",
        "skill": "sample-skill",
        "package": "skills@1.5.23",
    }

    dry = manager.plan_import_origins({"sample-skill": origin})
    assert dry["counts"]["linked"] == 1
    assert state.registry()["skills"]["sample-skill"]["origin"]["type"] == ("migration")
    applied = manager.apply_import_origins({"sample-skill": origin})
    assert applied["transaction"].startswith("import-lock-")
    assert state.registry()["skills"]["sample-skill"]["origin"] == origin
    assert (targets["codex"] / "sample-skill/SKILL.md").read_bytes() == deployed_before

    manager.rollback(applied["transaction"], apply=True)
    assert state.registry()["skills"]["sample-skill"]["origin"]["type"] == ("migration")
    assert (targets["codex"] / "sample-skill/SKILL.md").read_bytes() == deployed_before


def test_import_vercel_origin_refuses_authoritative_source_without_override(
    managed, tmp_path: Path
) -> None:
    state, manager, _ = managed
    skill = make_skill(tmp_path / "input")
    manager.apply_register(state.build_pending_manifest(skill), ["codex"])
    origin = {
        "type": "vercel-skills",
        "fetcher": "vercel-labs/skills",
        "source": "owner/repository",
        "source_id": "owner/repository",
        "source_type": "github",
        "skill": "sample-skill",
    }
    dry = manager.plan_import_origins({"sample-skill": origin})
    assert dry["counts"]["conflicts"] == 1
    with pytest.raises(ConflictError, match="replace authoritative origins"):
        manager.apply_import_origins({"sample-skill": origin})
    applied = manager.apply_import_origins(
        {"sample-skill": origin}, replace_origin=True
    )
    assert applied["transaction"].startswith("import-lock-")
