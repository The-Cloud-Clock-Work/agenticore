---
title: Profile System
nav_order: 3
---

# Profile System

Profiles are **directory packages** that configure Claude Code execution. Each
profile is a directory containing a `profile.yml` for Agenticore metadata and a
`.claude/` directory with native Claude Code configuration files.

Agenticore does **not** bundle any profiles. Profiles come from two external
sources: your [agentihooks](https://github.com/The-Cloud-Clock-Work/agentihooks)
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
prompts, and the `build_profiles.py` generator. Set `AGENTICORE_AGENTIHOOKS_PATH`
to the path of your cloned agentihooks repo.

**User profiles** (`~/.agenticore/profiles/`) are for personal overrides and
local experimentation. They always take highest priority.

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

With `.claude/settings.json` granting the permissions Claude needs:

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

Before each job, `materialize_profile()` copies the profile's `.claude/` and
`.mcp.json` into the job's target directory so Claude Code picks them up
natively.

### Local / Docker mode (default)

Files are copied directly into the repo clone directory:

```
{repo-cwd}/
├── .claude/              ← copied from profile
│   ├── settings.json
│   └── CLAUDE.md
└── .mcp.json             ← merged with any existing repo .mcp.json
```

### Kubernetes / shared FS mode

When `AGENTICORE_SHARED_FS_ROOT` is set, files are written to a per-job
directory on the shared volume, keeping the repo tree clean:

```
/shared/jobs/{job-id}/
├── .claude/
│   ├── settings.json
│   └── CLAUDE.md
└── .mcp.json
```

The runner sets `CLAUDE_CONFIG_DIR=/shared/jobs/{job-id}` in the Claude
subprocess environment so Claude reads config from there. The `job_config_dir`
field is stored on the job record for auditing.

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
exists?       (claude.default_profile)
   │             │
   └──────┬──────┘
          │
          ▼
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

These are passed via `--append-system-prompt "Job: {id} | Task: {task} | …"`.
