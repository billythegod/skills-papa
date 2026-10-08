from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import yaml

from .errors import ValidationError
from .models import AdapterResult
from .util import validate_skill_name

CODEX_KEYS = {"name", "description", "license", "allowed-tools", "metadata"}
CLAUDE_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "allowed-tools",
    "metadata",
    "argument-hint",
    "disable-model-invocation",
    "user-invocable",
    "model",
    "context",
    "agent",
    "hooks",
}
OPENCODE_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "metadata",
    "disable-model-invocation",
}
UNIVERSAL_KEYS = {
    "name",
    "description",
    "license",
    "compatibility",
    "allowed-tools",
    "metadata",
}


def parse_skill_markdown(data: bytes) -> tuple[dict[str, Any], str]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"SKILL.md must be UTF-8: {exc}") from exc
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValidationError(
            "SKILL.md must start with YAML frontmatter delimiter '---'"
        )
    closing = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            closing = index
            break
    if closing is None:
        raise ValidationError(
            "SKILL.md is missing the closing YAML frontmatter delimiter"
        )
    yaml_text = "".join(lines[1:closing])
    try:
        frontmatter = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ValidationError(f"invalid YAML frontmatter: {exc}") from exc
    if not isinstance(frontmatter, dict):
        raise ValidationError("SKILL.md frontmatter must be a mapping")
    name = frontmatter.get("name")
    description = frontmatter.get("description")
    validate_skill_name(name)
    if not isinstance(description, str) or not description.strip():
        raise ValidationError("SKILL.md description must be a non-empty string")
    if len(description.strip()) > 1024:
        raise ValidationError("SKILL.md description exceeds 1024 characters")
    return frontmatter, "".join(lines[closing + 1 :])


def render_skill_markdown(frontmatter: dict[str, Any], body: str) -> bytes:
    dumped = yaml.safe_dump(
        frontmatter,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=1000,
    ).rstrip()
    normalized_body = body
    if normalized_body and not normalized_body.startswith("\n"):
        normalized_body = "\n" + normalized_body
    return f"---\n{dumped}\n---\n{normalized_body.lstrip(chr(10))}".encode()


def _load_openai_yaml(
    source_files: dict[str, bytes], warnings: list[str]
) -> dict[str, Any]:
    raw = source_files.get("agents/openai.yaml")
    if raw is None:
        return {}
    try:
        value = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        warnings.append(f"ignored invalid agents/openai.yaml: {exc}")
        return {}
    if not isinstance(value, dict):
        warnings.append("ignored agents/openai.yaml because it is not a mapping")
        return {}
    return value


def _policy_from_openai(openai: dict[str, Any]) -> bool | None:
    policy = openai.get("policy")
    if isinstance(policy, dict) and isinstance(
        policy.get("allow_implicit_invocation"), bool
    ):
        return bool(policy["allow_implicit_invocation"])
    return None


def _filtered(frontmatter: dict[str, Any], keys: set[str]) -> dict[str, Any]:
    return {key: deepcopy(value) for key, value in frontmatter.items() if key in keys}


def _unique_policy_hosts(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(
                "metadata.skills-papa/target-policies contains a duplicate host"
            )
        result[key] = value
    return result


def _target_invocation_policy(
    frontmatter: dict[str, Any], target: str
) -> bool | None:
    metadata = frontmatter.get("metadata")
    key = "skills-papa/target-policies"
    if not isinstance(metadata, dict) or key not in metadata:
        return None
    raw = metadata[key]
    message = (
        "metadata.skills-papa/target-policies must be a JSON string mapping "
        "supported hosts (codex, claude, opencode) to boolean allow-implicit values"
    )
    if not isinstance(raw, str):
        raise ValidationError(message)
    try:
        policies = json.loads(raw, object_pairs_hook=_unique_policy_hosts)
    except json.JSONDecodeError as exc:
        raise ValidationError(message) from exc
    if not isinstance(policies, dict) or any(
        host not in {"codex", "claude", "opencode"}
        or not isinstance(value, bool)
        for host, value in policies.items()
    ):
        raise ValidationError(message)
    return policies.get(target)


def adapt_skill(
    source_files: dict[str, bytes], modes: dict[str, int], target: str
) -> AdapterResult:
    if "SKILL.md" not in source_files:
        raise ValidationError("skill manifest has no SKILL.md")
    frontmatter, body = parse_skill_markdown(source_files["SKILL.md"])
    warnings: list[str] = []
    output = {
        path: data
        for path, data in source_files.items()
        if path not in {"SKILL.md", "agents/openai.yaml"}
    }
    output_modes = {path: modes[path] for path in output}
    openai = _load_openai_yaml(source_files, warnings)
    implicit = _policy_from_openai(openai)
    disable = frontmatter.get("disable-model-invocation")
    if not isinstance(disable, bool):
        disable = None

    # Preserve verified host invocation differences when migration variants
    # are replaced by a single canonical source.
    policy_override = _target_invocation_policy(frontmatter, target)
    if policy_override is not None:
        implicit = policy_override
        disable = not policy_override

    if target == "codex":
        rendered = _filtered(frontmatter, CODEX_KEYS)
        metadata = rendered.get("metadata")
        if isinstance(metadata, dict):
            rendered["metadata"] = {
                key: value
                for key, value in metadata.items()
                if not str(key).startswith("opencode/")
            }
        if disable is not None:
            if "policy" in openai and not isinstance(openai["policy"], dict):
                raise ValidationError("agents/openai.yaml policy must be a mapping")
            openai.setdefault("policy", {})["allow_implicit_invocation"] = not disable
        if openai:
            output["agents/openai.yaml"] = yaml.safe_dump(
                openai, allow_unicode=True, sort_keys=False, width=1000
            ).encode("utf-8")
            output_modes["agents/openai.yaml"] = 0o644
    elif target == "claude":
        rendered = _filtered(frontmatter, CLAUDE_KEYS)
        metadata = rendered.get("metadata")
        if isinstance(metadata, dict):
            rendered["metadata"] = {
                key: value
                for key, value in metadata.items()
                if not str(key).startswith("opencode/")
            }
        if policy_override is not None:
            rendered["disable-model-invocation"] = not policy_override
        elif disable is None and implicit is not None:
            rendered["disable-model-invocation"] = not implicit
    elif target == "opencode":
        rendered = _filtered(frontmatter, OPENCODE_KEYS)
        if policy_override is not None:
            rendered["disable-model-invocation"] = not policy_override
        elif disable is None and implicit is not None:
            rendered["disable-model-invocation"] = not implicit
    elif target == "universal":
        rendered = _filtered(frontmatter, UNIVERSAL_KEYS)
        if disable is not None or implicit is not None:
            warnings.append("universal target omits agent-specific invocation policy")
    else:
        raise ValidationError(f"unsupported target: {target}")

    removed = sorted(set(frontmatter) - set(rendered))
    if removed:
        warnings.append(
            f"{target} adapter omitted unsupported frontmatter keys: {', '.join(removed)}"
        )
    output["SKILL.md"] = render_skill_markdown(rendered, body)
    output_modes["SKILL.md"] = 0o644
    return AdapterResult(files=output, modes=output_modes, warnings=warnings)


def validate_adapted_skill(result: AdapterResult, target: str) -> list[str]:
    frontmatter, _ = parse_skill_markdown(result.files["SKILL.md"])
    warnings: list[str] = []
    allowed = {
        "codex": CODEX_KEYS,
        "claude": CLAUDE_KEYS,
        "opencode": OPENCODE_KEYS,
        "universal": UNIVERSAL_KEYS,
    }[target]
    unexpected = sorted(set(frontmatter) - allowed)
    if unexpected:
        raise ValidationError(
            f"{target} adapter left unsupported keys: {', '.join(unexpected)}"
        )
    if target == "codex" and "agents/openai.yaml" in result.files:
        try:
            sidecar = yaml.safe_load(result.files["agents/openai.yaml"].decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ValidationError(
                f"generated Codex agents/openai.yaml is invalid: {exc}"
            ) from exc
        if not isinstance(sidecar, dict):
            raise ValidationError(
                "generated Codex agents/openai.yaml must be a mapping"
            )
    return warnings
