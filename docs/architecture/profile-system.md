# Profile System

Profiles are directory-based packages that contain native Claude Code config files
(`.claude/`, `.mcp.json`) plus a `profile.yml` for Agenticore metadata. They
specify the model, turn limits, permissions, and auto-PR behavior. The
repo's own `CLAUDE.md` stays untouched; profiles add behavior on top via
`--append-system-prompt` and the `.claude/` package materialized into the working directory.

## Profile YAML Schema

| Field | Type | Default | CLI Flag | Description |
|-------|------|---------|----------|-------------|
| `name` | string | dir name | | Profile identifier |
| `description` | string | `""` | | Human-readable description |
| `claude.model` | string | `sonnet` | `--model` | Claude model |
| `claude.max_turns` | int | `80` | `--max-turns` | Max agentic turns |
| `claude.output_format` | string | `json` | `--output-format` | Output format |
| `claude.permission_mode` | string | `bypassPermissions` | `--permission-mode` | Permission mode |
| `claude.no_session_persistence` | bool | `true` | `--no-session-persistence` | Disable session persistence |
| `claude.timeout` | int | `3600` | (process timeout) | Max seconds |
| `claude.worktree` | bool | `true` | `--worktree` | Use worktree isolation |
| `claude.effort` | string/null | `null` | `--effort` | Effort level (e.g. `high`) |
| `claude.max_budget_usd` | float/null | `null` | `--max-budget-usd` | Cost ceiling in USD |
| `claude.fallback_model` | string/null | `null` | `--fallback-model` | Fallback model on primary failure |
| `extends` | string/null | `null` | | Inherit from another profile |
| `auto_pr` | bool | `true` | | Create PR on success |
| `append_prompt` | string | `""` | `--append-system-prompt` | Additional system prompt (legacy profiles only) |

## Bundled Profiles

### code

Autonomous coding worker. Default profile for most tasks.

```yaml
name: code
description: "Autonomous coding worker"

claude:
  model: sonnet
  max_turns: 80
  permission_mode: bypassPermissions
  no_session_persistence: true
  output_format: json
  worktree: true
  effort: high
  timeout: 3600

auto_pr: true
```

### review

Code review analyst. Read-only, uses a faster model.

```yaml
name: review
description: "Code review analyst"

claude:
  model: haiku
  max_turns: 20
  permission_mode: bypassPermissions
  no_session_persistence: true
  output_format: json
  worktree: true
  timeout: 1800

auto_pr: false
```

## Custom Profiles

Place custom profile directories in `~/.agenticore/profiles/`. Each profile is a
directory containing a `profile.yml` (and optionally `.claude/`, `.mcp.json`).
User profiles override bundled defaults with the same name.

```
defaults/profiles/          <-- bundled (shipped with package)
    code/
        profile.yml
        .claude/
            CLAUDE.md
    review/
        profile.yml
        .claude/
            CLAUDE.md

~/.agenticore/profiles/     <-- user overrides
    code/                   <-- overrides bundled code/
        profile.yml
    deploy/                 <-- new custom profile
        profile.yml
```

Loading order: bundled defaults first, then user profiles. Same-name user
profiles replace the bundled version entirely.

## Template Variables

The `append_prompt` field supports template variables that are rendered at
job execution time:

| Variable | Value | Source |
|----------|-------|--------|
| `{{TASK}}` | Task description | `job.task` |
| `{{REPO_URL}}` | Repository URL | `job.repo_url` |
| `{{BASE_REF}}` | Base branch | `job.base_ref` |
| `{{JOB_ID}}` | Job UUID | `job.id` |
| `{{PROFILE}}` | Profile name | `job.profile` |

Templates use simple string replacement (`{{KEY}}` to value).

## Profile to CLI Args

```
+------------------+
|  Profile dir     |
|  code/           |
|  profile.yml     |
+--------+---------+
         |
         v
+--------+---------+
|  build_cli_args  |
|  (profiles.py)   |
+--------+---------+
         |
         v
+--------+------------------------------------------+
|  claude --worktree                                |
|         --model sonnet                            |
|         --max-turns 80                            |
|         --output-format json                      |
|         --permission-mode bypassPermissions       |
|         --no-session-persistence                  |
|         --effort high                             |
|         --append-system-prompt "Job: ... | ..."   |
|         -p "fix the auth bug"                     |
+---------------------------------------------------+
```

The `build_cli_args()` function in `profiles.py` converts a Profile dataclass
into a list of CLI arguments. The task prompt is always appended last with `-p`.

## Profile Resolution

```
Request arrives with profile=""
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
exists?       (config.claude.default_profile)
   |             |
   +------+------+
          |
          v
   resolved profile name
```

If an explicit profile is requested but doesn't exist, the router falls back to
the configured default profile (usually `code`).

See [Job Execution](job-execution.md) for how profiles are used during the
runner pipeline.
