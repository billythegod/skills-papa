# Vercel Skills Provider

## Responsibility boundary

Vercel Skills resolves remote sources, uses existing Git or `gh` authentication, downloads content, and emits source-lock metadata. Skills Papa runs it only inside a temporary project with `--agent universal --copy --yes --full-depth` and both `DISABLE_TELEMETRY=1` and `DO_NOT_TRACK=1`.

The provider must never be pointed at a configured Agent root. After download, Papa independently validates the skill tree, rejects input symlinks and unsafe YAML, renders target-specific frontmatter, stages every changed deployment, and performs its own backup transaction.

Local directory sources do not require Vercel Skills. They are read directly so local authoring remains fast and offline.

## Provider selection

`--fetcher vercel` is the remote default. `--fetcher skills-cli` is its compatibility alias. Use `--fetcher native` only when the source is Git-compatible and direct Git clone behavior is intentional.

The package is pinned in `config.json`:

```json
{
  "providers": {
    "vercel_skills": {
      "package": "skills@1.5.23",
      "telemetry": false
    }
  }
}
```

Accepted package forms are `skills`, `skills@latest`, and semantic `skills@X.Y.Z` versions. Prefer the tested pin. Upgrading the pin requires isolated provider, lock parsing, CLI, and live-source smoke tests.

## Provenance retained

When present, Papa reads the temporary project `skills-lock.json` and retains stable fields such as source identifier, source type, source URL, ref, skill path, upstream folder hash, and provider package. Papa's own content manifest remains authoritative for deployment and drift detection.

URLs containing embedded HTTP credentials are never persisted. Generic Git/GitLab lock entries that lost their original host are rejected rather than guessed as GitHub sources.

## Importing existing locks

`import-lock` accepts Vercel global lock schema versions and project lock files. Import is deliberately narrower than installation:

- matching registered skills receive Vercel source provenance;
- unregistered names are reported but not created;
- enabled targets and deployment manifests are unchanged;
- invalid entries block apply unless `--skip-invalid` is explicit;
- non-migration authoritative origins require `--replace-origin`;
- apply produces a registry-only transaction that supports rollback.

After import, run `upgrade <skill>` without `--apply` to fetch the recorded source and compare its content with Papa's current canonical manifest. Source association alone never authorizes an upgrade.
