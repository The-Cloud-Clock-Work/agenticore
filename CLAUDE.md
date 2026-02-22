# CLAUDE.md

## Project Overview

**Agenticore** is a Claude Code runner and orchestrator. It manages job lifecycle,
repo cloning/caching, profile-to-CLI-flag mapping, auto-PR creation, and OTEL pipeline.
Claude Code does the heavy lifting (coding, worktrees, telemetry).

## Build & Development

```bash
# Install
pip install -e .

# Start server (SSE transport)
AGENTICORE_TRANSPORT=sse agenticore serve

# Start server (stdio — for Claude Code CLI)
python -m agenticore

# Docker (full stack)
docker compose up --build -d

# Tests
pytest tests/unit -v -m unit --cov=agenticore

# Lint
ruff check agenticore/ tests/
ruff format --check agenticore/ tests/

# CLI
agenticore version
agenticore status
```

## Architecture

```
Request → Router → Clone repo → claude --worktree -p "task" → OTEL → PostgreSQL
                                                             → Auto-PR
                                                             → Job result → Redis
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | FastMCP server (5 tools) + REST routes |
| `config.py` | YAML config loader + env var overrides |
| `profiles.py` | Load profile packages → CLI flags |
| `repos.py` | Git clone/fetch with flock |
| `jobs.py` | Job store (Redis + file fallback) |
| `runner.py` | Spawn Claude subprocess with OTEL env |
| `router.py` | Code fast-path + AI fallback |
| `pr.py` | Auto-PR (git push + gh pr create) |
| `cli.py` | CLI tool |

## MCP Tools

- `run_task` — Submit task with repo_url, task, profile
- `get_job` — Job status, output, PR URL
- `list_jobs` — Recent jobs
- `cancel_job` — Cancel running job
- `list_profiles` — Available profiles

## Profile System

Profiles are directory-based packages with `profile.yml` + `.claude/` config.
Default profiles bundled in `defaults/profiles/`. Custom profiles in `~/.agenticore/profiles/`.

## Redis + File Fallback

Jobs stored as Redis hashes (`agenticore:job:{id}`) or `~/.agenticore/jobs/{id}.json`.
