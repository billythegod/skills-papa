# Operations

## Command shape

Global options must precede the subcommand:

```bash
skills-papa --home ~/.local/share/skills-papa --json status
```

Without `--apply`, commands that can mutate state only print their plan. Exit code `2` means validation, conflict, or state failure. `doctor` exits `1` when integrity problems are found.

## Initialize and inspect

```bash
skills-papa init
skills-papa init --apply
skills-papa audit
skills-papa doctor
skills-papa status
```

Override a target during initialization when needed:

```bash
skills-papa init --target codex=~/.codex/skills --apply
```

## Migrate existing skills

Migration adopts valid existing skills without changing the live directories. Divergent target versions are retained as target-specific manifests; invalid occurrences are reported and skipped. Skills listed in `config.owner_managed_skills` are also reported and skipped, protecting client-bundled or integrity-checked skills from later adoption.

```bash
skills-papa migrate
skills-papa migrate --apply
skills-papa doctor
skills-papa sync --on-conflict backup
skills-papa sync --on-conflict backup --apply
```

Do not apply synchronization until the migration report and invalid occurrences have been reviewed.

## Add and upgrade

Local source:

```bash
skills-papa add ./my-skill --target codex --target claude
skills-papa add ./my-skill --target codex --target claude --apply --sync
```

Remote sources use `vercel-labs/skills` by default. The provider handles GitHub, GitLab, generic Git, archives, well-known sources, and existing Git/`gh` authentication, but installs only into Papa's temporary project:

```bash
skills-papa add owner/repository --skill my-skill
skills-papa add owner/repository --skill my-skill --apply --sync
```

Use direct Git resolution only as an intentional fallback:

```bash
skills-papa add owner/repository --skill my-skill --fetcher native
```

`--fetcher skills-cli` remains a compatibility alias for `--fetcher vercel`.

Check the registered source. This is a dry run and reports source availability, old/new content manifests, declared versions, enabled targets, and any migrated target variants that would be retired:

```bash
skills-papa upgrade my-skill
```

Apply the reviewed upgrade atomically:

```bash
skills-papa upgrade my-skill --apply
```

Skills Papa stages every enabled deployment before changing a live target. It then records replaced directories under `backups/<upgrade-transaction>/<target>/<skill>/` and commits the registry in the same transaction. A staging or deployment error restores the previous registry and deployments. Use `rollback <upgrade-transaction> --apply` to restore a completed upgrade.

### Syncing after direct edits to a deployed skill

The registered source — not the live deployment — is what `upgrade` deploys. If you edited a deployed skill in place (for example the universal root `~/.agents/skills/<name>`) while the registry still points at an older source snapshot, a plain `upgrade <name> --apply` rolls *every* enabled target, including the edited one, back to the stale registered source. Observed case (2026-09-06): a v2.6.0 registry source overwrote a v2.9.0 universal deployment across all four targets in one transaction.

Correct sequence after any direct edit:

```bash
# 1. dry-run the plain upgrade first; a version drop vs the live deployment is the stop signal
skills-papa upgrade <name>

# 2. re-establish the edited deployment as the canonical source; review trees and version
skills-papa upgrade <name> --source ~/.agents/skills/<name>

# 3. apply, then verify each target's SKILL.md version and any new files
skills-papa upgrade <name> --source ~/.agents/skills/<name> --apply
```

If the overwrite already happened, restore from the transaction backup:

```bash
skills-papa rollback <upgrade-transaction> --apply
```

The registry source (its manifest hash) changes only when a `--source` re-anchor is accepted, so one `--source` run also fixes subsequent plain `upgrade` checks.

Migration intentionally records `origin.type=migration`, which has no remote update address. Establish a canonical upstream without editing the registry:

```bash
skills-papa upgrade my-skill --source ./maintained/my-skill
skills-papa upgrade my-skill --source owner/repository --subdir path/to/skill
skills-papa upgrade my-skill --source owner/repository --apply
```

An accepted replacement source becomes the source for later checks. If the skill was migrated with target-specific variants, the dry run lists them in `retired_target_variants`; apply replaces enabled targets with the new canonical source while preserving target enablement.

Check every registered skill:

```bash
skills-papa upgrade --all
```

`--all` is check-only in version 0.3.1. Unavailable sources are reported per skill and do not stop other checks. Review and apply each upgrade separately so every write has its own rollback boundary.

The older command remains an alias for the same atomic behavior:

```bash
skills-papa update my-skill
skills-papa update my-skill --apply
```

`update --no-sync --apply` is refused because changing the registered source without its enabled deployments would break the atomic upgrade guarantee. Use `add --replace` only when a deliberate source-only replacement is required.

Declare an optional version in skill frontmatter for clearer reports:

```yaml
metadata:
  version: 2.1.0
```

The content manifest remains authoritative. A changed version with unchanged files is not possible because the version declaration is itself part of `SKILL.md`; a changed repository revision with identical skill files is recorded as a provenance-only upgrade.

## Import Vercel lock provenance

Importing a lock file links sources only to skills already registered in Papa. It never calls Vercel's installer, registers absent skills, or changes enabled targets and deployments.

```bash
skills-papa import-lock --scope global
skills-papa import-lock --scope global --apply

skills-papa import-lock --scope project
skills-papa import-lock /path/to/skills-lock.json --scope project --apply
```

Defaults:

- global: `$XDG_STATE_HOME/skills/.skill-lock.json` when `XDG_STATE_HOME` is set, otherwise `~/.agents/.skill-lock.json`;
- project: `./skills-lock.json`.

The dry run groups entries as linked, current, not registered, conflicting, or invalid. Migration and prior Vercel origins are replaceable by default. Replacing a local or native Git authority requires `--replace-origin`. Invalid entries block apply unless the user deliberately accepts partial import with `--skip-invalid`.

Applying creates an `import-lock-*` registry transaction. It can be passed to `rollback`; since no deployment is changed, rollback restores only the previous registry source associations.

## Enable, disable, and remove

```bash
skills-papa enable my-skill --target codex
skills-papa enable my-skill --target codex --apply
skills-papa enable my-skill --target codex --on-conflict backup --apply

skills-papa disable my-skill --target claude
skills-papa disable my-skill --target claude --apply

skills-papa remove my-skill
skills-papa remove my-skill --apply
```

`disable` retains the registered source. `remove` unregisters the skill and backs up managed deployments; immutable objects remain available for transaction recovery.

## External ownership exclusions

After removing a client-owned skill from Papa and restoring it with the owner's native installer, make the ownership boundary persistent:

```bash
skills-papa exclude computer-use --owner orca \
  --reason "bundled and integrity-checked by Orca"
skills-papa exclude computer-use --owner orca \
  --reason "bundled and integrity-checked by Orca" --apply
```

Excluded names cannot be added or migrated. Their live directories remain visible under `audit.owner_managed`, but Papa does not render, synchronize, upgrade, disable, or remove them. `include NAME --apply` removes the guard without adopting the directory; run `migrate` or `add` separately after reviewing the ownership transfer.

All replaced or removed deployments are moved to `~/.local/share/skills-papa/backups/<transaction>/<target>/<skill>/`. Target skill roots contain only active deployments; Skills Papa does not create sibling `.backup-*` or `.bak` entries. `audit` reports any such entries left by older tools or manual workflows as legacy in-place backups.

## Rollback and crash recovery

Roll back the most recent compatible completed transaction:

```bash
skills-papa rollback sync-YYYYMMDDTHHMMSSZ-xxxxxxxx
skills-papa rollback sync-YYYYMMDDTHHMMSSZ-xxxxxxxx --apply
```

Out-of-order rollback is refused after newer registry changes.

If a process stopped during a transaction, all other writes are blocked:

```bash
skills-papa recover sync-YYYYMMDDTHHMMSSZ-xxxxxxxx
skills-papa recover sync-YYYYMMDDTHHMMSSZ-xxxxxxxx --apply
skills-papa doctor
```

## Export for editing

Managed manifests are immutable. Export one before editing, then add it back with `--replace`:

```bash
skills-papa export my-skill ./editable-my-skill --apply
skills-papa add ./editable-my-skill --replace --apply --sync
```
