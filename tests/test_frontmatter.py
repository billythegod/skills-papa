from __future__ import annotations

import yaml

from skills_papa.frontmatter import (
    adapt_skill,
    parse_skill_markdown,
    validate_adapted_skill,
)


def source_files() -> tuple[dict[str, bytes], dict[str, int]]:
    skill = b"""---
name: adapter-test
description: Verify target-specific skill conversion.
allowed-tools: Read Write Bash
argument-hint: "[task]"
disable-model-invocation: true
compatibility: macOS
metadata:
  author: test
  opencode/autoinvoke: false
---
# Adapter test
"""
    return (
        {"SKILL.md": skill, "scripts/run.sh": b"#!/bin/sh\n", "assets/icon.txt": b"x"},
        {"SKILL.md": 0o644, "scripts/run.sh": 0o755, "assets/icon.txt": 0o644},
    )


def test_codex_adapter_moves_invocation_policy_to_sidecar() -> None:
    files, modes = source_files()
    result = adapt_skill(files, modes, "codex")
    frontmatter, _ = parse_skill_markdown(result.files["SKILL.md"])
    assert "disable-model-invocation" not in frontmatter
    assert "argument-hint" not in frontmatter
    assert "compatibility" not in frontmatter
    assert frontmatter["metadata"] == {"author": "test"}
    sidecar = yaml.safe_load(result.files["agents/openai.yaml"])
    assert sidecar["policy"]["allow_implicit_invocation"] is False
    assert result.files["scripts/run.sh"] == b"#!/bin/sh\n"
    assert validate_adapted_skill(result, "codex") == []


def test_claude_adapter_reads_codex_invocation_policy() -> None:
    files, modes = source_files()
    frontmatter, body = parse_skill_markdown(files["SKILL.md"])
    frontmatter.pop("disable-model-invocation")
    from skills_papa.frontmatter import render_skill_markdown

    files["SKILL.md"] = render_skill_markdown(frontmatter, body)
    files["agents/openai.yaml"] = b"policy:\n  allow_implicit_invocation: false\n"
    modes["agents/openai.yaml"] = 0o644
    result = adapt_skill(files, modes, "claude")
    converted, _ = parse_skill_markdown(result.files["SKILL.md"])
    assert converted["disable-model-invocation"] is True
    assert "agents/openai.yaml" not in result.files


def test_universal_adapter_uses_portable_subset() -> None:
    files, modes = source_files()
    result = adapt_skill(files, modes, "universal")
    frontmatter, _ = parse_skill_markdown(result.files["SKILL.md"])
    assert "disable-model-invocation" not in frontmatter
    assert "argument-hint" not in frontmatter
    assert "compatibility" in frontmatter
    assert any("invocation policy" in warning for warning in result.warnings)
