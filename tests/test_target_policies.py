import json

import pytest
import yaml

from skills_papa.errors import ValidationError
from skills_papa.frontmatter import (
    adapt_skill,
    parse_skill_markdown,
    render_skill_markdown,
    validate_adapted_skill,
)


ABSENT = object()
SIDECAR = (
    b"interface:\n  display_name: Existing label\n"
    b"policy:\n  allow_implicit_invocation: false\n  other_setting: kept\n"
)


def package(policies=ABSENT, source_disable=True, sidecar=SIDECAR):
    fm = {
        "name": "policy-fixture",
        "description": "Preserve target invocation settings during a content upgrade.",
        "metadata": {"author": "fixture", "opencode/marker": "keep-on-universal"},
    }
    if source_disable is not ABSENT:
        fm["disable-model-invocation"] = source_disable
    if policies is not ABSENT:
        fm["metadata"]["skills-papa/target-policies"] = policies
    files = {
        "SKILL.md": render_skill_markdown(fm, "# Revised instructions\n"),
        "scripts/run.sh": b"#!/bin/sh\n",
    }
    if sidecar is not ABSENT:
        files["agents/openai.yaml"] = sidecar
    return files, {
        key: 0o755 if key == "scripts/run.sh" else 0o644 for key in files
    }


def invocation(result, target):
    if target == "codex":
        return yaml.safe_load(result.files["agents/openai.yaml"])["policy"][
            "allow_implicit_invocation"
        ]
    fm, _ = parse_skill_markdown(result.files["SKILL.md"])
    return not fm["disable-model-invocation"]


def test_one_canonical_source_preserves_different_host_policies():
    policies = '{"codex":true,"claude":false,"opencode":false}'
    files, modes = package(policies)
    for target, expected in (("codex", True), ("claude", False), ("opencode", False)):
        result = adapt_skill(files, modes, target)
        assert invocation(result, target) is expected
        assert result.files["scripts/run.sh"] == files["scripts/run.sh"]
        assert result.modes["scripts/run.sh"] == 0o755
        assert validate_adapted_skill(result, target) == []
    sidecar = yaml.safe_load(adapt_skill(files, modes, "codex").files["agents/openai.yaml"])
    assert sidecar["interface"]["display_name"] == "Existing label"
    assert sidecar["policy"]["other_setting"] == "kept"


@pytest.mark.parametrize("target", ["codex", "claude", "opencode"])
@pytest.mark.parametrize("source_disable", [True, False, ABSENT])
@pytest.mark.parametrize("allow_implicit", [True, False])
def test_target_override_wins_over_conflicting_or_missing_source_policy(
    target, source_disable, allow_implicit
):
    files, modes = package(
        json.dumps({target: allow_implicit}),
        source_disable=source_disable,
        sidecar=ABSENT,
    )
    result = adapt_skill(files, modes, target)
    assert invocation(result, target) is allow_implicit


def test_universal_preserves_original_string_metadata_without_host_policy():
    policies = '{ "codex": true, "claude": false, "opencode": false }'
    files, modes = package(policies)
    original, _ = parse_skill_markdown(files["SKILL.md"])
    universal = adapt_skill(files, modes, "universal")
    fm, _ = parse_skill_markdown(universal.files["SKILL.md"])
    assert "agents/openai.yaml" not in universal.files
    assert "disable-model-invocation" not in fm
    assert fm["metadata"] == original["metadata"]
    assert fm["metadata"]["skills-papa/target-policies"] == policies
    assert validate_adapted_skill(universal, "universal") == []


@pytest.mark.parametrize(
    "policies",
    [
        None,
        False,
        0,
        {"codex": True},
        [True],
        "",
        "not-json",
        "null",
        "[]",
        "true",
        '"false"',
        '{"codex":"false"}',
        '{"codex":0}',
        '{"codex":null}',
        '{"codex":[]}',
        '{"codex":{}}',
        '{"universal":false}',
        '{"unknown":true}',
        '{"codex":true,"codex":false}',
    ],
)
@pytest.mark.parametrize("target", ["codex", "claude", "opencode", "universal"])
def test_invalid_policy_annotations_fail_before_deployment(policies, target):
    files, modes = package(policies)
    with pytest.raises(ValidationError, match="target-policies"):
        adapt_skill(files, modes, target)


@pytest.mark.parametrize("target", ["codex", "claude", "opencode"])
@pytest.mark.parametrize("source_disable", [True, False, ABSENT])
def test_absent_override_preserves_existing_adapter_behavior(target, source_disable):
    files, modes = package(source_disable=source_disable)
    baseline = invocation(adapt_skill(files, modes, target), target)
    other_target = "claude" if target == "codex" else "codex"
    for policies in ("{}", json.dumps({other_target: True})):
        files, modes = package(policies, source_disable=source_disable)
        assert invocation(adapt_skill(files, modes, target), target) is baseline


@pytest.mark.parametrize("policy", ["null", "false", "[]", '"not-a-mapping"'])
def test_codex_rejects_malformed_sidecar_policy_with_clear_validation_error(policy):
    files, modes = package(
        '{"codex":false}',
        source_disable=ABSENT,
        sidecar=f"policy: {policy}\n".encode(),
    )
    with pytest.raises(ValidationError, match="policy must be a mapping"):
        adapt_skill(files, modes, "codex")
