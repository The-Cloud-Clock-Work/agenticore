---
title: Profile System
nav_order: 3
---

# Profile System

Profiles are directory packages that configure Claude Code execution. Each profile
is a directory containing a `profile.yml` for Agenticore metadata and a `.claude/`
directory with native Claude Code config files.

## Profile Directory Layout

```
defaults/profiles/code/
├── profile.yml          # Agenticore metadata (model, turns, auto_pr, etc.)
└── .claude/
    ├── settings.json    # Hooks, permissions, env vars
    ├── CLAUDE.md        # System instructions for Claude
    ├── agents/          # Custom subagents
    └── skills/          # Custom skills
```

An optional `.mcp.json` at the profile root adds MCP servers to the working directory.

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

## Bundled Profiles

### code

Autonomous coding worker. Default profile for most tasks.

```yaml
name: code
description: "Autonomous coding worker"

claude:
  model: sonnet
  max_turns: 80
  output_format: json
  permission_mode: bypassPermissions
  timeout: 3600
  worktree: true

auto_pr: true
```

The `.claude/settings.json` in this profile configures tool permissions and hooks.
The `.claude/CLAUDE.md` provides task-execution guidelines to Claude.

### review

Code review analyst. Read-only mode, uses a faster model.

```yaml
name: review
description: "Code review analyst"

claude:
  model: haiku
  max_turns: 20
  output_format: json
  permission_mode: bypassPermissions
  worktree: true

auto_pr: false
```

## Custom Profiles

Profiles are loaded from up to three directories, in priority order (highest wins):

```
defaults/profiles/                        ← bundled (shipped with package)

{AGENTICORE_AGENTIHOOKS_PATH}/profiles/   ← external (set via env var)

~/.agenticore/profiles/                   ← user overrides (always checked)
```

Same-name profiles from a higher-priority directory replace the lower-priority
version entirely.

## Profile Inheritance

A profile can extend another profile using the `extends` field:

```yaml
name: code-strict
extends: code

claude:
  max_turns: 20
  effort: high
```

Child values override parent defaults. The `.claude/` files are merged
(child overlays parent) during materialization.

## Materialization

Before each job, `materialize_profile()` copies the profile's `.claude/` and
`.mcp.json` into the job's working directory so Claude Code picks them up.

### Local / Docker mode (default)

Files are copied directly into the repo clone directory:

```
{repo-cwd}/
├── .claude/              ← copied from profile
│   ├── settings.json
│   └── CLAUDE.md
└── .mcp.json             ← merged with any existing .mcp.json
```

### Kubernetes / shared FS mode

When `AGENTICORE_SHARED_FS_ROOT` is set, files are written to a per-job directory
on the shared volume instead, keeping the repo tree clean:

```
/shared/jobs/{job-id}/
├── .claude/
│   ├── settings.json
│   └── CLAUDE.md
└── .mcp.json
```

The runner sets `CLAUDE_CONFIG_DIR=/shared/jobs/{job-id}` in the Claude subprocess
environment so Claude reads config from there. The `job_config_dir` field is stored
on the job record for auditing.

## Profile to CLI Args

```
profile.yml (claude section)
       |
       v
build_cli_args()
       |
       v
claude --worktree
       --model sonnet
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
         |
         v
+--------+---------+
|   router.py      |
|   route()        |
+--------+---------+
         |
   +-----+-----+
   |             |
   v             v
profile       no profile
specified?    specified
   |             |
   v             v
validate      use default
exists?       (claude.default_profile)
   |             |
   +------+------+
          |
          v
   resolved profile name
```

If the requested profile doesn't exist, the router falls back to
`claude.default_profile` (default: `code`).

## Template Variables

The `--append-system-prompt` flag receives dynamic context built from the
job at execution time:

| Variable | Value |
|----------|-------|
| `JOB_ID` | Job UUID |
| `TASK` | Task description |
| `REPO_URL` | Repository URL |
| `BASE_REF` | Base branch |

These are passed via `--append-system-prompt "Job: {id} | Task: {task} | ..."`.
