# Compatibility

## Preserving host invocation differences during an upgrade

When retired migration variants differ only in intentional invocation settings,
record the verified current host values in the canonical source before upgrading:

```yaml
metadata:
  skills-papa/target-policies: '{"codex":true,"claude":false}'
```

The metadata value must be a JSON string, keeping the portable metadata field
compatible with the Agent Skills string-to-string convention. Its JSON object
maps `codex`, `claude`, or `opencode` to boolean `allow_implicit_invocation`
values. JSON `false` is an explicit override, not an absent value. Arrays,
nulls, nested objects, numeric or string booleans, unknown hosts, duplicate
hosts, and a YAML mapping in place of the JSON string are rejected before
deployment. An empty JSON object means no overrides.

A named target overrides the generic
sidecar/frontmatter invocation flag; other targets retain existing conversion
rules. Universal emits no host invocation flag and retains the original metadata
string unchanged for future upgrades. Use this
only to preserve verified settings or implement a requested policy change, not
to infer explicit-only mode from sensitive operations. Preserve unrelated
sidecar fields. This annotation does not enable a disabled deployment or permit
external actions.

Skills Papa currently renders four targets.

## Codex

The generated `SKILL.md` retains `name`, `description`, `license`, `allowed-tools`, and `metadata`. Claude's `disable-model-invocation` is removed from frontmatter and converted to:

```yaml
policy:
  allow_implicit_invocation: false
```

inside `agents/openai.yaml`. Existing valid Codex sidecar fields are preserved.

## Claude Code

Claude-specific frontmatter such as `argument-hint`, `allowed-tools`, `model`, `context`, `hooks`, and `disable-model-invocation` is retained. A Codex invocation policy is converted back when the Claude field is absent.

## OpenCode

Portable metadata and OpenCode-compatible invocation metadata are retained. Codex sidecars are omitted. Unsupported fields are reported as adapter warnings.

## Universal

The universal target uses the portable subset: `name`, `description`, `license`, `compatibility`, `allowed-tools`, and `metadata`. Agent-specific invocation policy is deliberately omitted and reported as a warning.

## Codex and universal duplicate discovery

Codex can discover both its configured Codex root and the universal root. When the same skill name exists in both, prevent duplicate slash-menu entries without removing the universal deployment:

1. Resolve the live Codex and universal roots from `skills-papa status` or `audit`. Do not assume default paths when configuration says otherwise.
2. Read each `SKILL.md` version declaration. Also inspect Skills Papa's expected and actual manifests, because equal version labels do not prove equal content. Target-specific rendered tree hashes may legitimately differ; compare each deployment with its own expected hash and distinguish adapter output from unmanaged drift.
3. If either version is missing, the versions differ, or equal labels hide substantive non-adapter differences, do not synchronize, hide, or choose a winner automatically. Report both paths and versions plus a focused summary of changed frontmatter, instructions, sidecars, scripts, and support-file inventory. Ask the user which copy or upstream should become authoritative, then use the reviewed Skills Papa dry-run and apply workflow to reconcile it.
4. If the versions and managed content are equivalent, preserve the universal deployment and ensure Codex configuration contains exactly one entry for the universal `SKILL.md`:

   ```toml
   [[skills.config]]
   path = "/absolute/universal/root/skill-name/SKILL.md"
   enabled = false
   ```

   This is a Codex-only discovery override. Do not disable the universal target in Skills Papa.
5. Before changing Codex configuration, save a timestamped copy under Skills Papa's centralized backup directory. Preserve every unrelated setting and existing skill override. After editing, run `codex --strict-config --version` and, when available, inspect `codex debug prompt-input diagnostic`; a fresh skill list must retain the Codex path and omit the universal duplicate. Existing sessions may need to be restarted to refresh their menu.

## Storage behavior

Original bytes are stored once in a content-addressed object store. Target-specific `SKILL.md` and sidecars are small generated files. On macOS, support files use APFS clone-on-write; Linux requests reflink-or-copy. Ordinary copies are the safe fallback. Hardlinks are disabled by default because a writable hardlink could corrupt the object store; explicitly set `SKILLS_PAPA_ALLOW_HARDLINKS=1` only for immutable filesystems and reviewed workflows.

## Boundaries

- Supported hosts are macOS and Linux; the state lock uses `flock`.
- Input symlinks are rejected to prevent path escape.
- The built-in adapters cover Codex, Claude Code, OpenCode, and universal roots. Revalidate after those products change their schemas.
- Removed objects are retained for recovery in version 0.3; destructive garbage collection is intentionally not implemented yet.
- Client-owned skills can be marked in `config.owner_managed_skills`; they bypass every target adapter and remain byte-for-byte under their native owner.
