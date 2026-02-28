---
title: CLI Commands
nav_order: 1
---

# CLI Reference

Agenticore provides a CLI for server management, job submission, and status queries.
Job-related commands communicate with a running server via REST API.

```
agenticore <command> [options]
```

## Summary

| Command | Args | Key Flags | Server Required |
|---------|------|-----------|-----------------|
| `serve` | | `--port`, `--host` | No (starts it) |
| `run` | `<task>` | `--repo`, `--profile`, `--wait` | Yes |
| `jobs` | | `--limit`, `--status` | Yes |
| `job` | `<job_id>` | `--json` | Yes |
| `cancel` | `<job_id>` | | Yes |
| `profiles` | | | Yes |
| `status` | | | Yes |
| `init-shared-fs` | | `--shared-root` | No |
| `drain` | | `--timeout` | No |
| `update` | | `--source` | No |
| `version` | | | No |

## serve

Start the Agenticore server.

```bash
agenticore serve [--port PORT] [--host HOST]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--port` | int | 8200 | Server port |
| `--host` | str | 127.0.0.1 | Bind address |

Transport mode is controlled by `AGENTICORE_TRANSPORT` (default: `sse`).

```bash
agenticore serve
agenticore serve --port 9000 --host 0.0.0.0
```

## run

Submit a task for Claude Code execution.

```bash
agenticore run <task> [--repo URL] [--profile NAME] [--base-ref REF] [--wait] [--session-id ID]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--repo` | `-r` | str | (none) | GitHub repo URL to clone |
| `--profile` | `-p` | str | (auto) | Execution profile name |
| `--base-ref` | | str | `main` | Base branch for PR |
| `--wait` | `-w` | flag | false | Wait for job completion |
| `--session-id` | | str | (none) | Resume a Claude session |

```bash
# Fire-and-forget
agenticore run "fix the auth bug" --repo https://github.com/org/repo

# Wait for result
agenticore run "add unit tests" -r https://github.com/org/repo -w

# Use specific profile
agenticore run "review this PR" -r https://github.com/org/repo -p review

# Resume session
agenticore run "continue the refactor" --session-id abc123
```

## jobs

List recent jobs.

```bash
agenticore jobs [--limit N] [--status STATUS]
```

| Flag | Short | Type | Default | Description |
|------|-------|------|---------|-------------|
| `--limit` | `-n` | int | 20 | Max jobs to return |
| `--status` | `-s` | str | (all) | Filter: `queued`, `running`, `succeeded`, `failed`, `cancelled` |

```bash
agenticore jobs
agenticore jobs -s running -n 50
```

Output is a table with columns: `ID`, `STATUS`, `PROFILE`, `TASK`.

## job

Get details for a specific job.

```bash
agenticore job <job_id> [--json]
```

| Flag | Type | Description |
|------|------|-------------|
| `--json` | flag | Output raw JSON instead of formatted text |

```bash
agenticore job a1b2c3d4-...
agenticore job a1b2c3d4-... --json
```

Displays: ID, status, profile, task, repo URL, exit code, PR URL, timestamps,
error message, and output (truncated to 2000 chars).

## cancel

Cancel a running or queued job.

```bash
agenticore cancel <job_id>
```

Sends SIGTERM to the Claude subprocess if the job is running.

## profiles

List available execution profiles.

```bash
agenticore profiles
```

```
  code         Autonomous coding worker
               model=sonnet max_turns=80 auto_pr=True
  review       Code review analyst
               model=haiku max_turns=20 auto_pr=False
```

## status

Check server health.

```bash
agenticore status
# Status:  ok
# Service: agenticore
```

## init-shared-fs

Initialise the shared filesystem layout for Kubernetes deployments.

```bash
agenticore init-shared-fs [--shared-root PATH]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--shared-root` | str | `$AGENTICORE_SHARED_FS_ROOT` | Shared FS root path |

Creates the directory layout and copies bundled profiles to the shared volume:

```
/shared/
├── profiles/    ← bundled profiles copied here
├── repos/       ← git clone root
├── jobs/        ← per-job CLAUDE_CONFIG_DIR directories
└── job-state/   ← job JSON files (AGENTICORE_JOBS_DIR)
```

Typically run once as a Kubernetes init Job before the StatefulSet starts. See
[Kubernetes Deployment](../deployment/kubernetes.md) for the manifest.

## drain

Drain the pod before shutdown. Called by the Kubernetes PreStop hook.

```bash
agenticore drain [--timeout SECONDS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--timeout` | int | 300 | Max seconds to wait for running jobs |

Steps:
1. Marks this pod as draining in Redis (`agenticore:pod:{pod_name}:draining`)
2. Polls until all jobs with `pod_name == this pod` are no longer `running`
3. Removes the draining flag
4. Exits (Kubernetes then terminates the container)

The StatefulSet configures `terminationGracePeriodSeconds: 300` to give this time to finish.

## update

Self-update Agenticore to the latest version.

```bash
agenticore update [--source SOURCE]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--source` | str | `agenticore` | Install source (PyPI, git URL, or local path) |

```bash
agenticore update
agenticore update --source git+https://github.com/The-Cloud-Clock-Work/agenticore.git
agenticore update --source /path/to/agenticore
```

## version

Show the installed version.

```bash
agenticore version
# agenticore 0.1.5
```

## Client Configuration

The CLI connects to the server using these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_HOST` | `127.0.0.1` | Server host |
| `AGENTICORE_PORT` | `8200` | Server port |

```bash
AGENTICORE_HOST=10.0.0.5 AGENTICORE_PORT=9000 agenticore jobs
```
