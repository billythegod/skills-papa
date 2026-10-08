# Skills Papa

**One inventory, every agent.** A local safety and compatibility layer for managing Agent Skills across Codex, Claude Code, OpenCode, and the universal `.agents/skills` root — with migration, target-specific adaptation, drift detection, centralized backups, atomic upgrades, rollback, and crash recovery.

**一份清单，管好所有 agent。** 一个本地安全与兼容层：跨 Codex、Claude Code、OpenCode 与通用 `.agents/skills` 根目录统一管理 Agent Skills——覆盖迁移、按目标适配、漂移检测、集中备份、原子升级、回滚与崩溃恢复。

[English](#english) · [中文](#中文)

## Contents / 目录

**English**

1. [What is Skills Papa](#what-is-skills-papa)
2. [Safety model](#safety-model)
3. [Install](#install)
4. [Safe first run](#safe-first-run)
5. [Lifecycle](#lifecycle)
6. [State and ownership](#state-and-ownership)
7. [Upstream interoperability](#upstream-interoperability)
8. [Use as an agent skill](#use-as-an-agent-skill)
9. [Development](#development)
10. [License](#license-en)

**中文**

1. [Skills Papa 是什么](#skills-papa-是什么)
2. [安全模型](#安全模型)
3. [安装](#安装)
4. [安全的首次运行](#安全的首次运行)
5. [生命周期命令](#生命周期命令)
6. [状态与所有权](#状态与所有权)
7. [与上游的互操作](#与上游的互操作)
8. [作为 agent 技能使用](#作为-agent-技能使用)
9. [开发与测试](#开发与测试)
10. [许可](#许可-zh)

---

<a id="english"></a>

## What is Skills Papa

Skills Papa manages **one** skill inventory and deploys it consistently to multiple agent roots: `codex` (`~/.codex/skills`), `claude` (`~/.claude/skills`), `opencode` (`~/.opencode/skills`), and `universal` (`~/.agents/skills`, readable by every agent that follows the shared convention).

It delegates remote discovery, authentication, and download to [`vercel-labs/skills`](https://github.com/vercel-labs/skills), but keeps **deployment authority for itself**: downloaded content passes through local validation, target adaptation, staging, centralized backup, and a transaction journal before anything touches a live skill root.

Why you might want it:

- You add a skill once and enable it per agent, instead of copying directories by hand.
- Frontmatter is rendered per target (Codex and friends differ in what they accept), so the same source deploys everywhere.
- Every replacement is backed up centrally, every upgrade is one atomic transaction, and an interrupted run leaves the inventory locked until recovery — not half-mutated.

Version `0.3.3` is an alpha release. Installing or initializing Skills Papa does not modify any skill directory.

## Safety model

- **Dry run by default.** Every mutating command is a preview until you pass `--apply`.
- **Content-addressed sources.** Skill content lives as immutable objects in a central store; deployments are rendered from versioned manifests, never edited in place.
- **One transaction per upgrade.** Source registration and all enabled deployments advance together or not at all; every transaction can be rolled back.
- **Centralized backups.** Replaced deployments are stored under `backups/<transaction>/…`. Papa never litters `.bak` / `.backup-*` entries inside agent roots; `audit` flags such legacy entries so you can move them in.
- **Ownership boundaries.** Only paths recorded in the registry are managed. Hidden entries, Codex `.system`, and unmanaged destinations are preserved; an unmanaged destination conflict requires the explicit `--on-conflict backup` policy. Vendor-owned skills can be `exclude`d so their native client keeps full control.
- **No surprises from versions.** Version strings are informative; content hashes decide whether files changed.

## Install

Run from the checkout without persistent installation:

```bash
uv run --isolated --project . skills-papa --help
```

Or install as a user tool:

```bash
uv tool install .
skills-papa --version
```

Use `--json` for scripts and CI. Global options such as `--home` and `--json` must appear before the subcommand:

```bash
skills-papa --json status
skills-papa --home /path/to/state --json doctor
```

The default state root is `~/.local/share/skills-papa`; override it with `--home` or `SKILLS_PAPA_HOME`.

## Safe first run

```bash
skills-papa init            # preview, then: init --apply
skills-papa audit           # inventory + drift report
skills-papa migrate         # adopt existing skills (metadata + immutable copies only)
skills-papa migrate --apply
skills-papa doctor
skills-papa sync --on-conflict backup          # preview
skills-papa sync --on-conflict backup --apply  # deploy to enabled targets
```

Migration never rewrites live targets. To initialize against non-default agent roots, record overrides first (they change nothing until a later `--apply`):

```bash
skills-papa init --target codex=~/.codex/skills --apply
skills-papa init --target universal=~/shared/.agents/skills --apply
```

## Lifecycle

Add a local skill:

```bash
skills-papa add ./my-skill --target codex --target claude   # preview
skills-papa add ./my-skill --target codex --target claude --apply --sync
```

Add a remote skill through the default Vercel provider, or via Git as an intentional fallback:

```bash
skills-papa add owner/repository --skill my-skill --apply --sync
skills-papa add owner/repository --skill my-skill --fetcher native --apply --sync
```

Check and atomically upgrade one skill — the dry run reports old/new manifests, versions, affected targets, and variants that will be retired:

```bash
skills-papa upgrade my-skill            # read-only check
skills-papa upgrade my-skill --apply    # one atomic transaction, rollback-able
skills-papa upgrade my-skill --source owner/repository --apply   # moved / migrated source
skills-papa upgrade --all               # check everything, change nothing
```

Bulk apply is intentionally unavailable; apply reviewed upgrades one skill at a time. `update <skill>` is a compatibility alias for the atomic single-skill workflow.

Import source provenance from Vercel Skills lock files without touching deployments:

```bash
skills-papa import-lock --scope global            # ~/.agents/.skill-lock.json
skills-papa import-lock --scope project --apply   # ./skills-lock.json or explicit path
```

Enable, disable, remove — each a preview first:

```bash
skills-papa status
skills-papa doctor
skills-papa sync --apply
skills-papa disable my-skill --target claude --apply
skills-papa enable my-skill --target claude --apply
skills-papa remove my-skill --apply
```

Leave a vendor-owned or integrity-checked skill to its native lifecycle:

```bash
skills-papa remove vendor-skill --apply
skills-papa exclude vendor-skill --owner vendor-client \
  --reason "integrity checked and updated by vendor-client" --apply
```

Export before editing; the export is a fresh editable directory that changes nothing until you add it back:

```bash
skills-papa export my-skill ./editable-my-skill --apply
skills-papa add ./editable-my-skill --replace --apply --sync
```

Roll back a completed transaction, or recover an interrupted one:

```bash
skills-papa rollback <transaction-id> --apply
skills-papa recover <transaction-id> --apply
```

See [`references/operations.md`](references/operations.md) for the full workflow and [`references/compatibility.md`](references/compatibility.md) for adapter rules.

## State and ownership

```text
~/.local/share/skills-papa/
├── config.json        targets, providers, exclusions
├── registry.json      the managed inventory
├── objects/           content-addressed immutable files
├── manifests/         versioned skill trees
├── transactions/      crash journals and rollback metadata
└── backups/           exact replaced deployments
```

Upgrade metadata is read from `metadata.version` in `SKILL.md` (top-level `version` accepted for compatibility).

## Upstream interoperability

- The default `vercel` fetcher delegates remote discovery, private-repo auth, archive handling, and download to [`vercel-labs/skills`](https://github.com/vercel-labs/skills) (pinned `skills@1.5.23`, telemetry disabled, isolated temporary project). The older `skills-cli` fetcher name remains an alias.
- Vercel global `.skill-lock.json` and project `skills-lock.json` files can seed provenance for already-registered skills.
- Skills Papa never invokes Vercel's live install/update/remove against agent roots.
- Target conversion follows the public invocation-policy mapping documented by [`agent-skill-porter`](https://github.com/skill-mill/agent-skill-porter), with a built-in deterministic adapter so ordinary local use is standalone.
- No upstream source code is vendored.

Provider settings live in `config.json`; only `skills`, `skills@latest`, or `skills@X.Y.Z` is accepted, and the checked-in default stays pinned until integration tests pass against a newer release.

## Use as an agent skill

The repository root is itself a valid skill (`SKILL.md` included). Install it with a compatible skill installer, or link this directory into an agent's skill root after validation. Once registered, an agent gets the full operating discipline — dry-run previews, post-sync duplicate-discovery gates, ownership rules — described in `SKILL.md`.

## Development

Tests run in an isolated environment:

```bash
PYTHONPATH=src uv run --isolated --no-project \
  --with 'pytest>=8.3,<10' --with 'PyYAML>=6.0.2,<7' pytest -q tests
```

<a id="license-en"></a>

## License

[MIT](LICENSE)

---

<a id="中文"></a>

# 中文文档

## Skills Papa 是什么

Skills Papa 管理**一份**技能清单，并一致地部署到多个 agent 根目录：`codex`（`~/.codex/skills`）、`claude`（`~/.claude/skills`）、`opencode`（`~/.opencode/skills`）与 `universal`（`~/.agents/skills`，所有遵循通用约定的 agent 都能读取）。

远程发现、认证与下载委托给 [`vercel-labs/skills`](https://github.com/vercel-labs/skills)，但**部署权威始终留在本地**：下载的内容要经过本地校验、按目标适配、暂存、集中备份与事务日志，才会接触任何线上技能根目录。

它适合这样的你：

- 一次添加，按 agent 启用，不用再手工复制目录；
- 同一份源码按目标渲染 frontmatter（各家 agent 接受的格式并不相同），处处可部署；
- 每次替换都有集中备份，每次升级是一个原子事务，中断的运行会把清单锁住直到完成恢复——而不是留下改了一半的状态。

`0.3.3` 为 alpha 版本。安装与初始化不会修改任何技能目录。

## 安全模型

- **默认演练（dry run）。** 所有变更命令在加上 `--apply` 之前都只是预览。
- **内容寻址的源。** 技能内容以不可变对象存放在中央仓库，部署由版本化清单渲染生成，绝不就地修改。
- **升级即一个事务。** 源注册与所有已启用的部署要么一起前进、要么全部不动；每个事务都可回滚。
- **集中备份。** 被替换的部署保存在 `backups/<transaction>/…`。Papa 绝不在 agent 根目录里留下 `.bak` / `.backup-*` 之类的条目；`audit` 会把这类遗留就地备份标记出来，便于收编。
- **所有权边界。** 只管理登记在册的路径；隐藏条目、Codex 的 `.system` 与未纳管的目录一律保留，未纳管目标冲突必须显式 `--on-conflict backup`。厂商自有技能可以 `exclude`，交还其原生客户端全权管理。
- **版本不制造意外。** 版本号仅供参考；判断文件是否变化依据内容哈希。

## 安装

不落盘安装、直接在检出目录运行：

```bash
uv run --isolated --project . skills-papa --help
```

或安装为用户工具：

```bash
uv tool install .
skills-papa --version
```

脚本与 CI 请用 `--json`。`--home`、`--json` 等全局选项必须放在子命令之前：

```bash
skills-papa --json status
skills-papa --home /path/to/state --json doctor
```

默认状态根目录是 `~/.local/share/skills-papa`，可用 `--home` 或环境变量 `SKILLS_PAPA_HOME` 覆盖。

## 安全的首次运行

```bash
skills-papa init            # 先预览，再：init --apply
skills-papa audit           # 清单与漂移报告
skills-papa migrate         # 收编既有技能（仅元数据与不可变副本）
skills-papa migrate --apply
skills-papa doctor
skills-papa sync --on-conflict backup          # 预览
skills-papa sync --on-conflict backup --apply  # 部署到已启用目标
```

迁移不会改写线上目标。要初始化为非默认的 agent 根目录，先登记覆盖项（在被 `--apply` 之前不会改动任何线上技能）：

```bash
skills-papa init --target codex=~/.codex/skills --apply
skills-papa init --target universal=~/shared/.agents/skills --apply
```

## 生命周期命令

添加本地技能：

```bash
skills-papa add ./my-skill --target codex --target claude   # 预览
skills-papa add ./my-skill --target codex --target claude --apply --sync
```

通过默认 Vercel 提供器添加远程技能，或以 Git 直连作为有意的回退：

```bash
skills-papa add owner/repository --skill my-skill --apply --sync
skills-papa add owner/repository --skill my-skill --fetcher native --apply --sync
```

检查并原子升级单个技能——预览会给出新旧清单、版本、受影响目标与将被退役的变体：

```bash
skills-papa upgrade my-skill            # 只读检查
skills-papa upgrade my-skill --apply    # 一个原子事务，可回滚
skills-papa upgrade my-skill --source owner/repository --apply   # 迁移过/搬家了的源
skills-papa upgrade --all               # 全量检查，不做任何修改
```

批量 apply 是有意不提供的；请逐个技能审阅后应用。`update <skill>` 是原子单技能流程的兼容别名。

从 Vercel Skills 锁文件导入来源信息，不触碰部署：

```bash
skills-papa import-lock --scope global            # ~/.agents/.skill-lock.json
skills-papa import-lock --scope project --apply   # ./skills-lock.json 或显式路径
```

启用、停用、移除——每一步都先预览：

```bash
skills-papa status
skills-papa doctor
skills-papa sync --apply
skills-papa disable my-skill --target claude --apply
skills-papa enable my-skill --target claude --apply
skills-papa remove my-skill --apply
```

把厂商自有或做完整性校验的技能留给它的原生生命周期：

```bash
skills-papa remove vendor-skill --apply
skills-papa exclude vendor-skill --owner vendor-client \
  --reason "integrity checked and updated by vendor-client" --apply
```

编辑前先导出；导出的是一份全新的可编辑目录，在你显式重新 add 回来之前不改变注册表与任何部署：

```bash
skills-papa export my-skill ./editable-my-skill --apply
skills-papa add ./editable-my-skill --replace --apply --sync
```

回滚已完成的事务，或恢复被中断的事务：

```bash
skills-papa rollback <transaction-id> --apply
skills-papa recover <transaction-id> --apply
```

完整命令流程见 [`references/operations.md`](references/operations.md)，适配器规则见 [`references/compatibility.md`](references/compatibility.md)。

## 状态与所有权

```text
~/.local/share/skills-papa/
├── config.json        目标、提供器、排除项
├── registry.json      受管清单
├── objects/           内容寻址的不可变文件
├── manifests/         版本化的技能树
├── transactions/      崩溃日志与回滚元数据
└── backups/           被替换部署的精确备份
```

升级元数据读取自 `SKILL.md` 的 `metadata.version`（兼容接受顶层 `version`）。

## 与上游的互操作

- 默认 `vercel` 抓取器把远程发现、私有仓库认证、压缩包处理与下载委托给 [`vercel-labs/skills`](https://github.com/vercel-labs/skills)（固定 `skills@1.5.23`、关闭遥测、隔离的临时工程）。旧名 `skills-cli` 抓取器保留为别名。
- Vercel 全局 `.skill-lock.json` 与项目 `skills-lock.json` 可以为已注册技能补充来源信息。
- Skills Papa 绝不调用 Vercel 的线上 install/update/remove 去改动 agent 根目录。
- 目标转换遵循 [`agent-skill-porter`](https://github.com/skill-mill/agent-skill-porter) 公开的调用策略映射，并内置确定性适配器，日常本地使用完全独立。
- 未 vendored 任何上游源码。

提供器设置位于 `config.json`；只接受 `skills`、`skills@latest` 或 `skills@X.Y.Z`，检出默认值保持固定，直到集成测试通过更新的上游版本。

## 作为 agent 技能使用

仓库根目录本身就是一个合法技能（含 `SKILL.md`）。用兼容的技能安装器安装，或在校验后把本目录链接进某个 agent 的技能根目录。注册之后，agent 将获得 `SKILL.md` 描述的完整操作纪律——演练预览、同步后重复发现核对门槛、所有权规则。

## 开发与测试

测试在隔离环境中运行：

```bash
PYTHONPATH=src uv run --isolated --no-project \
  --with 'pytest>=8.3,<10' --with 'PyYAML>=6.0.2,<7' pytest -q tests
```

<a id="许可-zh"></a>

## 许可

[MIT](LICENSE)
