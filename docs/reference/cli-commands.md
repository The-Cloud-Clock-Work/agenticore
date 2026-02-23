---
title: CLI Commands
nav_order: 1
---

# CLI Reference

Agenticore provides a CLI for server management, job submission, and status queries.
All job-related commands communicate with a running server via REST API.

```
agenticore <command> [options]
```

## Summary

| Command | Args | Key Flags | Server Required |
|---------|------|-----------|-----------------|
| `run` | | `--port`, `--host` | No (starts it) |
| `submit` | `<task>` | `--repo`, `--profile`, `--wait` | Yes |
| `jobs` | | `--limit`, `--status` | Yes |
| `job` | `<job_id>` | `--json` | Yes |
| `cancel` | `<job_id>` | | Yes |
| `profiles` | | | Yes |
| `status` | | | Yes |
| `update` | | `--source` | No |
| `version` | | | No |

## run

Start the Agenticore server.

```bash
agenticore run [--port PORT] [--host HOST]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--port` | int | 8200 | Server port |
| `--host` | str | 127.0.0.1 | Bind address |

The transport mode is controlled by `AGENTICORE_TRANSPORT` (default: `sse`).

```bash
# Start on default port
agenticore run

# Start on custom port
agenticore run --port 9000 --host 0.0.0.0
```

## submit

Submit a task for Claude Code execution.

```bash
agenticore submit <task> [--repo URL] [--profile NAME] [--base-ref REF] [--wait] [--session-id ID]
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
agenticore submit "fix the auth bug" --repo https://github.com/org/repo

# Wait for result
agenticore submit "add unit tests" -r https://github.com/org/repo -w

# Use specific profile
agenticore submit "review this PR" -r https://github.com/org/repo -p review

# Resume session
agenticore submit "continue the refactor" --session-id abc123
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
# List all recent jobs
agenticore jobs

# Only running jobs
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
# Human-readable output
agenticore job a1b2c3d4-...

# JSON output (for scripting)
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

```bash
agenticore cancel a1b2c3d4-...
```

## profiles

List available execution profiles.

```bash
agenticore profiles
```

Displays each profile with: name, description, model, max_turns, and auto_pr setting.

```bash
agenticore profiles
#   code         Autonomous coding worker
#                model=sonnet max_turns=80 auto_pr=True
#   review       Code review analyst
#                model=haiku max_turns=20 auto_pr=False
```

## status

Check server health.

```bash
agenticore status
```

Queries `GET /health` and shows the response.

```bash
agenticore status
# Status:  ok
# Service: agenticore
```

## update

Self-update Agenticore to the latest version.

```bash
agenticore update [--source SOURCE]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--source` | str | `agenticore` | Install source (PyPI package, git URL, or local path) |

```bash
# Update from PyPI
agenticore update

# Update from git
agenticore update --source git+https://github.com/The-Cloud-Clock-Work/agenticore.git

# Update from local path
agenticore update --source /path/to/agenticore
```

## version

Show the installed version.

```bash
agenticore version
# agenticore 0.1.0
```

## Client Configuration

The CLI connects to the server using these environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_HOST` | `127.0.0.1` | Server host |
| `AGENTICORE_PORT` | `8200` | Server port |

```bash
# Connect to a remote server
AGENTICORE_HOST=10.0.0.5 AGENTICORE_PORT=9000 agenticore jobs
```
