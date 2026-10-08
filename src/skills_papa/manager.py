from __future__ import annotations

import errno
import os
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .errors import ConflictError, SkillsPapaError, StateError, ValidationError
from .frontmatter import adapt_skill, parse_skill_markdown, validate_adapted_skill
from .models import (
    SUPPORTED_TARGETS,
    AdapterResult,
    Change,
    FileRecord,
    PendingManifest,
)
from .state import State, default_config
from .util import (
    atomic_write_json,
    content_id,
    ensure_exact_child,
    immutable_mode,
    materialize_shared_file,
    read_json,
    sha256_bytes,
    sha256_file,
    transaction_id,
    tree_hash,
    utc_now,
    validate_skill_name,
)


def _desired_tree_hash(result: AdapterResult) -> str:
    return content_id(
        (
            path,
            sha256_bytes(data),
            immutable_mode(result.modes.get(path, 0o644)),
        )
        for path, data in result.files.items()
    )


def _remove_exact(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _move_exact(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
        source.unlink()
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
        shutil.rmtree(source)
    else:
        shutil.copy2(source, destination, follow_symlinks=False)
        source.unlink()


def _is_legacy_backup_name(name: str) -> bool:
    return ".backup-" in name or name.endswith((".bak", ".backup", "~"))


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expanduser(str(path))))


class Manager:
    def __init__(self, state: State):
        self.state = state

    def init(
        self, *, apply: bool, target_overrides: dict[str, str] | None = None
    ) -> dict[str, Any]:
        config = default_config()
        for target, path in (target_overrides or {}).items():
            if target not in config["targets"]:
                raise ValidationError(f"unsupported target override: {target}")
            config["targets"][target]["root"] = str(Path(path).expanduser().resolve())
        result = {
            "operation": "init",
            "apply": apply,
            "state_home": str(self.state.home),
            "config": config,
        }
        if apply:
            self.state.initialize(config)
        return result

    def _owner_managed_entry(self, name: str) -> dict[str, str] | None:
        return self.state.owner_managed_skills().get(name)

    def _reject_owner_managed(self, name: str) -> None:
        entry = self._owner_managed_entry(name)
        if entry is None:
            return
        detail = f" ({entry['reason']})" if entry.get("reason") else ""
        raise ConflictError(
            f"skill {name!r} is managed by {entry['owner']}, not Skills Papa{detail}; "
            f"run 'skills-papa include {name} --apply' only when ownership is intentionally transferred"
        )

    def set_owner_managed(
        self,
        name: str,
        *,
        owner: str | None,
        reason: str = "",
        apply: bool,
    ) -> dict[str, Any]:
        name = validate_skill_name(name)
        if owner is not None:
            owner = owner.strip()
            reason = reason.strip()
            if not owner or len(owner) > 128 or any(c in owner for c in "\x00\r\n"):
                raise ValidationError("owner must be a non-empty single-line string")
            if len(reason) > 1024 or any(c in reason for c in "\x00\r\n"):
                raise ValidationError(
                    "reason must be a single line of at most 1024 characters"
                )
        registry = self.state.registry()
        if owner is not None and name in registry["skills"]:
            raise ConflictError(
                f"skill {name!r} is still registered; remove it before assigning external ownership"
            )
        current = self.state.owner_managed_skills().get(name)
        desired = {"owner": owner, "reason": reason} if owner is not None else None
        changed = current != desired
        action = (
            "exclude-owner-managed" if owner is not None else "include-for-management"
        )
        result = {
            "operation": "exclude" if owner is not None else "include",
            "skill": name,
            "owner": owner,
            "reason": reason if owner is not None else None,
            "apply": apply,
            "changes": [Change(action, None, name, detail=owner).as_dict()]
            if changed
            else [],
        }
        if not apply or not changed:
            return result
        with self.state.exclusive_lock():
            registry = self.state.registry()
            if owner is not None and name in registry["skills"]:
                raise ConflictError(
                    f"skill {name!r} became registered; remove it before assigning external ownership"
                )
            config = read_json(self.state.config_path)
            raw = config.setdefault("owner_managed_skills", {})
            if not isinstance(raw, dict):
                raise StateError("config.owner_managed_skills must be a mapping")
            if owner is None:
                raw.pop(name, None)
            else:
                raw[name] = {"owner": owner, "reason": reason}
            atomic_write_json(self.state.config_path, config)
        return result

    def plan_register(
        self,
        pending: PendingManifest,
        targets: Iterable[str],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._reject_owner_managed(pending.name)
        registry = self.state.registry()
        selected = self._validate_targets(targets)
        exists = pending.name in registry["skills"]
        if exists and not replace:
            raise ConflictError(
                f"skill {pending.name!r} is already registered; pass --replace to update it"
            )
        return {
            "operation": "register",
            "skill": pending.name,
            "manifest": pending.content_id,
            "version": pending.version,
            "targets": selected,
            "replace": exists,
            "origin": pending.origin,
            "warnings": pending.warnings,
            "changes": [
                Change(
                    "update-source" if exists else "add-source",
                    None,
                    pending.name,
                    detail=f"manifest {pending.content_id[:12]}",
                ).as_dict()
            ],
        }

    def apply_register(
        self,
        pending: PendingManifest,
        targets: Iterable[str],
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        self._reject_owner_managed(pending.name)
        selected = self._validate_targets(targets)
        with self.state.exclusive_lock():
            self._reject_owner_managed(pending.name)
            registry = self.state.registry()
            previous = registry["skills"].get(pending.name)
            if previous is not None and not replace:
                raise ConflictError(
                    f"skill {pending.name!r} is already registered; pass --replace to update it"
                )
            self.state.persist_manifest(pending)
            now = utc_now()
            registry["skills"][pending.name] = {
                "manifest": pending.content_id,
                "target_manifests": {},
                "origin": pending.origin,
                "version": pending.version,
                "enabled_targets": selected,
                "created_at": previous.get("created_at", now) if previous else now,
                "updated_at": now,
            }
            self.state.save_registry(registry)
        return {
            "operation": "register",
            "skill": pending.name,
            "manifest": pending.content_id,
            "version": pending.version,
            "targets": selected,
            "replace": previous is not None,
            "origin": pending.origin,
            "warnings": pending.warnings,
            "changes": [
                Change(
                    "update-source" if previous is not None else "add-source",
                    None,
                    pending.name,
                    detail=f"manifest {pending.content_id[:12]}",
                ).as_dict()
            ],
        }

    def _validate_targets(self, targets: Iterable[str]) -> list[str]:
        available = self.state.target_specs()
        selected: list[str] = []
        for target in targets:
            if target not in SUPPORTED_TARGETS or target not in available:
                raise ValidationError(f"unknown target: {target}")
            if target not in selected:
                selected.append(target)
        if not selected:
            raise ValidationError("at least one target is required")
        return selected

    def _entry_manifest(self, entry: dict[str, Any], target: str) -> str:
        variants = entry.get("target_manifests", {})
        if isinstance(variants, dict) and isinstance(variants.get(target), str):
            return variants[target]
        manifest_id = entry.get("manifest")
        if not isinstance(manifest_id, str):
            raise StateError("skill entry has no manifest")
        return manifest_id

    def _adapt(self, name: str, entry: dict[str, Any], target: str) -> AdapterResult:
        manifest_id = self._entry_manifest(entry, target)
        files, modes = self.state.manifest_files(name, manifest_id)
        result = adapt_skill(files, modes, target)
        validate_adapted_skill(result, target)
        return result

    def _adapt_pending(self, pending: PendingManifest, target: str) -> AdapterResult:
        files: dict[str, bytes] = {}
        modes: dict[str, int] = {}
        for record in pending.files:
            source = pending.source_paths[record.path]
            data = source.read_bytes()
            if len(data) != record.size or sha256_bytes(data) != record.sha256:
                raise StateError(f"upgrade source changed while planning: {source}")
            files[record.path] = data
            modes[record.path] = record.mode
        result = adapt_skill(files, modes, target)
        validate_adapted_skill(result, target)
        return result

    def _entry_version(self, name: str, entry: dict[str, Any]) -> str | None:
        value = entry.get("version")
        if value is not None and str(value).strip():
            return str(value).strip()
        manifest_id = entry.get("manifest")
        if not isinstance(manifest_id, str):
            return None
        files, _ = self.state.manifest_files(name, manifest_id)
        skill_md = files.get("SKILL.md")
        if skill_md is None:
            return None
        frontmatter, _ = parse_skill_markdown(skill_md)
        metadata = frontmatter.get("metadata")
        raw = metadata.get("version") if isinstance(metadata, dict) else None
        if raw is None:
            raw = frontmatter.get("version")
        return str(raw).strip() if raw is not None and str(raw).strip() else None

    def _plan_upgrade_with_registry(
        self,
        pending: PendingManifest,
        registry: dict[str, Any],
        *,
        on_conflict: str,
    ) -> tuple[dict[str, Any], dict[tuple[str, str], AdapterResult]]:
        if on_conflict not in {"error", "backup"}:
            raise ValidationError("--on-conflict must be 'error' or 'backup'")
        entry = registry["skills"].get(pending.name)
        if entry is None:
            raise ValidationError(f"skill is not registered: {pending.name}")
        previous_manifest = entry.get("manifest")
        if not isinstance(previous_manifest, str):
            raise StateError(f"skill entry has no manifest: {pending.name}")
        source_changed = previous_manifest != pending.content_id
        origin_changed = entry.get("origin") != pending.origin
        canonical_changed = source_changed or origin_changed
        specs = self.state.target_specs()
        enabled_targets = sorted(set(entry.get("enabled_targets", [])))
        retired_variants = (
            sorted(entry.get("target_manifests", {}).keys())
            if canonical_changed and isinstance(entry.get("target_manifests"), dict)
            else []
        )
        changes: list[dict[str, Any]] = []
        warnings: list[str] = list(pending.warnings)
        adapted: dict[tuple[str, str], AdapterResult] = {}
        if source_changed:
            changes.append(
                Change(
                    "upgrade-source",
                    None,
                    pending.name,
                    detail=(
                        f"manifest {previous_manifest[:12]} -> "
                        f"{pending.content_id[:12]}"
                    ),
                ).as_dict()
            )
        elif origin_changed:
            changes.append(
                Change(
                    "upgrade-origin",
                    None,
                    pending.name,
                    detail="accept replacement source provenance",
                ).as_dict()
            )
        for target in enabled_targets:
            if target not in specs:
                raise StateError(f"configured target is missing: {target}")
            result = (
                self._adapt_pending(pending, target)
                if canonical_changed
                else self._adapt(pending.name, entry, target)
            )
            adapted[(target, pending.name)] = result
            warnings.extend(
                f"{target}:{pending.name}: {item}" for item in result.warnings
            )
            desired = _desired_tree_hash(result)
            destination = ensure_exact_child(specs[target].root, pending.name)
            deployment = registry["deployments"].get(target, {}).get(pending.name)
            if destination.exists() or destination.is_symlink():
                current = tree_hash(destination) if destination.is_dir() else None
                if current == desired:
                    if deployment is None:
                        changes.append(
                            Change(
                                "adopt-matching",
                                target,
                                pending.name,
                                str(destination),
                                f"tree {desired[:12]}",
                            ).as_dict()
                        )
                    continue
                if deployment is None and on_conflict == "error":
                    raise ConflictError(
                        f"unmanaged destination differs: {destination}; "
                        "use --on-conflict backup"
                    )
                action = "replace-unmanaged" if deployment is None else "upgrade"
            else:
                action = "install"
            changes.append(
                Change(
                    action,
                    target,
                    pending.name,
                    str(destination),
                    f"tree {desired[:12]}",
                ).as_dict()
            )
        if canonical_changed:
            status = "update-available"
        elif changes:
            status = "redeploy-required"
        else:
            status = "current"
            changes.append(Change("current", None, pending.name).as_dict())
        return (
            {
                "operation": "upgrade",
                "skill": pending.name,
                "status": status,
                "previous_manifest": previous_manifest,
                "manifest": pending.content_id,
                "origin_changed": origin_changed,
                "previous_version": self._entry_version(pending.name, entry),
                "version": pending.version,
                "origin": pending.origin,
                "enabled_targets": enabled_targets,
                "retired_target_variants": retired_variants,
                "changes": changes,
                "warnings": sorted(set(warnings)),
                "on_conflict": on_conflict,
            },
            adapted,
        )

    def plan_upgrade(
        self, pending: PendingManifest, *, on_conflict: str = "error"
    ) -> dict[str, Any]:
        result, _ = self._plan_upgrade_with_registry(
            pending, self.state.registry(), on_conflict=on_conflict
        )
        result["apply"] = False
        return result

    def apply_upgrade(
        self, pending: PendingManifest, *, on_conflict: str = "error"
    ) -> dict[str, Any]:
        with self.state.exclusive_lock():
            registry_before = self.state.registry()
            plan, adapted = self._plan_upgrade_with_registry(
                pending, registry_before, on_conflict=on_conflict
            )
            source_changed = plan["previous_manifest"] != pending.content_id
            canonical_changed = source_changed or bool(plan["origin_changed"])
            deployment_changes = [
                item for item in plan["changes"] if item.get("target") is not None
            ]
            if not canonical_changed and not deployment_changes:
                plan["apply"] = True
                plan["transaction"] = None
                return plan
            if source_changed:
                self.state.persist_manifest(pending)
            registry_after = read_json(self.state.registry_path)
            entry_after = registry_after["skills"][pending.name]
            if canonical_changed:
                entry_after["manifest"] = pending.content_id
                entry_after["target_manifests"] = {}
                entry_after["origin"] = pending.origin
                entry_after["version"] = pending.version
                entry_after["updated_at"] = utc_now()
            operations: list[dict[str, Any]] = []
            for target in plan["enabled_targets"]:
                result = adapted[(target, pending.name)]
                desired = _desired_tree_hash(result)
                destination = ensure_exact_child(
                    self.state.target_specs()[target].root, pending.name
                )
                current = (
                    tree_hash(destination)
                    if destination.is_dir() and not destination.is_symlink()
                    else None
                )
                if current != desired:
                    operations.append(
                        {
                            "kind": "install",
                            "target": target,
                            "skill": pending.name,
                            "destination": str(destination),
                            "desired_tree_hash": desired,
                        }
                    )
                registry_after["deployments"].setdefault(target, {})[pending.name] = {
                    "path": str(destination),
                    "tree_hash": desired,
                    "manifest": pending.content_id
                    if canonical_changed
                    else self._entry_manifest(entry_after, target),
                    "deployed_at": utc_now(),
                    "adopted": False,
                }
            if operations or canonical_changed or deployment_changes:
                plan["transaction"] = self._apply_operations(
                    "upgrade", registry_before, registry_after, operations, adapted
                )
            else:
                self.state.save_registry(registry_after)
                plan["transaction"] = None
            plan["status"] = "upgraded" if canonical_changed else "redeployed"
            plan["apply"] = True
            return plan

    def _plan_origin_import(
        self,
        registry: dict[str, Any],
        origins: dict[str, dict[str, Any]],
        *,
        replace_origin: bool,
    ) -> dict[str, Any]:
        changes: list[dict[str, Any]] = []
        current: list[str] = []
        missing: list[str] = []
        conflicts: list[dict[str, str]] = []
        accepted: dict[str, dict[str, Any]] = {}
        replaceable_types = {"migration", "skills-cli", "vercel-skills"}
        for name, origin in sorted(origins.items()):
            entry = registry["skills"].get(name)
            if entry is None:
                missing.append(name)
                continue
            if not isinstance(origin, dict) or origin.get("type") != "vercel-skills":
                raise ValidationError(f"invalid Vercel origin for skill: {name}")
            if not isinstance(origin.get("source"), str):
                raise ValidationError(f"Vercel origin has no source for skill: {name}")
            previous = entry.get("origin", {})
            if previous == origin:
                current.append(name)
                continue
            previous_type = (
                str(previous.get("type")) if isinstance(previous, dict) else "invalid"
            )
            if previous_type not in replaceable_types and not replace_origin:
                conflicts.append({"skill": name, "origin_type": previous_type})
                continue
            accepted[name] = origin
            changes.append(
                Change(
                    "replace-origin" if previous_type != "migration" else "link-origin",
                    None,
                    name,
                    detail=str(origin["source"]),
                ).as_dict()
            )
        return {
            "operation": "import-lock",
            "replace_origin": replace_origin,
            "accepted": accepted,
            "changes": changes,
            "current": current,
            "missing": missing,
            "conflicts": conflicts,
            "counts": {
                "lock_skills": len(origins),
                "linked": len(accepted),
                "current": len(current),
                "not_registered": len(missing),
                "conflicts": len(conflicts),
            },
        }

    def plan_import_origins(
        self,
        origins: dict[str, dict[str, Any]],
        *,
        replace_origin: bool = False,
    ) -> dict[str, Any]:
        result = self._plan_origin_import(
            self.state.registry(), origins, replace_origin=replace_origin
        )
        result["apply"] = False
        return result

    def apply_import_origins(
        self,
        origins: dict[str, dict[str, Any]],
        *,
        replace_origin: bool = False,
    ) -> dict[str, Any]:
        with self.state.exclusive_lock():
            registry_before = self.state.registry()
            result = self._plan_origin_import(
                registry_before, origins, replace_origin=replace_origin
            )
            if result["conflicts"]:
                names = ", ".join(item["skill"] for item in result["conflicts"])
                raise ConflictError(
                    f"lock import would replace authoritative origins for: {names}; "
                    "review and pass --replace-origin"
                )
            if not result["accepted"]:
                result["apply"] = True
                result["transaction"] = None
                return result
            registry_after = read_json(self.state.registry_path)
            now = utc_now()
            for name, origin in result["accepted"].items():
                registry_after["skills"][name]["origin"] = dict(origin)
                registry_after["skills"][name]["updated_at"] = now
            result["transaction"] = self._apply_operations(
                "import-lock", registry_before, registry_after, [], {}
            )
            result["apply"] = True
            return result

    def plan_sync(
        self,
        names: Iterable[str] | None = None,
        targets: Iterable[str] | None = None,
        *,
        on_conflict: str = "error",
    ) -> dict[str, Any]:
        if on_conflict not in {"error", "backup"}:
            raise ValidationError("--on-conflict must be 'error' or 'backup'")
        registry = self.state.registry()
        specs = self.state.target_specs()
        selected_names = sorted(set(names or registry["skills"].keys()))
        selected_targets = set(targets or specs.keys())
        changes: list[dict[str, Any]] = []
        warnings: list[str] = []
        for name in selected_names:
            validate_skill_name(name)
            entry = registry["skills"].get(name)
            if entry is None:
                raise ValidationError(f"skill is not registered: {name}")
            enabled = set(entry.get("enabled_targets", []))
            for target in sorted(enabled & selected_targets):
                if target not in specs:
                    raise StateError(f"configured target is missing: {target}")
                spec = specs[target]
                if name in spec.protected_names or name.startswith("."):
                    raise ConflictError(
                        f"refusing to manage protected skill {target}:{name}"
                    )
                destination = ensure_exact_child(spec.root, name)
                result = self._adapt(name, entry, target)
                warnings.extend(f"{target}:{name}: {item}" for item in result.warnings)
                desired = _desired_tree_hash(result)
                deployment = registry["deployments"].get(target, {}).get(name)
                if destination.exists() or destination.is_symlink():
                    current = tree_hash(destination) if destination.is_dir() else None
                    if current == desired:
                        if deployment is None:
                            changes.append(
                                Change(
                                    "adopt-identical", target, name, str(destination)
                                ).as_dict()
                            )
                        continue
                    if deployment is None and on_conflict == "error":
                        raise ConflictError(
                            f"unmanaged destination differs: {destination}; use --on-conflict backup"
                        )
                    action = "replace-unmanaged" if deployment is None else "update"
                else:
                    action = "install"
                changes.append(
                    Change(
                        action, target, name, str(destination), f"tree {desired[:12]}"
                    ).as_dict()
                )
        return {
            "operation": "sync",
            "changes": changes,
            "warnings": sorted(set(warnings)),
            "on_conflict": on_conflict,
        }

    def apply_sync(
        self,
        names: Iterable[str] | None = None,
        targets: Iterable[str] | None = None,
        *,
        on_conflict: str = "error",
    ) -> dict[str, Any]:
        with self.state.exclusive_lock():
            plan = self.plan_sync(names, targets, on_conflict=on_conflict)
            registry_before = self.state.registry()
            registry_after = read_json(self.state.registry_path)
            operations: list[dict[str, Any]] = []
            adapted: dict[tuple[str, str], AdapterResult] = {}
            for change in plan["changes"]:
                name = change["skill"]
                target = change["target"]
                entry = registry_after["skills"][name]
                result = self._adapt(name, entry, target)
                desired = _desired_tree_hash(result)
                if change["action"] == "adopt-identical":
                    registry_after["deployments"].setdefault(target, {})[name] = {
                        "path": change["path"],
                        "tree_hash": desired,
                        "manifest": self._entry_manifest(entry, target),
                        "deployed_at": utc_now(),
                        "adopted": True,
                    }
                    continue
                adapted[(target, name)] = result
                operations.append(
                    {
                        "kind": "install",
                        "target": target,
                        "skill": name,
                        "destination": change["path"],
                        "desired_tree_hash": desired,
                    }
                )
                registry_after["deployments"].setdefault(target, {})[name] = {
                    "path": change["path"],
                    "tree_hash": desired,
                    "manifest": self._entry_manifest(entry, target),
                    "deployed_at": utc_now(),
                    "adopted": False,
                }
            if operations:
                transaction = self._apply_operations(
                    "sync", registry_before, registry_after, operations, adapted
                )
                plan["transaction"] = transaction
            else:
                self.state.save_registry(registry_after)
                plan["transaction"] = None
            plan["apply"] = True
            return plan

    def _write_generated(self, path: Path, data: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(immutable_mode(mode))

    def _stage_skill(
        self, target: str, name: str, result: AdapterResult
    ) -> tuple[Path, dict[str, int]]:
        spec = self.state.target_specs()[target]
        spec.root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=f".skills-papa-{name}-", dir=spec.root))
        strategies: dict[str, int] = defaultdict(int)
        try:
            for relative, data in sorted(result.files.items()):
                destination = stage / relative
                mode = result.modes.get(relative, 0o644)
                file_hash = sha256_bytes(data)
                object_path = self.state.object_path(file_hash, mode)
                if (
                    relative != "SKILL.md"
                    and object_path.is_file()
                    and sha256_file(object_path) == file_hash
                ):
                    strategy = materialize_shared_file(object_path, destination, mode)
                    strategies[strategy] += 1
                else:
                    self._write_generated(destination, data, mode)
                    strategies["generated-copy"] += 1
            return stage, dict(strategies)
        except Exception:
            _remove_exact(stage)
            raise

    def _apply_operations(
        self,
        action: str,
        registry_before: dict[str, Any],
        registry_after: dict[str, Any],
        operations: list[dict[str, Any]],
        adapted: dict[tuple[str, str], AdapterResult],
    ) -> str:
        txid = transaction_id(action)
        backup_root = self.state.backups_dir / txid
        journal_path = self.state.transactions_dir / f"{txid}.json"
        journal: dict[str, Any] = {
            "schema_version": 1,
            "id": txid,
            "action": action,
            "status": "applying",
            "created_at": utc_now(),
            "registry_before": registry_before,
            "operations": [],
        }
        staged: dict[tuple[str, str], Path] = {}
        try:
            for operation in operations:
                key = (operation["target"], operation["skill"])
                if operation["kind"] == "install":
                    stage, strategies = self._stage_skill(*key, adapted[key])
                    staged[key] = stage
                    operation["stage"] = str(stage)
                    operation["materialization"] = strategies
                destination = Path(operation["destination"])
                operation["backup"] = str(
                    backup_root / operation["target"] / operation["skill"]
                )
                operation["had_previous"] = bool(
                    destination.exists() or destination.is_symlink()
                )
                journal["operations"].append(operation)
            atomic_write_json(journal_path, journal)
        except Exception:
            for stage in staged.values():
                if stage.exists() or stage.is_symlink():
                    _remove_exact(stage)
            raise
        applied: list[dict[str, Any]] = []
        try:
            for operation in journal["operations"]:
                target = operation["target"]
                name = operation["skill"]
                spec = self.state.target_specs()[target]
                destination = ensure_exact_child(spec.root, name)
                if destination != _lexical_absolute(Path(operation["destination"])):
                    raise StateError(
                        f"journal destination does not match configured target: {destination}"
                    )
                backup = Path(operation["backup"])
                if destination.exists() or destination.is_symlink():
                    _move_exact(destination, backup)
                if operation["kind"] == "install":
                    stage = staged[(target, name)]
                    os.replace(stage, destination)
                elif operation["kind"] != "remove":
                    raise StateError(
                        f"unknown transaction operation: {operation['kind']}"
                    )
                applied.append(operation)
                operation["applied"] = True
                atomic_write_json(journal_path, journal)
            self.state.save_registry(registry_after)
            journal["registry_after_revision"] = registry_after.get("revision")
            journal["status"] = "completed"
            journal["completed_at"] = utc_now()
            atomic_write_json(journal_path, journal)
            return txid
        except Exception as exc:
            for operation in reversed(applied):
                destination = Path(operation["destination"])
                backup = Path(operation["backup"])
                if destination.exists() or destination.is_symlink():
                    _remove_exact(destination)
                if backup.exists() or backup.is_symlink():
                    _move_exact(backup, destination)
            for stage in staged.values():
                if stage.exists() or stage.is_symlink():
                    _remove_exact(stage)
            self.state.save_registry(registry_before)
            journal["status"] = "rolled-back-after-error"
            journal["error"] = str(exc)
            journal["completed_at"] = utc_now()
            atomic_write_json(journal_path, journal)
            raise

    def set_enabled(
        self,
        name: str,
        target: str,
        enabled: bool,
        *,
        apply: bool,
        on_conflict: str = "error",
    ) -> dict[str, Any]:
        if on_conflict not in {"error", "backup"}:
            raise ValidationError("--on-conflict must be 'error' or 'backup'")
        registry = self.state.registry()
        specs = self.state.target_specs()
        if name not in registry["skills"]:
            raise ValidationError(f"skill is not registered: {name}")
        if target not in specs:
            raise ValidationError(f"unknown target: {target}")
        entry = registry["skills"][name]
        current = set(entry.get("enabled_targets", []))
        destination = ensure_exact_child(specs[target].root, name)
        action = "enable" if enabled else "disable"
        changes: list[dict[str, Any]] = []
        if enabled and target not in current:
            changes.append(Change("enable", target, name, str(destination)).as_dict())
        elif not enabled and target in current:
            changes.append(Change("disable", target, name, str(destination)).as_dict())
        result = {"operation": action, "changes": changes, "apply": apply}
        if not apply or not changes:
            return result
        with self.state.exclusive_lock():
            registry_before = self.state.registry()
            registry_after = read_json(self.state.registry_path)
            entry_after = registry_after["skills"][name]
            enabled_targets = set(entry_after.get("enabled_targets", []))
            if enabled:
                enabled_targets.add(target)
                entry_after["enabled_targets"] = sorted(enabled_targets)
                adapted = self._adapt(name, entry_after, target)
                desired = _desired_tree_hash(adapted)
                deployment = registry_after["deployments"].get(target, {}).get(name)
                if destination.exists() or destination.is_symlink():
                    current_tree = (
                        tree_hash(destination) if destination.is_dir() else None
                    )
                    if current_tree == desired:
                        registry_after["deployments"].setdefault(target, {})[name] = {
                            "path": str(destination),
                            "tree_hash": desired,
                            "manifest": self._entry_manifest(entry_after, target),
                            "deployed_at": utc_now(),
                            "adopted": deployment is None,
                        }
                        self.state.save_registry(registry_after)
                        result["transaction"] = None
                        return result
                    if deployment is None and on_conflict == "error":
                        raise ConflictError(
                            f"unmanaged destination differs: {destination}; "
                            "use --on-conflict backup"
                        )
                operation = {
                    "kind": "install",
                    "target": target,
                    "skill": name,
                    "destination": str(destination),
                    "desired_tree_hash": desired,
                }
                registry_after["deployments"].setdefault(target, {})[name] = {
                    "path": str(destination),
                    "tree_hash": desired,
                    "manifest": self._entry_manifest(entry_after, target),
                    "deployed_at": utc_now(),
                    "adopted": False,
                }
                result["transaction"] = self._apply_operations(
                    "enable",
                    registry_before,
                    registry_after,
                    [operation],
                    {(target, name): adapted},
                )
                return result
            enabled_targets.discard(target)
            entry_after["enabled_targets"] = sorted(enabled_targets)
            deployment = registry_after["deployments"].get(target, {}).pop(name, None)
            operations: list[dict[str, Any]] = []
            if deployment and (destination.exists() or destination.is_symlink()):
                operations.append(
                    {
                        "kind": "remove",
                        "target": target,
                        "skill": name,
                        "destination": str(destination),
                    }
                )
            if operations:
                result["transaction"] = self._apply_operations(
                    "disable", registry_before, registry_after, operations, {}
                )
            else:
                self.state.save_registry(registry_after)
                result["transaction"] = None
            return result

    def remove(self, name: str, *, apply: bool) -> dict[str, Any]:
        registry = self.state.registry()
        entry = registry["skills"].get(name)
        if entry is None:
            raise ValidationError(f"skill is not registered: {name}")
        specs = self.state.target_specs()
        operations: list[dict[str, Any]] = []
        changes: list[dict[str, Any]] = []
        for target, deployments in registry["deployments"].items():
            if name not in deployments or target not in specs:
                continue
            destination = ensure_exact_child(specs[target].root, name)
            changes.append(
                Change("remove-deployment", target, name, str(destination)).as_dict()
            )
            if destination.exists() or destination.is_symlink():
                operations.append(
                    {
                        "kind": "remove",
                        "target": target,
                        "skill": name,
                        "destination": str(destination),
                    }
                )
        changes.append(Change("unregister", None, name).as_dict())
        result = {"operation": "remove", "changes": changes, "apply": apply}
        if not apply:
            return result
        with self.state.exclusive_lock():
            registry_before = self.state.registry()
            registry_after = read_json(self.state.registry_path)
            registry_after["skills"].pop(name, None)
            for deployments in registry_after["deployments"].values():
                if isinstance(deployments, dict):
                    deployments.pop(name, None)
            if operations:
                result["transaction"] = self._apply_operations(
                    "remove", registry_before, registry_after, operations, {}
                )
            else:
                self.state.save_registry(registry_after)
                result["transaction"] = None
            return result

    def rollback(self, txid: str, *, apply: bool) -> dict[str, Any]:
        if "/" in txid or ".." in txid:
            raise ValidationError("invalid transaction id")
        journal_path = self.state.transactions_dir / f"{txid}.json"
        journal = read_json(journal_path)
        if not isinstance(journal, dict) or journal.get("id") != txid:
            raise ValidationError(f"transaction not found: {txid}")
        if journal.get("status") != "completed":
            raise ConflictError(
                f"transaction is not rollback-ready: {journal.get('status')}"
            )
        changes = [
            Change(
                "restore", item["target"], item["skill"], item["destination"]
            ).as_dict()
            for item in reversed(journal.get("operations", []))
        ]
        if not changes:
            changes.append(
                {
                    "action": "restore-registry",
                    "skill": "*",
                    "detail": f"registry snapshot from {txid}",
                }
            )
        result = {
            "operation": "rollback",
            "transaction": txid,
            "changes": changes,
            "apply": apply,
        }
        if not apply:
            return result
        with self.state.exclusive_lock():
            current_registry = self.state.registry()
            if current_registry.get("revision") != journal.get(
                "registry_after_revision"
            ):
                raise ConflictError(
                    "cannot roll back an out-of-order transaction; newer registry changes exist"
                )
            rollback_id = transaction_id("rollback")
            rollback_backup = self.state.backups_dir / rollback_id
            specs = self.state.target_specs()
            checked: list[tuple[dict[str, Any], Path, Path]] = []
            for item in reversed(journal.get("operations", [])):
                target = item["target"]
                name = item["skill"]
                if target not in specs:
                    raise StateError(f"transaction references unknown target: {target}")
                destination = ensure_exact_child(specs[target].root, name)
                if destination != _lexical_absolute(Path(item["destination"])):
                    raise StateError(
                        f"transaction target moved since creation: {target}:{name}"
                    )
                backup = _lexical_absolute(Path(item["backup"]))
                expected_backup = self.state.backups_dir / txid / target / name
                if backup != expected_backup:
                    raise StateError(f"unsafe rollback backup path: {backup}")
                if item.get("had_previous") and not (
                    backup.exists() or backup.is_symlink()
                ):
                    raise StateError(f"rollback backup is missing: {backup}")
                if destination.exists() or destination.is_symlink():
                    if item.get("kind") != "install" or not destination.is_dir():
                        raise ConflictError(
                            f"rollback destination changed outside Skills Papa: {destination}"
                        )
                    expected_tree = item.get("desired_tree_hash")
                    if (
                        not isinstance(expected_tree, str)
                        or tree_hash(destination) != expected_tree
                    ):
                        raise ConflictError(
                            f"rollback destination has drifted: {destination}"
                        )
                checked.append((item, destination, backup))
            for item, destination, backup in checked:
                target = item["target"]
                name = item["skill"]
                if destination.exists() or destination.is_symlink():
                    _move_exact(destination, rollback_backup / target / name)
                if item.get("had_previous"):
                    _move_exact(backup, destination)
            registry_before = journal.get("registry_before")
            if not isinstance(registry_before, dict):
                raise StateError("transaction has no registry snapshot")
            self.state.save_registry(registry_before)
            journal["status"] = "rolled-back"
            journal["rolled_back_at"] = utc_now()
            journal["rollback_id"] = rollback_id
            atomic_write_json(journal_path, journal)
            atomic_write_json(
                self.state.transactions_dir / f"{rollback_id}.json",
                {
                    "schema_version": 1,
                    "id": rollback_id,
                    "action": "rollback",
                    "status": "completed",
                    "created_at": utc_now(),
                    "source_transaction": txid,
                    "backup_root": str(rollback_backup),
                },
            )
            result["rollback_id"] = rollback_id
            return result

    def recover(self, txid: str, *, apply: bool) -> dict[str, Any]:
        if "/" in txid or ".." in txid:
            raise ValidationError("invalid transaction id")
        journal_path = self.state.transactions_dir / f"{txid}.json"
        journal = read_json(journal_path)
        if not isinstance(journal, dict) or journal.get("id") != txid:
            raise ValidationError(f"transaction not found: {txid}")
        if journal.get("status") != "applying":
            raise ConflictError(
                f"transaction does not require recovery: {journal.get('status')}"
            )
        changes = [
            Change(
                "recover",
                item.get("target"),
                item.get("skill"),
                item.get("destination"),
            ).as_dict()
            for item in reversed(journal.get("operations", []))
            if item.get("applied") or item.get("stage")
        ]
        result = {
            "operation": "recover",
            "transaction": txid,
            "changes": changes,
            "apply": apply,
        }
        if not apply:
            return result
        with self.state.exclusive_lock(allow_unfinished=True):
            specs = self.state.target_specs()
            checked: list[tuple[dict[str, Any], Path, Path, Path | None]] = []
            for item in reversed(journal.get("operations", [])):
                target = item["target"]
                name = item["skill"]
                if target not in specs:
                    raise StateError(f"transaction references unknown target: {target}")
                destination = ensure_exact_child(specs[target].root, name)
                if destination != _lexical_absolute(Path(item["destination"])):
                    raise StateError(
                        f"transaction target moved since creation: {target}:{name}"
                    )
                backup = _lexical_absolute(Path(item["backup"]))
                expected_backup = self.state.backups_dir / txid / target / name
                if backup != expected_backup:
                    raise StateError(f"unsafe recovery backup path: {backup}")
                stage_value = item.get("stage")
                stage: Path | None = None
                if stage_value:
                    stage = _lexical_absolute(Path(stage_value))
                    if stage.parent != specs[target].root.expanduser().resolve():
                        raise StateError(f"unsafe staged path in journal: {stage}")
                backup_exists = backup.exists() or backup.is_symlink()
                destination_exists = destination.exists() or destination.is_symlink()
                mutation_started = backup_exists or not item.get("had_previous")
                if (
                    item.get("applied")
                    and item.get("had_previous")
                    and not backup_exists
                ):
                    raise StateError(f"recovery backup is missing: {backup}")
                if mutation_started and destination_exists:
                    if item.get("kind") != "install" or not destination.is_dir():
                        raise ConflictError(
                            f"recovery destination changed outside Skills Papa: {destination}"
                        )
                    expected_tree = item.get("desired_tree_hash")
                    if (
                        not isinstance(expected_tree, str)
                        or tree_hash(destination) != expected_tree
                    ):
                        raise ConflictError(
                            f"recovery destination has drifted: {destination}"
                        )
                checked.append((item, destination, backup, stage))
            for item, destination, backup, stage in checked:
                if stage is not None and (stage.exists() or stage.is_symlink()):
                    _remove_exact(stage)
                backup_exists = backup.exists() or backup.is_symlink()
                mutation_started = backup_exists or not item.get("had_previous")
                if not mutation_started:
                    continue
                if destination.exists() or destination.is_symlink():
                    _remove_exact(destination)
                if backup_exists:
                    _move_exact(backup, destination)
            registry_before = journal.get("registry_before")
            if not isinstance(registry_before, dict):
                raise StateError("transaction has no registry snapshot")
            self.state.save_registry(registry_before)
            journal["status"] = "recovered"
            journal["recovered_at"] = utc_now()
            atomic_write_json(journal_path, journal)
            return result

    def status(self) -> dict[str, Any]:
        registry = self.state.registry()
        specs = self.state.target_specs()
        skills: list[dict[str, Any]] = []
        for name, entry in sorted(registry["skills"].items()):
            deployments: dict[str, Any] = {}
            for target in sorted(set(entry.get("enabled_targets", [])) | set(specs)):
                deployment = registry["deployments"].get(target, {}).get(name)
                if deployment is None:
                    deployments[target] = {"status": "not-deployed"}
                    continue
                destination = Path(deployment["path"])
                if not destination.is_dir():
                    deployments[target] = {
                        "status": "missing",
                        "path": str(destination),
                    }
                    continue
                current = tree_hash(destination)
                deployments[target] = {
                    "status": "ok"
                    if current == deployment.get("tree_hash")
                    else "drifted",
                    "path": str(destination),
                    "expected": deployment.get("tree_hash"),
                    "actual": current,
                }
            skills.append(
                {
                    "name": name,
                    "manifest": entry.get("manifest"),
                    "version": self._entry_version(name, entry),
                    "enabled_targets": entry.get("enabled_targets", []),
                    "origin": entry.get("origin", {}),
                    "deployments": deployments,
                }
            )
        return {
            "state_home": str(self.state.home),
            "skills": skills,
            "count": len(skills),
            "owner_managed_skills": self.state.owner_managed_skills(),
        }

    def doctor(self) -> dict[str, Any]:
        registry = self.state.registry()
        errors: list[str] = []
        warnings: list[str] = []
        specs = self.state.target_specs()
        for name, entry in sorted(registry["skills"].items()):
            manifest_ids = {entry.get("manifest")}
            variants = entry.get("target_manifests", {})
            if isinstance(variants, dict):
                manifest_ids.update(variants.values())
            for manifest_id in manifest_ids:
                if not isinstance(manifest_id, str):
                    errors.append(f"{name}: invalid manifest reference")
                    continue
                try:
                    manifest = self.state.load_manifest(name, manifest_id)
                    for raw in manifest["files"]:
                        record = FileRecord.from_dict(raw)
                        obj = self.state.object_path(record.sha256, record.mode)
                        if not obj.is_file():
                            errors.append(f"{name}: missing object {record.sha256}")
                        elif sha256_file(obj) != record.sha256:
                            errors.append(f"{name}: corrupt object {record.sha256}")
                except SkillsPapaError as exc:
                    errors.append(f"{name}: {exc}")
            for target in entry.get("enabled_targets", []):
                try:
                    result = self._adapt(name, entry, target)
                    warnings.extend(
                        f"{target}:{name}: {item}" for item in result.warnings
                    )
                except SkillsPapaError as exc:
                    errors.append(f"{target}:{name}: {exc}")
        for target, deployments in registry["deployments"].items():
            if target not in specs:
                errors.append(f"deployment references unknown target: {target}")
                continue
            for name, deployment in deployments.items():
                expected_path = ensure_exact_child(specs[target].root, name)
                if expected_path != _lexical_absolute(Path(deployment.get("path", ""))):
                    errors.append(
                        f"{target}:{name}: deployment path does not match config"
                    )
                    continue
                if not expected_path.is_dir():
                    errors.append(f"{target}:{name}: deployment is missing")
                    continue
                actual = tree_hash(expected_path)
                if actual != deployment.get("tree_hash"):
                    errors.append(f"{target}:{name}: deployed tree has drifted")
        for journal_path in sorted(self.state.transactions_dir.glob("*.json")):
            journal = read_json(journal_path)
            if isinstance(journal, dict) and journal.get("status") == "applying":
                errors.append(
                    f"unfinished transaction requires inspection: {journal.get('id')}"
                )
        for target, spec in specs.items():
            if not spec.root.is_dir():
                continue
            for stage in spec.root.glob(".skills-papa-*"):
                warnings.append(f"{target}: stale staging directory: {stage}")
        return {
            "ok": not errors,
            "errors": errors,
            "warnings": sorted(set(warnings)),
            "checked_skills": len(registry["skills"]),
        }

    def audit(self) -> dict[str, Any]:
        specs = self.state.target_specs()
        owner_managed = self.state.owner_managed_skills()
        records: dict[str, dict[str, Any]] = defaultdict(dict)
        errors: list[dict[str, str]] = []
        legacy_backups: list[dict[str, str]] = []
        inode_groups: dict[tuple[int, int], list[str]] = defaultdict(list)
        for target, spec in specs.items():
            if not spec.root.is_dir():
                continue
            for skill_dir in sorted(spec.root.iterdir()):
                if (
                    skill_dir.name.startswith(".")
                    or skill_dir.name in spec.protected_names
                ):
                    continue
                if _is_legacy_backup_name(skill_dir.name):
                    legacy_backups.append(
                        {
                            "target": target,
                            "name": skill_dir.name,
                            "path": str(skill_dir),
                        }
                    )
                    continue
                skill_md = skill_dir / "SKILL.md"
                if not skill_md.is_file():
                    continue
                try:
                    frontmatter, _ = parse_skill_markdown(skill_md.read_bytes())
                    name = str(frontmatter["name"])
                    if skill_dir.name != name:
                        raise ValidationError(
                            f"directory name {skill_dir.name!r} does not match skill name {name!r}"
                        )
                    digest = tree_hash(skill_dir)
                    records[name][target] = {
                        "path": str(skill_dir),
                        "tree_hash": digest,
                    }
                    file_stat = skill_md.stat()
                    inode_groups[(file_stat.st_dev, file_stat.st_ino)].append(
                        f"{target}:{name}"
                    )
                except (OSError, SkillsPapaError) as exc:
                    errors.append(
                        {"target": target, "path": str(skill_dir), "error": str(exc)}
                    )
        divergent = {
            name: variants
            for name, variants in records.items()
            if name not in owner_managed
            and len({item["tree_hash"] for item in variants.values()}) > 1
        }
        shared = [items for items in inode_groups.values() if len(items) > 1]
        return {
            "targets": {name: str(spec.root) for name, spec in specs.items()},
            "skills": records,
            "skill_count": len(records),
            "divergent": divergent,
            "divergent_count": len(divergent),
            "shared_inode_groups": shared,
            "shared_inode_group_count": len(shared),
            "legacy_backups": legacy_backups,
            "legacy_backup_count": len(legacy_backups),
            "errors": errors,
            "owner_managed": {
                name: {
                    "policy": owner_managed[name],
                    "placements": records.get(name, {}),
                }
                for name in sorted(owner_managed)
            },
            "owner_managed_count": len(owner_managed),
        }

    def migrate(self, *, apply: bool, replace: bool = False) -> dict[str, Any]:
        specs = self.state.target_specs()
        owner_managed = self.state.owner_managed_skills()
        occurrences: dict[str, dict[str, PendingManifest]] = defaultdict(dict)
        invalid: list[dict[str, str]] = []
        skipped_owner_managed: list[dict[str, str]] = []
        for target, spec in specs.items():
            if not spec.root.is_dir():
                continue
            for skill_dir in sorted(spec.root.iterdir()):
                if (
                    skill_dir.name.startswith(".")
                    or skill_dir.name in spec.protected_names
                    or _is_legacy_backup_name(skill_dir.name)
                ):
                    continue
                if skill_dir.name in owner_managed:
                    skipped_owner_managed.append(
                        {
                            "target": target,
                            "skill": skill_dir.name,
                            "path": str(skill_dir),
                            "owner": owner_managed[skill_dir.name]["owner"],
                        }
                    )
                    continue
                if not (skill_dir / "SKILL.md").is_file():
                    continue
                try:
                    pending = self.state.build_pending_manifest(
                        skill_dir,
                        origin={
                            "type": "migration",
                            "target": target,
                            "path": str(skill_dir),
                        },
                    )
                    occurrences[pending.name][target] = pending
                except (OSError, SkillsPapaError) as exc:
                    invalid.append(
                        {"target": target, "path": str(skill_dir), "error": str(exc)}
                    )
        priority = ("universal", "claude", "codex", "opencode")
        changes: list[dict[str, Any]] = []
        summaries: dict[str, Any] = {}
        for name, variants in sorted(occurrences.items()):
            canonical_target = next(target for target in priority if target in variants)
            canonical = variants[canonical_target]
            divergent_targets = {
                target: pending.content_id
                for target, pending in variants.items()
                if pending.content_id != canonical.content_id
            }
            summaries[name] = {
                "canonical_target": canonical_target,
                "manifest": canonical.content_id,
                "targets": sorted(variants),
                "target_variants": divergent_targets,
            }
            changes.append(
                Change(
                    "adopt" if not divergent_targets else "adopt-with-variants",
                    None,
                    name,
                    detail=f"from {canonical_target}",
                ).as_dict()
            )
        result = {
            "operation": "migrate",
            "apply": apply,
            "skills": summaries,
            "changes": changes,
            "invalid": invalid,
            "invalid_count": len(invalid),
            "owner_managed": skipped_owner_managed,
            "owner_managed_count": len(skipped_owner_managed),
        }
        if not apply:
            return result
        with self.state.exclusive_lock():
            registry = self.state.registry()
            newly_excluded = set(self.state.owner_managed_skills()) & set(occurrences)
            if newly_excluded:
                raise ConflictError(
                    "migration candidates became owner-managed: "
                    + ", ".join(sorted(newly_excluded))
                )
            for name, variants in sorted(occurrences.items()):
                if name in registry["skills"] and not replace:
                    raise ConflictError(
                        f"migration would replace registered skill {name!r}; pass --replace"
                    )
                canonical_target = next(
                    target for target in priority if target in variants
                )
                canonical = variants[canonical_target]
                for pending in variants.values():
                    self.state.persist_manifest(pending)
                now = utc_now()
                existing = registry["skills"].get(name)
                target_manifests = {
                    target: pending.content_id
                    for target, pending in variants.items()
                    if pending.content_id != canonical.content_id
                }
                registry["skills"][name] = {
                    "manifest": canonical.content_id,
                    "target_manifests": target_manifests,
                    "origin": {
                        "type": "migration",
                        "canonical_target": canonical_target,
                        "sources": {
                            target: str(pending.source_paths["SKILL.md"].parent)
                            for target, pending in variants.items()
                        },
                    },
                    "enabled_targets": sorted(variants),
                    "created_at": existing.get("created_at", now) if existing else now,
                    "updated_at": now,
                }
                for target, pending in variants.items():
                    destination = ensure_exact_child(specs[target].root, name)
                    registry["deployments"].setdefault(target, {})[name] = {
                        "path": str(destination),
                        "tree_hash": tree_hash(destination),
                        "manifest": pending.content_id,
                        "deployed_at": now,
                        "adopted": True,
                    }
            self.state.save_registry(registry)
        return result

    def export(
        self, name: str, destination: Path, *, target: str | None = None
    ) -> dict[str, Any]:
        registry = self.state.registry()
        entry = registry["skills"].get(name)
        if entry is None:
            raise ValidationError(f"skill is not registered: {name}")
        if destination.exists():
            raise ConflictError(f"export destination already exists: {destination}")
        manifest_id = (
            self._entry_manifest(entry, target) if target else entry["manifest"]
        )
        files, modes = self.state.manifest_files(name, manifest_id)
        destination.mkdir(parents=True)
        for relative, data in files.items():
            output = destination / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(data)
            output.chmod(modes[relative])
        return {
            "operation": "export",
            "skill": name,
            "target_variant": target,
            "destination": str(destination),
            "manifest": manifest_id,
        }
