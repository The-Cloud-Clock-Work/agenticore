# Agenticore

Claude Code runner and orchestrator. Manages job lifecycle, repo cloning/caching, profile-based execution, auto-PR creation, and OTEL observability pipeline.

![Agenticore Architecture](docs/media/agenticore-readme-banner.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/blob/main/LICENSE)
[![Tests](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml)
[![Docker](https://img.shields.io/docker/v/tccw/agenticore?label=Docker%20Hub)](https://hub.docker.com/r/tccw/agenticore)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)

## How It Works

```
Request (MCP or REST)
  │
  ├─ Router selects profile
  ├─ Clone/fetch repo (flock-protected cache)
  ├─ Spawn: claude --worktree -p "task" (with profile flags + OTEL env)
  ├─ Collect telemetry → OTEL Collector → Langfuse + PostgreSQL
  ├─ Ship Claude session transcript → Langfuse (via SDK)
  ├─ Auto-PR on success (when profile enables it)
  └─ Job result → Redis (or file fallback)
```

1. **Takes a task** via MCP tool or REST API
2. **Clones the repo** into a cached directory (flock-serialized, reuses on subsequent runs)
3. **Runs Claude Code** as a subprocess with profile-derived CLI flags and OTEL environment
4. **Streams telemetry** through Claude's native OTEL support to a collector (exported to Langfuse and/or PostgreSQL)
5. **Ships transcripts** from the Claude session to Langfuse as spans for full visibility
6. **Creates a PR** automatically if the profile has `auto_pr: true` and the job succeeds

## Quick Start

### Local (no Docker)

```bash
pip install -e .

# Start server with SSE transport (HTTP server on :8200)
AGENTICORE_TRANSPORT=sse agenticore serve

# Or stdio transport (for use as an MCP server in Claude Code)
python -m agenticore
```

### Docker Compose (local development)

The compose stack spins up agenticore alongside local Redis, PostgreSQL, and an OTEL Collector so you can test the full pipeline without any external infrastructure.

```bash
cp .env.example .env
# Edit .env — at minimum set GITHUB_TOKEN if you want auto-PR

docker compose up --build -d
```

This starts:

| Service | Purpose | Port |
|---------|---------|------|
| **agenticore** | The runner server | 8200 |
| **redis** | Job store | 6379 |
| **postgres** | OTEL telemetry sink | 5432 |
| **otel-collector** | Receives OTEL from Claude, exports to Langfuse + Postgres | 4317 (gRPC), 4318 (HTTP) |

All services are wired together automatically — agenticore points at `redis://redis:6379/0` and the OTEL collector at `http://otel-collector:4317`.

### Production Deployment

In production you run **only the agenticore container**. Redis, PostgreSQL, and the OTEL Collector are external managed services (e.g., AWS ElastiCache, RDS, a hosted OTEL backend).

```bash
docker build -t agenticore .

docker run -d \
  -p 8200:8200 \
  -e AGENTICORE_TRANSPORT=sse \
  -e AGENTICORE_HOST=0.0.0.0 \
  -e AGENTICORE_PORT=8200 \
  -e REDIS_URL=redis://your-redis-host:6379/0 \
  -e AGENTICORE_OTEL_ENABLED=true \
  -e OTEL_EXPORTER_OTLP_ENDPOINT=https://your-otel-collector:4317 \
  -e OTEL_EXPORTER_OTLP_PROTOCOL=grpc \
  -e POSTGRES_URL=postgresql://user:pass@your-postgres:5432/agenticore \
  -e GITHUB_TOKEN=ghp_... \
  agenticore
```

The key difference from local dev:

| Concern | Local (docker-compose) | Production |
|---------|----------------------|------------|
| Redis | Local container (`redis:7-alpine`) | External managed service |
| PostgreSQL | Local container (`postgres:16-alpine`) | External managed service |
| OTEL Collector | Local container (`otel-collector-contrib`) | External hosted collector |
| Agenticore | Built from source | Same image, external config |
| Config | Hardcoded service names | Env vars pointing to real endpoints |

If Redis is unavailable, agenticore falls back to file-based job storage at `~/.agenticore/jobs/`. This means you can run without Redis entirely for simple setups.

## Configuration

All configuration is loaded from `~/.agenticore/config.yml` with environment variable overrides. Env vars always take priority.

### Environment Variables

#### Server

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_TRANSPORT` | `stdio` | `sse` for HTTP server, `stdio` for MCP pipe |
| `AGENTICORE_HOST` | `127.0.0.1` | Bind address (`0.0.0.0` in Docker) |
| `AGENTICORE_PORT` | `8200` | Server port |
| `AGENTICORE_API_KEYS` | _(empty)_ | Comma-separated API keys for auth (optional) |

#### Redis

| Variable | Default | Description |
|----------|---------|-------------|
| `REDIS_URL` | _(empty)_ | Redis connection URL. Empty = file fallback |
| `REDIS_KEY_PREFIX` | `agenticore` | Key namespace prefix (keys: `{prefix}:job:{id}`) |

#### Claude Code

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_CLAUDE_BINARY` | `claude` | Path to Claude Code binary |
| `AGENTICORE_CLAUDE_TIMEOUT` | `3600` | Max job runtime in seconds |
| `AGENTICORE_DEFAULT_PROFILE` | `code` | Profile used when none specified |
| `AGENTICORE_CLAUDE_CONFIG_DIR` | _(empty)_ | Custom Claude config directory |

#### Repos

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_REPOS_ROOT` | `~/agenticore-repos` | Clone cache directory |
| `AGENTICORE_MAX_PARALLEL_JOBS` | `3` | Max concurrent jobs |
| `AGENTICORE_JOB_TTL` | `86400` | Job expiry in seconds |

#### OTEL

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_OTEL_ENABLED` | `true` | Enable/disable telemetry |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | OTLP protocol (`grpc` or `http`) |
| `AGENTICORE_OTEL_LOG_PROMPTS` | `false` | Log user prompts to telemetry |
| `AGENTICORE_OTEL_LOG_TOOL_DETAILS` | `true` | Log tool execution to telemetry |

#### GitHub

| Variable | Default | Description |
|----------|---------|-------------|
| `GITHUB_TOKEN` | _(empty)_ | GitHub token for auto-PR (uses `gh` CLI) |

#### Langfuse

| Variable | Default | Description |
|----------|---------|-------------|
| `LANGFUSE_HOST` | `https://cloud.langfuse.com` | Langfuse API host |
| `LANGFUSE_PUBLIC_KEY` | _(empty)_ | Langfuse public key (enables SDK tracing) |
| `LANGFUSE_SECRET_KEY` | _(empty)_ | Langfuse secret key |
| `LANGFUSE_BASIC_AUTH` | _(empty)_ | Base64(`public_key:secret_key`) for OTEL collector |

#### Agentihooks

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_AGENTIHOOKS_PATH` | _(empty)_ | Path to cloned agentihooks repo (e.g. `/app`) |

#### PostgreSQL

| Variable | Default | Description |
|----------|---------|-------------|
| `POSTGRES_URL` | _(empty)_ | PostgreSQL connection URL (OTEL sink) |

## MCP Tools

Connect agenticore as an MCP server in Claude Code or any MCP client. All task submissions are async (fire and forget) by default — pass `wait=true` to block until completion.

| Tool | Description |
|------|-------------|
| `run_task` | Submit a task (async by default, `wait=true` to block) |
| `get_job` | Get job status, output, artifacts, and PR URL |
| `list_jobs` | List recent jobs with status |
| `cancel_job` | Cancel a running job |
| `list_profiles` | List available execution profiles |

## REST API

When running with `AGENTICORE_TRANSPORT=sse`, the same operations are available over HTTP. Same semantics: async by default, `"wait": true` to block.

```bash
# Submit a job (async — returns job ID immediately)
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task": "fix the auth bug", "repo_url": "https://github.com/org/repo"}'

# Submit and wait for completion
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task": "fix the auth bug", "repo_url": "https://github.com/org/repo", "wait": true}'

# Check status
curl http://localhost:8200/jobs/{job_id}

# List jobs
curl http://localhost:8200/jobs

# Cancel
curl -X DELETE http://localhost:8200/jobs/{job_id}

# List profiles
curl http://localhost:8200/profiles

# Health check
curl http://localhost:8200/health
```

If `AGENTICORE_API_KEYS` is set, include `X-API-Key: <key>` header or `?api_key=<key>` query param.

## Profiles

Profiles are directory-based packages that define how Claude Code runs. Each profile is a directory containing a `profile.yml` for agenticore-specific metadata, plus native Claude Code config files (`.claude/` directory and `.mcp.json`).

**Default profiles** are bundled in `defaults/profiles/`. **External profiles** can be loaded from an agentihooks repo via `AGENTICORE_AGENTIHOOKS_PATH`. **Custom profiles** go in `~/.agenticore/profiles/` and override everything with the same name.

Profile priority (highest wins): user (`~/.agenticore/profiles/`) > agentihooks > defaults.

### Directory Structure

```
profiles/
  code/
    profile.yml              # Agenticore metadata
    .claude/
      CLAUDE.md              # Profile-specific instructions
      settings.json          # Tool permissions
    .mcp.json                # MCP server config (optional)
  review/
    profile.yml
    .claude/
      CLAUDE.md
      settings.json
    .mcp.json
```

### `code` (default)

Autonomous coding worker. Writes code, commits, and triggers auto-PR.

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

### `review`

Read-only code reviewer. Analyzes code without modifying files.

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

### Profile Fields

| Field | Description |
|-------|-------------|
| `claude.model` | Claude model to use (`sonnet`, `haiku`, `opus`) |
| `claude.max_turns` | Maximum agentic turns |
| `claude.output_format` | Output format (`json`, `text`, `stream-json`) |
| `claude.permission_mode` | Permission mode for tool use |
| `claude.no_session_persistence` | Disable session persistence (default: true) |
| `claude.effort` | Effort level (e.g. `high`) |
| `claude.timeout` | Job timeout in seconds |
| `claude.worktree` | Use git worktree (isolates from main branch) |
| `claude.max_budget_usd` | Optional max budget in USD |
| `claude.fallback_model` | Optional fallback model |
| `auto_pr` | Create PR on successful completion |
| `extends` | Inherit from another profile |

## Architecture

### Modules

| Module | Purpose |
|--------|---------|
| `server.py` | FastMCP server (5 tools) + REST routes + SSE |
| `config.py` | YAML config loader with env var overrides |
| `profiles.py` | Load profile packages into CLI flags |
| `repos.py` | Git clone/fetch with flock serialization |
| `jobs.py` | Job store — Redis hash or JSON file fallback |
| `runner.py` | Spawn Claude subprocess with OTEL env vars |
| `telemetry.py` | Langfuse trace lifecycle + transcript shipping |
| `router.py` | Profile selection (explicit or default) |
| `pr.py` | Auto-PR via `git push` + `gh pr create` |
| `cli.py` | CLI client (`agenticore run`, `agenticore serve`, etc.) |

### Container

The Dockerfile builds a Python 3.12 image with:
- **git** and **curl** for repo operations
- **gh CLI** for auto-PR creation
- **Claude Code** must be available at the path specified by `AGENTICORE_CLAUDE_BINARY`

The container exposes port 8200 and runs in SSE transport mode by default.

### OTEL Pipeline

```
Claude Code (subprocess)
  │ OTLP gRPC/HTTP
  ▼
OTEL Collector
  │ batch processor
  ├──► Langfuse (otlphttp/langfuse)
  └──► PostgreSQL / debug
```

Claude Code has native OTEL support. Agenticore sets the OTEL env vars on the subprocess so telemetry flows to the OTEL Collector, which exports to both Langfuse (via OTLP HTTP) and PostgreSQL/debug. Additionally, the runner ships Claude session transcripts directly to Langfuse via the SDK as spans on the job trace.

Set `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` to enable SDK-level tracing. Set `LANGFUSE_BASIC_AUTH` for the OTEL collector export path.

## CLI

```bash
agenticore run "fix the bug" \  # Submit task (async, returns job ID)
  --repo https://github.com/org/repo \
  --profile code
agenticore run "add tests" --wait  # Submit and wait for completion
agenticore jobs                 # List recent jobs
agenticore job <id>             # Get job details
agenticore cancel <id>          # Cancel a running job
agenticore profiles             # List available profiles
agenticore serve                # Start the server (SSE mode)
agenticore status               # Server health check
agenticore version              # Print version
```

## Development

```bash
pip install -e ".[dev]"

# Tests
pytest tests/unit -v -m unit --cov=agenticore

# Lint
ruff check agenticore/ tests/
ruff format --check agenticore/ tests/
```

## Code Quality

This project is continuously analyzed by [SonarQube](https://sonar.homeofanton.com/dashboard?id=agenticore) for code quality, security vulnerabilities, and test coverage.
