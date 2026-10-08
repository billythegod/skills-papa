from __future__ import annotations

from pathlib import Path

import pytest

from skills_papa.manager import Manager
from skills_papa.state import State


@pytest.fixture
def managed(tmp_path: Path) -> tuple[State, Manager, dict[str, Path]]:
    targets = {
        name: tmp_path / "targets" / name
        for name in ("codex", "claude", "opencode", "universal")
    }
    state = State(tmp_path / "state")
    manager = Manager(state)
    manager.init(
        apply=True,
        target_overrides={name: str(path) for name, path in targets.items()},
    )
    return state, manager, targets


def make_skill(
    root: Path,
    name: str = "sample-skill",
    *,
    body: str = "# Sample\n\nDo the work.\n",
    extra_frontmatter: str = "",
) -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: A sample skill for lifecycle tests.\n"
        f"{extra_frontmatter}"
        "---\n"
        f"{body}",
        encoding="utf-8",
    )
    scripts = skill / "scripts"
    scripts.mkdir()
    runner = scripts / "run.sh"
    runner.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
    runner.chmod(0o755)
    return skill
