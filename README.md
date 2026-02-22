# Agenticore

Claude Code runner and orchestrator. Thin job lifecycle management with repo cloning, profile-based execution, auto-PR creation, and OTEL observability pipeline.

## Quick Start

```bash
# Install
pip install -e .

# Run with SSE transport
AGENTICORE_TRANSPORT=sse python -m agenticore

# Or use Docker
docker compose up --build -d
```

## What It Does

1. **Takes a task** — via MCP tool or REST API
2. **Clones the repo** — cached, flock-protected
3. **Runs Claude Code** — `claude --worktree -p "task"` with profile-derived flags
4. **Collects telemetry** — Claude's native OTEL → OTEL Collector → PostgreSQL
5. **Creates PR** — auto-PR on success (when enabled in profile)

## MCP Tools

| Tool | Purpose |
|------|---------|
| `run_task` | Submit task: repo_url, task, profile, wait, session_id |
| `get_job` | Job status, output, artifacts, PR URL |
| `list_jobs` | Recent jobs with status |
| `cancel_job` | Cancel a running job |
| `list_profiles` | Available execution profiles |

## REST API

```bash
# Submit a job
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task": "fix the auth bug", "repo_url": "https://github.com/org/repo", "profile": "code"}'

# Check status
curl http://localhost:8200/jobs/{job_id}

# List jobs
curl http://localhost:8200/jobs

# Cancel
curl -X DELETE http://localhost:8200/jobs/{job_id}

# List profiles
curl http://localhost:8200/profiles

# Health
curl http://localhost:8200/health
```

## Profiles

Profiles map to Claude Code CLI flags. Stored as YAML in `~/.agenticore/profiles/`.

```yaml
name: code
description: "Autonomous coding worker"
claude:
  model: sonnet
  max_turns: 80
  worktree: true
  permission_mode: dangerously-skip-permissions
auto_pr: true
```

## Docker Compose Stack

- **agenticore** — The runner server
- **redis** — Job store
- **postgres** — OTEL sink (queryable)
- **otel-collector** — Claude OTEL → PostgreSQL

## Kubernetes (Helm)

```bash
helm install agenticore ./helm/agenticore \
  --set redis.url=redis://redis:6379/0 \
  --set github.token=$GITHUB_TOKEN
```

## Development

```bash
pip install -e ".[dev]"
pytest tests/unit -v -m unit --cov=agenticore
ruff check agenticore/ tests/
```
# agenticore
