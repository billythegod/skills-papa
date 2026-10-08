from __future__ import annotations

import pytest
from conftest import make_skill

from skills_papa.errors import ConflictError


def test_migrate_preserves_target_variants_and_skips_invalid(managed) -> None:
    state, manager, targets = managed
    universal = make_skill(targets["universal"], "shared-skill", body="# Universal\n")
    codex = make_skill(targets["codex"], "shared-skill", body="# Codex variant\n")
    broken = targets["claude"] / "broken-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: broken-skill\nmetadata:\n  flag: false\n    author: bad\n---\n"
    )
    before_universal = (universal / "SKILL.md").read_bytes()
    before_codex = (codex / "SKILL.md").read_bytes()
    dry = manager.migrate(apply=False)
    assert dry["invalid_count"] == 1
    assert dry["skills"]["shared-skill"]["canonical_target"] == "universal"
    assert "codex" in dry["skills"]["shared-skill"]["target_variants"]
    applied = manager.migrate(apply=True)
    assert applied["apply"] is True
    entry = state.registry()["skills"]["shared-skill"]
    assert entry["manifest"] != entry["target_manifests"]["codex"]
    assert state.registry()["deployments"]["codex"]["shared-skill"]["adopted"] is True
    assert (universal / "SKILL.md").read_bytes() == before_universal
    assert (codex / "SKILL.md").read_bytes() == before_codex


def test_audit_reports_divergence(managed) -> None:
    _, manager, targets = managed
    make_skill(targets["universal"], "shared-skill", body="# One\n")
    make_skill(targets["codex"], "shared-skill", body="# Two\n")
    result = manager.audit()
    assert result["skill_count"] == 1
    assert result["divergent_count"] == 1
    assert "shared-skill" in result["divergent"]


def test_audit_reports_legacy_in_place_backups(managed) -> None:
    _, manager, targets = managed
    make_skill(targets["codex"], "sample-skill.backup-20260727")
    make_skill(targets["universal"], "sample-skill.bak")
    result = manager.audit()
    assert result["skill_count"] == 0
    assert result["legacy_backup_count"] == 2
    assert {(item["target"], item["name"]) for item in result["legacy_backups"]} == {
        ("codex", "sample-skill.backup-20260727"),
        ("universal", "sample-skill.bak"),
    }


def test_invalid_target_can_be_replaced_from_valid_variant(managed) -> None:
    state, manager, targets = managed
    make_skill(targets["codex"], "repair-skill", body="# Valid Codex source\n")
    broken = targets["universal"] / "repair-skill"
    broken.mkdir(parents=True)
    (broken / "SKILL.md").write_text(
        "---\nname: repair-skill\nmetadata:\n  flag: false\n    author: bad\n---\n"
    )
    manager.migrate(apply=True)
    assert state.registry()["skills"]["repair-skill"]["enabled_targets"] == ["codex"]
    result = manager.set_enabled(
        "repair-skill", "universal", True, apply=True, on_conflict="backup"
    )
    assert result["transaction"].startswith("enable-")
    assert "Valid Codex source" in (broken / "SKILL.md").read_text()


def test_owner_managed_skill_is_never_adopted_or_registered(managed) -> None:
    state, manager, targets = managed
    external = make_skill(targets["universal"], "vendor-skill")

    dry = manager.set_owner_managed(
        "vendor-skill",
        owner="vendor-client",
        reason="integrity checked by its client",
        apply=False,
    )
    assert dry["changes"][0]["action"] == "exclude-owner-managed"
    manager.set_owner_managed(
        "vendor-skill",
        owner="vendor-client",
        reason="integrity checked by its client",
        apply=True,
    )

    migration = manager.migrate(apply=False)
    assert "vendor-skill" not in migration["skills"]
    assert migration["owner_managed_count"] == 1
    assert migration["owner_managed"][0]["owner"] == "vendor-client"
    manager.migrate(apply=True)
    assert "vendor-skill" not in state.registry()["skills"]
    assert external.is_dir()

    pending = state.build_pending_manifest(external)
    with pytest.raises(ConflictError, match="managed by vendor-client"):
        manager.plan_register(pending, ["universal"])

    audit = manager.audit()
    assert audit["owner_managed_count"] == 1
    assert audit["owner_managed"]["vendor-skill"]["placements"]["universal"]

    manager.set_owner_managed("vendor-skill", owner=None, apply=True)
    manager.migrate(apply=True)
    assert "vendor-skill" in state.registry()["skills"]


def test_owner_managed_rule_requires_unregistered_skill(managed) -> None:
    state, manager, targets = managed
    skill = make_skill(targets["universal"], "registered-skill")
    pending = state.build_pending_manifest(skill)
    manager.apply_register(pending, ["universal"])

    with pytest.raises(ConflictError, match="still registered"):
        manager.set_owner_managed(
            "registered-skill", owner="vendor-client", apply=False
        )
