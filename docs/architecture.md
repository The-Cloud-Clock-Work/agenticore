# Agenticore Architecture

## Overview

Agenticore is a thin orchestration layer for Claude Code. It manages job lifecycle,
repo cloning, profile-to-CLI-flag mapping, auto-PR creation, and OTEL pipeline setup.
Claude Code does the actual work (coding, worktree management, telemetry emission).

## Architecture

```
Request (MCP tool or REST API)
    → Router (code default, AI fallback)
    → Clone repo (if GitHub URL)
    → Run: claude --worktree -p "task" --model X --settings profile.json ...
    → OTEL telemetry → OTEL Collector → PostgreSQL
    → Auto-PR (on success: commit, push, gh pr create)
    → Job result stored in Redis (+ file fallback)
    → Query PostgreSQL for observability
```

## Key Principle

Agenticore is **thin orchestration**. Claude Code handles:
- Worktree management (`--worktree`)
- All coding work
- Telemetry emission (native OTEL)

Agenticore handles:
- Job lifecycle (queue, run, status, cancel)
- Repo cloning + caching
- Profile → CLI flags mapping
- Auto-PR creation
- OTEL collector → PostgreSQL pipeline

## Dual Interface

- **MCP tools** — AI clients connect directly
- **REST API** — Programmatic access, webhooks, CI/CD

### MCP Tools (5)

| Tool | Purpose |
|------|---------|
| `run_task` | Submit task: repo_url, task, profile, wait, session_id |
| `get_job` | Job status, output, artifacts, PR URL |
| `list_jobs` | Recent jobs with status |
| `cancel_job` | Cancel a running job |
| `list_profiles` | Available execution profiles |

### REST API

| Endpoint | Method | Maps to |
|----------|--------|---------|
| `POST /jobs` | POST | `run_task` |
| `GET /jobs/{id}` | GET | `get_job` |
| `GET /jobs` | GET | `list_jobs` |
| `DELETE /jobs/{id}` | DELETE | `cancel_job` |
| `GET /profiles` | GET | `list_profiles` |
| `GET /health` | GET | Health check |

## Execution Modes

| Mode | How | Use case |
|------|-----|----------|
| Fire-and-forget | Returns job_id immediately | Background work |
| Sync | `wait=true`, holds until done | Quick tasks |
| Stateful | `session_id` → `claude --resume` | Continue previous work |
| Stateless | Default, fresh session per job | One-shot tasks |

## Repo Management

```
{repos.root}/
└── {sha256(url)[:12]}/
    ├── .lock
    └── repo/
```

1. `ensure_clone(repo_url)` — Clone once, `git fetch --all` on re-use, flock-protected
2. `claude --worktree -p "task"` with `cwd=repo/` — Claude handles worktree creation
3. Claude finishes → agenticore runs auto-PR if configured

## Observability

Claude Code → OTEL Collector (:4317) → PostgreSQL → Grafana / Langfuse

## Docker Compose Stack

- agenticore — The runner server
- redis — Job store
- postgres — OTEL sink
- otel-collector — Receives Claude OTEL, writes to Postgres
