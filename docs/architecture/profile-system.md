# Profile System

Profiles map to Claude Code CLI flags. Each profile is a YAML file that specifies
the model, turn limits, permissions, prompt additions, and auto-PR behavior. The
repo's own `CLAUDE.md` stays untouched; profiles add behavior on top via
`--append-system-prompt` and `--settings`.

## Profile YAML Schema

| Field | Type | Default | CLI Flag | Description |
|-------|------|---------|----------|-------------|
| `name` | string | file stem | | Profile identifier |
| `description` | string | `""` | | Human-readable description |
| `claude.model` | string | `sonnet` | `--model` | Claude model |
| `claude.max_turns` | int | `80` | `--max-turns` | Max agentic turns |
| `claude.output_format` | string | `json` | `--output-format` | Output format |
| `claude.permission_mode` | string | `dangerously-skip-permissions` | `--dangerously-skip-permissions` | Permission mode |
| `claude.timeout` | int | `3600` | (process timeout) | Max seconds |
| `claude.worktree` | bool | `true` | `--worktree` | Use worktree isolation |
| `append_prompt` | string | `""` | `--append-system-prompt` | Additional system prompt (supports templates) |
| `settings.permissions` | object | `{}` | `--settings` | Permission allowlist |
| `claude_md` | string/null | `null` | | Custom CLAUDE.md content (reserved) |
| `auto_pr` | bool | `true` | | Create PR on success |

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
  permission_mode: dangerously-skip-permissions
  timeout: 3600
  worktree: true

append_prompt: |
  ## Job Context
  Task: {{TASK}}
  Repository: {{REPO_URL}}
  Base branch: {{BASE_REF}}

  ## Guidelines
  - Commit with descriptive messages
  - Do NOT create PRs — the system handles that

settings:
  permissions:
    allow:
      - "Bash(*)"
      - "Read(*)"
      - "Write(*)"
      - "Edit(*)"

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
  output_format: json
  permission_mode: dangerously-skip-permissions
  worktree: true

append_prompt: |
  ## Task
  {{TASK}}

  ## Guidelines
  - Do NOT modify files
  - Provide structured feedback

auto_pr: false
```

## Custom Profiles

Profiles are loaded from up to three directories, in priority order (highest wins):

```
defaults/profiles/                        <-- bundled (shipped with package)
    code/profile.yml
    review/profile.yml

{AGENTICORE_AGENTIHOOKS_PATH}/profiles/   <-- external (agentihooks repo)
    default/profile.yml                   <-- loaded only if env var is set
    review/profile.yml

~/.agenticore/profiles/                   <-- user overrides (always checked)
    code/profile.yml                      <-- overrides bundled code
    deploy/profile.yml                    <-- new custom profile
```

Loading order: bundled defaults first, then agentihooks (if `AGENTICORE_AGENTIHOOKS_PATH`
is set), then user profiles. Same-name profiles from a higher-priority directory
replace the lower-priority version entirely.

### Agentihooks Integration

Set `AGENTICORE_AGENTIHOOKS_PATH` to the root of a cloned agentihooks repo (e.g. `/app`).
Agenticore will load profiles from `{path}/profiles/`. These profiles can include
pre-wired hook events in their `settings.json` — agenticore materializes `settings.json`
into the working directory as-is, and Claude Code picks up the hooks automatically.
No hook-specific logic exists in agenticore.

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
|  Profile YAML    |
|  code.yml        |
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
|         --dangerously-skip-permissions            |
|         --append-system-prompt "## Job Context..."|
|         --settings '{"permissions":{...}}'        |
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
