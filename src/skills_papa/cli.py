from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .errors import SkillsPapaError, ValidationError
from .fetch import resolve_source
from .manager import Manager
from .models import VERSION
from .state import State
from .vercel import (
    default_vercel_lock_path,
    read_vercel_lock,
    vercel_package_from_config,
)


def default_state_home() -> Path:
    configured = os.environ.get("SKILLS_PAPA_HOME")
    return (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".local/share/skills-papa"
    )


def _target_override(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("target override must be NAME=PATH")
    name, path = value.split("=", 1)
    if not name or not path:
        raise argparse.ArgumentTypeError("target override must be NAME=PATH")
    return name, path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="skills-papa",
        description="Safely manage Agent Skills across multiple coding agents.",
    )
    parser.add_argument(
        "--home", type=Path, default=default_state_home(), help="state directory"
    )
    parser.add_argument(
        "--json", action="store_true", help="emit machine-readable JSON"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser(
        "init", help="initialize an isolated Skills Papa state"
    )
    init_parser.add_argument(
        "--target", action="append", default=[], type=_target_override
    )
    init_parser.add_argument(
        "--apply", action="store_true", help="write the state directory"
    )

    add_parser = subparsers.add_parser(
        "add",
        aliases=["import"],
        help="register one skill from a local or remote source",
    )
    add_parser.add_argument("source")
    add_parser.add_argument(
        "--skill", help="select a skill when the source contains several"
    )
    add_parser.add_argument(
        "--subdir", help="subdirectory within a local path or git repository"
    )
    add_parser.add_argument(
        "--fetcher",
        choices=["vercel", "native", "skills-cli"],
        default="vercel",
        help="source resolver (skills-cli is a compatibility alias for vercel)",
    )
    add_parser.add_argument(
        "--target",
        action="append",
        choices=["codex", "claude", "opencode", "universal"],
    )
    add_parser.add_argument(
        "--replace", action="store_true", help="replace an existing source"
    )
    add_parser.add_argument(
        "--sync", action="store_true", help="deploy after registering"
    )
    add_parser.add_argument(
        "--on-conflict", choices=["error", "backup"], default="error"
    )
    add_parser.add_argument("--apply", action="store_true")

    update_parser = subparsers.add_parser(
        "update", help="compatibility alias for an atomic skill upgrade"
    )
    update_parser.add_argument("skill")
    update_parser.add_argument(
        "--sync", action=argparse.BooleanOptionalAction, default=True
    )
    update_parser.add_argument(
        "--on-conflict", choices=["error", "backup"], default="error"
    )
    update_parser.add_argument("--apply", action="store_true")

    upgrade_parser = subparsers.add_parser(
        "upgrade",
        help="check or atomically upgrade a registered skill from its source",
    )
    upgrade_parser.add_argument("skill", nargs="?")
    upgrade_parser.add_argument(
        "--all",
        dest="all_skills",
        action="store_true",
        help="check every registered skill (check-only in this release)",
    )
    upgrade_parser.add_argument(
        "--source",
        help="override the registered source for this upgrade and future checks",
    )
    upgrade_parser.add_argument(
        "--subdir", help="subdirectory within the replacement source"
    )
    upgrade_parser.add_argument("--fetcher", choices=["vercel", "native", "skills-cli"])
    upgrade_parser.add_argument(
        "--on-conflict", choices=["error", "backup"], default="error"
    )
    upgrade_parser.add_argument("--apply", action="store_true")

    sync_parser = subparsers.add_parser(
        "sync", help="render and deploy registered skills"
    )
    sync_parser.add_argument("skills", nargs="*")
    sync_parser.add_argument(
        "--target",
        action="append",
        choices=["codex", "claude", "opencode", "universal"],
    )
    sync_parser.add_argument(
        "--on-conflict", choices=["error", "backup"], default="error"
    )
    sync_parser.add_argument("--apply", action="store_true")

    for command, help_text in (
        ("enable", "enable and deploy a skill for one target"),
        ("disable", "disable and safely remove one target deployment"),
    ):
        item = subparsers.add_parser(command, help=help_text)
        item.add_argument("skill")
        item.add_argument(
            "--target",
            required=True,
            choices=["codex", "claude", "opencode", "universal"],
        )
        if command == "enable":
            item.add_argument(
                "--on-conflict", choices=["error", "backup"], default="error"
            )
        item.add_argument("--apply", action="store_true")

    remove_parser = subparsers.add_parser(
        "remove", help="unregister a skill and its managed deployments"
    )
    remove_parser.add_argument("skill")
    remove_parser.add_argument("--apply", action="store_true")

    exclude_parser = subparsers.add_parser(
        "exclude", help="leave a skill under an external owner's lifecycle"
    )
    exclude_parser.add_argument("skill")
    exclude_parser.add_argument("--owner", required=True)
    exclude_parser.add_argument("--reason", default="")
    exclude_parser.add_argument("--apply", action="store_true")

    include_parser = subparsers.add_parser(
        "include", help="allow Skills Papa to manage a previously excluded skill"
    )
    include_parser.add_argument("skill")
    include_parser.add_argument("--apply", action="store_true")

    rollback_parser = subparsers.add_parser(
        "rollback", help="restore a completed deployment transaction"
    )
    rollback_parser.add_argument("transaction")
    rollback_parser.add_argument("--apply", action="store_true")

    recover_parser = subparsers.add_parser(
        "recover", help="recover an interrupted deployment transaction"
    )
    recover_parser.add_argument("transaction")
    recover_parser.add_argument("--apply", action="store_true")

    subparsers.add_parser(
        "status", aliases=["list"], help="show registered skills and deployment drift"
    )
    subparsers.add_parser(
        "doctor", help="verify objects, manifests, adapters, and deployments"
    )
    subparsers.add_parser(
        "audit", help="inspect existing target roots without changing them"
    )

    migrate_parser = subparsers.add_parser(
        "migrate",
        help="adopt existing target skills while preserving target-specific variants",
    )
    migrate_parser.add_argument("--replace", action="store_true")
    migrate_parser.add_argument("--apply", action="store_true")

    lock_parser = subparsers.add_parser(
        "import-lock",
        help="link registered skills to sources from a Vercel skills lock file",
    )
    lock_parser.add_argument("path", nargs="?", type=Path)
    lock_parser.add_argument("--scope", choices=["global", "project"], default="global")
    lock_parser.add_argument(
        "--replace-origin",
        action="store_true",
        help="replace a non-migration authoritative source",
    )
    lock_parser.add_argument(
        "--skip-invalid",
        action="store_true",
        help="apply valid lock entries even when other entries are invalid",
    )
    lock_parser.add_argument("--apply", action="store_true")

    export_parser = subparsers.add_parser(
        "export", help="export an editable source or target variant"
    )
    export_parser.add_argument("skill")
    export_parser.add_argument("destination", type=Path)
    export_parser.add_argument(
        "--target", choices=["codex", "claude", "opencode", "universal"]
    )
    export_parser.add_argument("--apply", action="store_true")
    return parser


def _changes_human(result: dict[str, Any]) -> None:
    changes = result.get("changes")
    if isinstance(changes, list):
        if not changes:
            print("No changes.")
        for change in changes:
            if not isinstance(change, dict):
                continue
            target = f"{change.get('target')}:" if change.get("target") else ""
            detail = f" — {change['detail']}" if change.get("detail") else ""
            path = f" ({change['path']})" if change.get("path") else ""
            print(
                f"- {change.get('action')}: {target}{change.get('skill')}{path}{detail}"
            )


def print_result(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return
    operation = result.get("operation", "result")
    apply = result.get("apply")
    suffix = " [APPLIED]" if apply is True else " [DRY RUN]" if apply is False else ""
    print(f"Skills Papa: {operation}{suffix}")
    if operation == "status":
        print(f"State: {result.get('state_home')}")
        print(f"Registered skills: {result.get('count', 0)}")
        for skill in result.get("skills", []):
            states = ", ".join(
                f"{target}={item.get('status')}"
                for target, item in sorted(skill.get("deployments", {}).items())
                if target in skill.get("enabled_targets", [])
                or item.get("status") != "not-deployed"
            )
            version = f" v{skill['version']}" if skill.get("version") else ""
            print(f"- {skill.get('name')}{version}: {states or 'not deployed'}")
    elif operation == "doctor":
        print("OK" if result.get("ok") else "Problems found")
        print(f"Checked skills: {result.get('checked_skills', 0)}")
    elif operation == "audit":
        print(f"Skills found: {result.get('skill_count', 0)}")
        print(f"Divergent skills: {result.get('divergent_count', 0)}")
        print(f"Shared inode groups: {result.get('shared_inode_group_count', 0)}")
        print(f"Legacy in-place backups: {result.get('legacy_backup_count', 0)}")
        print(f"Parse errors: {len(result.get('errors', []))}")
        print(f"Owner-managed exclusions: {result.get('owner_managed_count', 0)}")
    elif operation == "migrate":
        print(f"Skills ready to adopt: {len(result.get('skills', {}))}")
        print(f"Invalid occurrences skipped: {result.get('invalid_count', 0)}")
        print(
            f"Owner-managed occurrences skipped: {result.get('owner_managed_count', 0)}"
        )
        _changes_human(result)
    elif operation == "upgrade-check":
        counts = result.get("counts", {})
        print(f"Checked skills: {counts.get('checked', 0)}")
        print(f"Updates available: {counts.get('update_available', 0)}")
        print(f"Unavailable sources: {counts.get('unavailable', 0)}")
        for item in result.get("skills", []):
            version = ""
            if item.get("previous_version") != item.get("version"):
                version = (
                    f" ({item.get('previous_version') or '?'} -> "
                    f"{item.get('version') or '?'})"
                )
            detail = f" — {item['error']}" if item.get("error") else ""
            print(f"- {item.get('skill')}: {item.get('status')}{version}{detail}")
    elif operation == "import-lock":
        counts = result.get("counts", {})
        print(f"Lock: {result.get('lock_path')}")
        print(f"Skills in lock: {counts.get('lock_skills', 0)}")
        print(f"Origins to link: {counts.get('linked', 0)}")
        print(f"Not registered: {counts.get('not_registered', 0)}")
        print(f"Conflicts: {counts.get('conflicts', 0)}")
        _changes_human(result)
    else:
        _changes_human(result)
        if result.get("transaction"):
            print(f"Transaction: {result['transaction']}")
        if result.get("state_home"):
            print(f"State: {result['state_home']}")
    for warning in result.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    for error in result.get("errors", []):
        print(f"error: {error}", file=sys.stderr)


def _default_targets(state: State) -> list[str]:
    value = state.config().get("default_targets", ["codex", "claude", "opencode"])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValidationError("config.default_targets must be a string list")
    return value


def _registered_upgrade_source(entry: dict[str, Any]) -> tuple[str, str, str | None]:
    origin = entry.get("origin", {})
    if not isinstance(origin, dict):
        raise ValidationError("skill origin has invalid shape")
    origin_type = origin.get("type")
    if origin_type == "local":
        source = origin.get("path") or origin.get("source")
        fetcher = "native"
        subdir = None
    elif origin_type == "git":
        source = origin.get("source") or origin.get("url")
        fetcher = "native"
        subdir = origin.get("subdir")
    elif origin_type in {"skills-cli", "vercel-skills"}:
        source = origin.get("source")
        fetcher = "vercel"
        subdir = None
    else:
        raise ValidationError(
            f"skill origin cannot be upgraded automatically: {origin_type}; "
            "pass --source to establish an upgrade source"
        )
    if not isinstance(source, str) or not source:
        raise ValidationError(
            "skill origin has no usable source; pass --source to replace it"
        )
    return source, fetcher, subdir if isinstance(subdir, str) else None


def _upgrade_one(
    state: State,
    manager: Manager,
    name: str,
    *,
    source_override: str | None,
    subdir_override: str | None,
    fetcher_override: str | None,
    on_conflict: str,
    apply: bool,
) -> dict[str, Any]:
    entry = state.registry()["skills"].get(name)
    if entry is None:
        raise ValidationError(f"skill is not registered: {name}")
    if source_override is None:
        source, fetcher, registered_subdir = _registered_upgrade_source(entry)
        subdir = subdir_override or registered_subdir
    else:
        source = source_override
        fetcher = fetcher_override or "vercel"
        subdir = subdir_override
    if fetcher_override is not None:
        fetcher = fetcher_override
    with resolve_source(
        source,
        skill=name,
        subdir=subdir,
        fetcher=fetcher,
        vercel_package=vercel_package_from_config(state.config()),
    ) as resolved:
        pending = state.build_pending_manifest(
            resolved.path, origin=resolved.origin, expected_name=name
        )
        if apply:
            return manager.apply_upgrade(pending, on_conflict=on_conflict)
        return manager.plan_upgrade(pending, on_conflict=on_conflict)


def _check_all_upgrades(state: State, manager: Manager) -> dict[str, Any]:
    skills: list[dict[str, Any]] = []
    for name in sorted(state.registry()["skills"]):
        try:
            plan = _upgrade_one(
                state,
                manager,
                name,
                source_override=None,
                subdir_override=None,
                fetcher_override=None,
                on_conflict="error",
                apply=False,
            )
            skills.append(
                {
                    "skill": name,
                    "status": plan["status"],
                    "previous_version": plan.get("previous_version"),
                    "version": plan.get("version"),
                    "previous_manifest": plan.get("previous_manifest"),
                    "manifest": plan.get("manifest"),
                    "changes": plan.get("changes", []),
                    "warnings": plan.get("warnings", []),
                }
            )
        except (SkillsPapaError, OSError) as exc:
            skills.append(
                {
                    "skill": name,
                    "status": "unavailable",
                    "error": str(exc),
                }
            )
    return {
        "operation": "upgrade-check",
        "apply": False,
        "skills": skills,
        "counts": {
            "checked": len(skills),
            "update_available": sum(
                item["status"] == "update-available" for item in skills
            ),
            "redeploy_required": sum(
                item["status"] == "redeploy-required" for item in skills
            ),
            "current": sum(item["status"] == "current" for item in skills),
            "unavailable": sum(item["status"] == "unavailable" for item in skills),
        },
    }


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    state = State(args.home)
    manager = Manager(state)
    command = args.command
    if command == "init":
        result = manager.init(
            apply=args.apply,
            target_overrides=dict(args.target),
        )
        return result, 0
    state.require_initialized()
    if command in {"add", "import"}:
        targets = args.target or _default_targets(state)
        with resolve_source(
            args.source,
            skill=args.skill,
            subdir=args.subdir,
            fetcher=args.fetcher,
            vercel_package=vercel_package_from_config(state.config()),
        ) as resolved:
            pending = state.build_pending_manifest(
                resolved.path, origin=resolved.origin
            )
            result = manager.plan_register(pending, targets, replace=args.replace)
            result["apply"] = args.apply
            result["sync_after_register"] = bool(args.sync)
            if args.apply:
                result = manager.apply_register(pending, targets, replace=args.replace)
                result["apply"] = True
                if args.sync:
                    result["sync"] = manager.apply_sync(
                        [pending.name], targets, on_conflict=args.on_conflict
                    )
            return result, 0
    if command == "update":
        if args.apply and not args.sync:
            raise ValidationError(
                "update --no-sync cannot apply atomically; omit --no-sync or use "
                "'add --replace' for a source-only replacement"
            )
        result = _upgrade_one(
            state,
            manager,
            args.skill,
            source_override=None,
            subdir_override=None,
            fetcher_override=None,
            on_conflict=args.on_conflict,
            apply=args.apply,
        )
        result["requested_command"] = "update"
        return result, 0
    if command == "upgrade":
        if bool(args.skill) == bool(args.all_skills):
            raise ValidationError("pass exactly one SKILL or --all")
        if args.all_skills:
            if args.apply:
                raise ValidationError(
                    "bulk upgrade is check-only in this release; inspect the report "
                    "and apply individual skills"
                )
            if args.source or args.subdir or args.fetcher:
                raise ValidationError(
                    "--source, --subdir, and --fetcher require a single SKILL"
                )
            return _check_all_upgrades(state, manager), 0
        return _upgrade_one(
            state,
            manager,
            args.skill,
            source_override=args.source,
            subdir_override=args.subdir,
            fetcher_override=args.fetcher,
            on_conflict=args.on_conflict,
            apply=args.apply,
        ), 0
    if command == "sync":
        names = args.skills or None
        if args.apply:
            result = manager.apply_sync(
                names, args.target, on_conflict=args.on_conflict
            )
        else:
            result = manager.plan_sync(names, args.target, on_conflict=args.on_conflict)
            result["apply"] = False
        return result, 0
    if command == "enable":
        return manager.set_enabled(
            args.skill,
            args.target,
            True,
            apply=args.apply,
            on_conflict=args.on_conflict,
        ), 0
    if command == "disable":
        return manager.set_enabled(args.skill, args.target, False, apply=args.apply), 0
    if command == "remove":
        return manager.remove(args.skill, apply=args.apply), 0
    if command == "exclude":
        return manager.set_owner_managed(
            args.skill,
            owner=args.owner,
            reason=args.reason,
            apply=args.apply,
        ), 0
    if command == "include":
        return manager.set_owner_managed(
            args.skill,
            owner=None,
            apply=args.apply,
        ), 0
    if command == "rollback":
        return manager.rollback(args.transaction, apply=args.apply), 0
    if command == "recover":
        return manager.recover(args.transaction, apply=args.apply), 0
    if command in {"status", "list"}:
        result = manager.status()
        result["operation"] = "status"
        return result, 0
    if command == "doctor":
        result = manager.doctor()
        result["operation"] = "doctor"
        return result, 0 if result["ok"] else 1
    if command == "audit":
        result = manager.audit()
        result["operation"] = "audit"
        return result, 0
    if command == "migrate":
        return manager.migrate(apply=args.apply, replace=args.replace), 0
    if command == "import-lock":
        path = args.path or default_vercel_lock_path(args.scope)
        lock = read_vercel_lock(path, scope=args.scope)
        if args.apply and lock["invalid"] and not args.skip_invalid:
            raise ValidationError(
                "Vercel lock contains invalid entries; inspect the dry run and pass "
                "--skip-invalid to import only valid entries"
            )
        package = vercel_package_from_config(state.config())
        for origin in lock["origins"].values():
            origin["package"] = package
        if args.apply:
            result = manager.apply_import_origins(
                lock["origins"], replace_origin=args.replace_origin
            )
        else:
            result = manager.plan_import_origins(
                lock["origins"], replace_origin=args.replace_origin
            )
        result["lock_path"] = lock["path"]
        result["lock_scope"] = lock["scope"]
        result["lock_version"] = lock["version"]
        result["invalid"] = lock["invalid"]
        result["invalid_count"] = len(lock["invalid"])
        result["skip_invalid"] = bool(args.skip_invalid)
        if lock["invalid"]:
            result.setdefault("warnings", []).extend(
                f"{item['skill']}: {item['error']}" for item in lock["invalid"]
            )
        return result, 0
    if command == "export":
        if not args.apply:
            return {
                "operation": "export",
                "apply": False,
                "changes": [
                    {
                        "action": "export",
                        "skill": args.skill,
                        "path": str(args.destination.expanduser()),
                        "target": args.target,
                    }
                ],
            }, 0
        result = manager.export(
            args.skill, args.destination.expanduser().resolve(), target=args.target
        )
        result["apply"] = True
        return result, 0
    raise ValidationError(f"unsupported command: {command}")


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result, exit_code = run(args)
        print_result(result, as_json=args.json)
    except SkillsPapaError as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {"type": type(exc).__name__, "message": str(exc)},
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(f"skills-papa: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    raise SystemExit(exit_code)
