---
title: Profile System
nav_order: 3
---

# Profile System

Profiles are **directory packages** that configure Claude Code execution. Each
profile is a directory containing a `profile.yml` for Agenticore metadata and a
`.claude/` directory with native Claude Code configuration files.

Agenticore does **not** bundle any profiles. Profiles come from two external
sources: your [agentihooks](https://github.com/The-Cloud-Clockwork/agentihooks)
integration and your user directory.

## Profile Directory Layout

```
<profiles-dir>/{name}/
├── profile.yml          # Agenticore metadata (model, turns, auto_pr, etc.)
├── .claude/
│   ├── settings.json    # Hooks, tool permissions, env vars
│   ├── CLAUDE.md        # System instructions for Claude
│   ├── agents/          # Custom subagents
│   └── skills/          # Custom slash-command skills
└── .mcp.json            # MCP server config merged into the job
```

## profile.yml Schema

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | directory name | Profile identifier |
| `description` | string | `""` | Human-readable description |
| `claude.model` | string | `sonnet` | Claude model |
| `claude.max_turns` | int | `80` | `--max-turns` |
| `claude.output_format` | string | `json` | `--output-format` |
| `claude.permission_mode` | string | `bypassPermissions` | `--permission-mode` |
| `claude.timeout` | int | `3600` | Process timeout in seconds |
| `claude.worktree` | bool | `true` | Pass `--worktree` to Claude |
| `claude.effort` | string/null | `null` | `--effort` (e.g. `high`) |
| `claude.max_budget_usd` | float/null | `null` | `--max-budget-usd` |
| `claude.fallback_model` | string/null | `null` | `--fallback-model` |
| `auto_pr` | bool | `true` | Create PR on success |
| `extends` | string/null | `null` | Inherit from another profile |

## Profile Discovery

Profiles are loaded from two directories. Later sources override earlier ones
when names collide.

```
{AGENTICORE_AGENTIHOOKS_PATH}/profiles/   ← agentihooks integration
~/.agenticore/profiles/                   ← user profiles (always checked)
```

**agentihooks** is the authoritative source for organisation-wide profiles. It
owns the full profile authoring pipeline — hook wiring, MCP categories, system
prompts, and the `build_profiles.py` generator. It ships as a **PyPI package**
(`agentihooks`) and is a pip dependency of agenticore — so profiles are already
available in the installed package. Set `AGENTICORE_AGENTIHOOKS_PATH` only when
you want to overlay the PyPI install with a live local checkout for development.

**User profiles** (`~/.agenticore/profiles/`) are for personal overrides and
local experimentation. They always take highest priority.

## agentihooks Profiles

Profiles come from the installed [agentihooks](https://pypi.org/project/agentihooks/)
package, which owns the full profile authoring pipeline. The `.claude/settings.json`
and `.mcp.json` inside each profile directory are **pre-built** by agentihooks'
`scripts/build_profiles.py`, which merges `profiles/_base/settings.base.json`
(hook wiring, permissions) with any per-profile overrides.

In Kubernetes deployments agentihooks is installed from PyPI as part of the
container image — **no clone, no watcher.** Pod restart picks up a new release
(bump the agentihooks floor in agenticore's `pyproject.toml` or rebuild to pull
a newer PyPI version). Set `AGENTICORE_AGENTIHOOKS_URL` to override with a one-
shot clone + editable install for bleeding-edge testing; set
`AGENTICORE_AGENTIHOOKS_PATH` for a dev loopback over a mounted checkout.
### Agentihooks Bundle

The **agentihooks-bundle** repo (`AGENTICORE_AGENTIHOOKS_BUNDLE_URL`) provides
companion configuration passed to `agentihooks init --bundle <path>`. Unlike
agentihooks itself (a PyPI dep), the bundle is still a content repo cloned at
startup and has its own background watcher controlled by
`AGENTICORE_AGENTIHOOKS_BUNDLE_SYNC_INTERVAL` (default 300s, `0` disables).

### Agentihub — direct provisioning

In Agent Mode, agent packages come directly from **agentihub** — not from
agentihooks profiles. Agenticore's `agent_mode/initializer.py` clones agentihub
and copies `agents/{name}/package/` → `/app/package/`. A background watcher
refreshes the clone every `AGENTICORE_AGENTIHUB_SYNC_INTERVAL` seconds
(default 300, `0` disables).

```
agenticore   = execution engine (this project)
agentihooks  = hook system + MCP tools (guardrails, integrations)
agentihub    = agent identities (CLAUDE.md, prompts, evaluation)
```

| Variable | Description |
|----------|-------------|
| `AGENTICORE_AGENTIHUB_URL` | Git URL for the agentihub repo |
| `AGENTICORE_AGENTIHUB_PATH` | Explicit path override (skips cloning) |
| `AGENTICORE_AGENTIHUB_SYNC_INTERVAL` | Hot-reload interval in seconds (`0` disables) |
| `AGENTIHUB_AGENT` | Agent name to load (matches `agents/{name}/` directory) |

## Writing a Profile

Minimal `profile.yml`:

```yaml
name: code
description: "Autonomous coding worker"

claude:
  model: claude-sonnet-4-6
  max_turns: 80
  permission_mode: bypassPermissions
  timeout: 3600
  worktree: true

auto_pr: true
```

The `.claude/settings.json` and `.mcp.json` files are generated by the agentihooks
build script. If writing a user profile (`~/.agenticore/profiles/`), create them
manually. Example `.claude/settings.json`:

```json
{
  "permissions": {
    "allow": [
      "Bash(*)",
      "Read(*)",
      "Write(*)",
      "Edit(*)",
      "Glob(*)",
      "Grep(*)",
      "Task(*)"
    ]
  }
}
```

And `.claude/CLAUDE.md` providing system instructions:

```markdown
# Agenticore Worker

## Guidelines
- Commit with descriptive messages
- Do NOT create PRs — the system handles that
- Focus on the task, be thorough, test your changes
```

## Profile Inheritance

A profile can extend another using the `extends` field:

```yaml
name: code-strict
extends: code          # inherits all settings from 'code'

claude:
  max_turns: 20
  effort: high
```

Child values override parent defaults. The `.claude/` files are **layered**
(child overlays parent) during materialization — files present in the child
profile replace the parent's versions; files only in the parent are kept.

## Materialization

Before each job, `materialize_profile()` resolves the profile directory path
for tracking and `.mcp.json` merging.

Claude Code uses `~/.claude/` by default — `CLAUDE_CONFIG_DIR` is **not set**.
Agentihooks installs profiles into `~/.claude/` at container startup via
`agentihooks global`. The `CLAUDE_CODE_HOME_DIR` env var is set as a safeguard
pointing to the home directory root (e.g., `/shared`).

### Simple profiles (no `extends`) — zero I/O

The profile directory path is returned for tracking only. No files are copied
and no env var override is applied. Claude Code reads config from `~/.claude/`.

### Profiles with `extends` — isolated merge directory

When a profile uses `extends`, the chain must be merged. The merged output goes
to an isolated per-job directory for `.mcp.json` content, which the runner
injects into the job CWD.

Local / Docker mode (no `AGENTICORE_SHARED_FS_ROOT`):

```
/tmp/agenticore-jobs/{job-id}/
├── .claude/
│   ├── settings.json    ← base first, child overlay on top
│   └── CLAUDE.md
└── .mcp.json            ← mcpServers merged across chain
```

Kubernetes / shared FS mode (`AGENTICORE_SHARED_FS_ROOT` set):

```
/shared/jobs/{job-id}/
├── .claude/
│   ├── settings.json
│   └── CLAUDE.md
└── .mcp.json
```

The `job_config_dir` is stored on the job record for auditing. The repo clone
is always left clean.

## Profile to CLI Args

```
profile.yml (claude section)
       │
       ▼
build_cli_args()
       │
       ▼
claude --worktree
       --model claude-sonnet-4-6
       --max-turns 80
       --output-format json
       --permission-mode bypassPermissions
       -p "<task>"
```

The `build_cli_args()` function in `profiles.py` converts the `claude` section
of `profile.yml` into CLI flags. The task is always last with `-p`.

## Profile Resolution and Routing

```
Request arrives (profile="" or profile="code")
         │
         ▼
 ┌───────┴────────┐
 │   router.py    │
 │   route()      │
 └───────┬────────┘
         │
   ┌─────┴──────┐
   │             │
   ▼             ▼
profile       no profile
specified?    specified
   │             │
   ▼             ▼
validate      use default
exists?       (agentihooks_profile)
   │             │
   └──────┬──────┘
          │
          ▼
   resolved profile name
```

If the requested profile doesn't exist, the router falls back to
`agentihooks_profile` (default: `coding`, set via `AGENTIHOOKS_PROFILE`).

## Template Variables

The `--append-system-prompt` flag receives dynamic context built from the
job at execution time:

| Variable | Value |
|----------|-------|
| `JOB_ID` | Job UUID |
| `TASK` | Task description |
| `REPO_URL` | Repository URL |
| `BASE_REF` | Base branch |

These are passed via `--append-system-prompt "Job: {id} | Task: {task} | …"`.
