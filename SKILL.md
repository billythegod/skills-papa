---
name: skills-papa
description: Govern Vercel and local Agent Skills across Codex, Claude Code, OpenCode, and universal roots with migration, compatibility adaptation, drift checks, centralized backups, atomic upgrades, rollback, and recovery. Use for shared skill lifecycle management, not for authoring unrelated skill content.
metadata:
  version: 0.3.3
  local_revision: 2026-09-10.1
  skills-papa/target-policies: '{"claude":true,"codex":true,"opencode":true}'
---
# Skills Papa

Use the bundled `skills-papa` CLI as the authority for managed skill state. Prefer an installed command; otherwise run it from this skill directory with `uv run --isolated --project <skill-directory> skills-papa`.

Start with read-only evidence:

```bash
skills-papa status
skills-papa doctor
skills-papa audit
skills-papa upgrade --all
```

Every mutating command is a dry run unless `--apply` is present. Run and inspect the dry run first. A clear user request to add, migrate, upgrade, update, synchronize, enable, disable, remove, roll back, or recover a skill authorizes the matching `--apply` after the preview when targets are unambiguous; an audit, explanation, status, or upgrade-check request does not authorize `--apply`.

Use `upgrade <skill>` for the normal read-only check and `upgrade <skill> --apply` only after reviewing the old/new manifests, versions, enabled targets, and retired variants. For migrated or missing sources, use `--source` to establish a canonical upstream. Also use it after any direct edit to a deployed skill (typically the universal root): a plain `upgrade <skill> --apply` deploys the *registered* source and silently overwrites the local edits, so first re-establish the edited deployment as canonical (`upgrade <skill> --source <edited-deployment-path>`, review the previewed trees and version, then `--apply`) and treat a dry-run version *drop* against the live deployment as a stop signal. If an upgrade already overwrote newer local work, `rollback <upgrade-transaction> --apply` restores it. `upgrade --all` is check-only; do not simulate bulk apply with an unreviewed loop.

Remote `add` and replacement-source resolution use the pinned `vercel-labs/skills` provider by default. It may fetch and authenticate, but it writes only to a temporary project; Skills Papa remains the only component allowed to deploy into configured Agent roots. Use `--fetcher native` only for a reviewed Git-compatible source or when `npx` is unavailable.

Use `import-lock` to associate already registered or migrated skills with sources from Vercel global or project lock files. It does not install missing skills or change live deployments. Inspect invalid, missing, and conflicting entries before `--apply`; never add `--skip-invalid` or `--replace-origin` without explicit intent.

Use `exclude NAME --owner OWNER` for skills whose native client performs integrity checks or owns their update lifecycle. Remove the Papa registration first, restore the skill with the owner's native installer, then apply the exclusion. Excluded skills remain visible to `audit` but must not be adopted, adapted, synchronized, or upgraded by Papa. Use `include` only for an explicit ownership transfer.

Before leaving the same skill name in both the configured Codex and universal roots, reconcile Codex discovery. Compare declared versions and managed manifests, allowing only the target-adapter differences described in [references/compatibility.md](references/compatibility.md). If versions are missing, unequal, or the same version hides substantive drift, stop before changing visibility: report both paths, versions, and a focused content difference, then ask the user which copy is authoritative. If they are equivalent, keep the universal deployment for other agents and add exactly one disabled `skills.config` entry for its `SKILL.md` in Codex configuration. Back up the configuration, preserve unrelated entries, validate it, and verify a fresh Codex skill list contains only the Codex copy. A synchronization request authorizes this duplicate-visibility cleanup only after equivalence is established; it never authorizes choosing between conflicting copies.

After every synchronization that enables both `codex` and `universal` for a skill, make this duplicate-discovery reconciliation a required post-sync gate before reporting completion. Once each target is `status: ok` and the repeat upgrade check is `current`, compare the live Codex and universal copies; when they are equivalent under the target adapter, back up `~/.codex/config.toml` and add exactly one `enabled = false` `skills.config` entry for `~/.agents/skills/<skill-name>/SKILL.md`. This hides only the universal entry from Codex and does not disable the universal deployment for other agents. If the copies are not equivalent, stop and report the focused difference rather than hiding either copy.

Do not edit managed target directories directly. Never bypass unmanaged-destination conflicts with filesystem commands. Use `--on-conflict backup` only when the user intends Skills Papa to take ownership. Preserve `.system`, hidden entries, and unregistered skills.

If a command reports an unfinished transaction, stop other mutations and inspect `recover <transaction>` before applying recovery. Use `--json` when results need programmatic inspection.

- Read [references/operations.md](references/operations.md) for lifecycle commands, migration, and recovery.
- Read [references/compatibility.md](references/compatibility.md) when diagnosing frontmatter conversion or target-specific behavior.
- Read [references/vercel-skills.md](references/vercel-skills.md) for remote source resolution, lock import, provider configuration, and responsibility boundaries.
