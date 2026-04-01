# Agenticore

Production-grade Claude Code runner and orchestrator. Submit a task, get a PR.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/blob/main/LICENSE)
[![Tests](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml)
[![Docker](https://img.shields.io/docker/v/tccw/agenticore?label=Docker%20Hub)](https://hub.docker.com/r/tccw/agenticore)
[![Helm](https://img.shields.io/badge/Helm-GHCR-blue)](https://ghcr.io/the-cloud-clock-work/charts/agenticore)
[![PyPI](https://img.shields.io/pypi/v/agenticore)](https://pypi.org/project/agenticore/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)

```
MCP Client / REST Client / CLI
            │
            ▼
    ┌── Agenticore ──────────────────────────────────────────────┐
    │   Auth · Router · Job Queue                                │
    │                                                            │
    │   Clone repo ──► Bespoke worktree ──► claude -p "task"  │
    │   (cached)       (locked branch)    (cwd = worktree)     │
    │                                         │                  │
    │                                         ▼                  │
    │                                   Auto-PR (gh)             │
    │                                   Job result → Redis       │
    └──────────────────────┬─────────────────────────────────────┘
                           │
                    OTEL Collector
                    → Langfuse / PostgreSQL
```

---

Agenticore is a **thin orchestration layer** on top of Claude Code:

- Accepts tasks from **MCP clients, REST, or CLI** — same API surface, one port
- **Clones and caches repos**, serializes concurrent access with distributed locks
- Creates **bespoke worktrees** — locked before Claude starts, deterministic branch names
- **Applies execution profiles** — installed into `~/.claude/` at startup via agentihooks
- Spawns `claude -p "<task>"` in the worktree and **opens a PR when it succeeds**
- Ships **full OTEL traces** (prompts, tool calls, token counts) to Langfuse / PostgreSQL
- Runs standalone, in Docker, or on **Kubernetes** (Helm chart, KEDA autoscaling, graceful drain)

---

## Install

```bash
pip install agenticore
```

Or from source:

```bash
git clone https://github.com/The-Cloud-Clock-Work/agenticore.git
cd agenticore
pip install -e .
```

---

## Quickstart

```bash
# 1. Set your credentials
export ANTHROPIC_AUTH_TOKEN=sk-ant-...
export GITHUB_TOKEN=ghp_...

# 2. Start the server
agenticore serve

# 3. Submit a task
agenticore run "fix the null pointer in auth.py" \
  --repo https://github.com/org/repo \
  --wait
```

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `agenticore run "<task>" --repo <url>` | Submit a task (returns job ID immediately) |
| `agenticore run "<task>" --repo <url> --wait` | Submit and wait for completion |
| `agenticore jobs` | List recent jobs |
| `agenticore job <id>` | Get job details, output, and PR URL |
| `agenticore cancel <id>` | Cancel a running job |
| `agenticore profiles` | List available execution profiles |
| `agenticore serve` | Start the server |
| `agenticore status` | Check server health |
| `agenticore version` | Show version |
| `agenticore update` | Update to latest version |
| `agenticore init-shared-fs` | Initialise shared filesystem (Kubernetes) |
| `agenticore drain` | Drain pod before shutdown (Kubernetes) |
| `agenticore hooks sync [--target T]` | Clone/fetch repos and rebuild profiles (`all`, `agentihooks`, `bundle`, `agentihub`) |
| `agenticore agent <flags>` | Build, run, and manage the local container / dev compose stack |
| `agenticore push --main` | Build and push Docker image to a registry |

```bash
# Submit a task
agenticore run "fix the null pointer in auth.py" \
  --repo https://github.com/org/repo \
  --profile code

# Wait for result and see PR URL
agenticore run "add unit tests for the parser" \
  --repo https://github.com/org/repo \
  --wait

# Check a specific job
agenticore job a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

---

## MCP Tools

Connect any MCP-compatible client and use these tools:

| Tool | Parameters | Description |
|------|-----------|-------------|
| `run_task` | `task`, `repo_url`, `profile`, `base_ref`, `wait`, `session_id`, `create_repo` | Submit a task for Claude Code execution |
| `get_job` | `job_id` | Get status, output, and PR URL for a job |
| `list_jobs` | `limit`, `status` | List recent jobs |
| `cancel_job` | `job_id` | Cancel a running or queued job |
| `list_profiles` | — | List available execution profiles |
| `plan_task` | `task`, `repo_url`, `wait` | Create a read-only implementation plan |
| `execute_plan` | `plan_id`, `repo_url`, `profile`, `wait` | Execute a ready plan as a coding job |
| `list_worktrees` | — | List all worktrees with age, size, branch, push status |
| `cleanup_worktrees` | `paths` | Remove specific worktrees (unlock + delete) |

All tools return the same JSON structure as the REST endpoints.

---

## REST API

```bash
# Submit a job (async — returns immediately with job ID)
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task": "fix the auth bug", "repo_url": "https://github.com/org/repo"}'

# Submit and wait for completion
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task": "fix the auth bug", "repo_url": "https://github.com/org/repo", "wait": true}'

# Get job status, output, and PR URL
curl http://localhost:8200/jobs/{job_id}

# List jobs (with optional filters)
curl "http://localhost:8200/jobs?limit=10&status=running"

# Cancel a job
curl -X DELETE http://localhost:8200/jobs/{job_id}

# List profiles
curl http://localhost:8200/profiles

# Health check (no auth required)
curl http://localhost:8200/health

# Trigger repo sync on demand (all repos)
curl -X POST http://localhost:8200/admin/sync

# Sync a specific repo
curl -X POST "http://localhost:8200/admin/sync?target=agentihub"
```

---

## Connecting MCP Clients

Agenticore exposes two MCP transports on the same port:

| Transport | Endpoint | Use Case |
|-----------|----------|----------|
| Streamable HTTP | `/mcp` | `type: "http"` — Claude Code, Claude Desktop, most clients |
| SSE | `/sse` | Legacy SSE clients |
| stdio | stdin/stdout | Direct Claude Code subprocess integration |

### Claude Code CLI / Claude Desktop

Add to your project's `.mcp.json` or `~/.mcp.json`:

```json
{
  "mcpServers": {
    "agenticore": {
      "type": "http",
      "url": "http://localhost:8200/mcp"
    }
  }
}
```

### With API Key Authentication

```json
{
  "mcpServers": {
    "agenticore": {
      "type": "http",
      "url": "http://your-server:8200/mcp",
      "headers": {
        "X-API-Key": "your-secret-key"
      }
    }
  }
}
```

### stdio (Claude Code subprocess)

```json
{
  "mcpServers": {
    "agenticore": {
      "command": "python",
      "args": ["-m", "agenticore"]
    }
  }
}
```

---

## Authentication

Authentication is **optional**. When disabled, all endpoints are public.

### API Keys

```bash
# Via environment variable (comma-separated for multiple keys)
AGENTICORE_API_KEYS="key-1,key-2" agenticore serve
```

Pass the key in any of these ways:

| Method | Example |
|--------|---------|
| Header | `curl -H "X-Api-Key: key-1" http://localhost:8200/jobs` |
| Query param | `curl "http://localhost:8200/jobs?api_key=key-1"` |
| Bearer token | `curl -H "Authorization: Bearer key-1" http://localhost:8200/jobs` |

The `/health` endpoint is always public regardless of auth settings.

---

## Profiles

Profiles are **directory packages** that configure how Claude Code runs. Each profile
is a self-contained `.claude/` tree installed into `~/.claude/` at container startup
by `agentihooks global`. Claude Code reads from `~/.claude/` by default.

```
<profiles-dir>/{name}/
├── profile.yml          ← Agenticore metadata (model, turns, auto_pr, timeout…)
├── .claude/
│   ├── settings.json    ← Hooks, tool permissions, env vars
│   ├── CLAUDE.md        ← System instructions for Claude
│   ├── agents/          ← Custom subagents
│   └── skills/          ← Custom slash-command skills
└── .mcp.json            ← MCP server config merged into the job
```

### Profile discovery

Profiles are **not bundled with Agenticore**. They live in two places:

| Source | Path | Set via |
|--------|------|---------|
| agentihooks integration | `{AGENTICORE_AGENTIHOOKS_PATH}/profiles/` | `AGENTICORE_AGENTIHOOKS_PATH` env var |
| User profiles | `~/.agenticore/profiles/` | Always checked |

Later sources override earlier ones when names collide.

### Profile inheritance

```yaml
# ~/.agenticore/profiles/code-strict/profile.yml
name: code-strict
extends: code          # inherits all settings from 'code'

claude:
  max_turns: 20
  effort: high
```

Child values override parent defaults. `.claude/` files are layered (child overlays
parent) during materialization.

### What Agenticore does with a profile at job start

1. Resolves the profile path (for tracking/audit)
2. For `extends` profiles: merges chain into a per-job directory for `.mcp.json`
3. Injects `.mcp.json` into the job CWD (merging with any existing `.mcp.json`)
4. Translates `profile.yml` `claude:` fields into CLI flags:
   `--model sonnet --max-turns 80 --permission-mode bypassPermissions …`

Full profile reference: [Profile System docs](docs/architecture/profile-system.md)

---

## Agent Mode

Agent Mode transforms Agenticore from a job orchestrator into a **purpose-built
agent container**. Where standard mode clones repos and creates PRs, Agent Mode
runs a pre-configured **package** — a directory with a system prompt, MCP
servers, hooks, and skills — and exposes it as a completions API.

Packages follow the same `.claude/` directory convention as agentihub packages.
The difference is lifecycle: profiles are materialized per-job and discarded;
packages are mounted at container startup and define the agent's permanent
identity.

```
Standard Mode:  Request → clone repo → bespoke worktree → materialize profile → claude → PR
Agent Mode:     Request → load package → claude -p "task" → result (+ notifications)
```

### Key features

- **Async completion queue** — `wait=false` pushes to a Redis queue; a worker
  process picks it up. Poll `GET /completions/{uuid}` or receive results via
  webhook callback.
- **Notification streaming** — Real-time `status`, `tool_call`, and `thinking`
  events delivered to a `callback_url` during execution via Claude Code hooks.
- **Session continuity** — Conversations can be resumed across requests using
  the external correlation UUID.
- **Redis+file fallback** — Same pattern as the job store. Everything works
  without Redis (inline execution, file-based state).

### Quick start

```bash
# Start the server in agent mode
AGENT_MODE=true AGENT_MODE_PACKAGE_DIR=./my-package \
  AGENTICORE_TRANSPORT=sse agenticore serve

# Start the queue worker (separate terminal)
AGENT_MODE=true AGENT_MODE_PACKAGE_DIR=./my-package \
  python -m agenticore.agent_mode

# Submit an async completion with webhook notifications
curl -X POST http://localhost:8200/completions \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Analyze the auth module",
    "uuid": "req-1",
    "wait": false,
    "callback_url": "https://your-app.com/webhook",
    "notifications": {"status": true, "tool_call": true}
  }'

# Poll for result
curl http://localhost:8200/completions/req-1
```

Full reference: [Agent Mode docs](docs/architecture/agent-mode.md)

---

## Helm (Kubernetes)

Agenticore ships a production-ready Helm chart published to GHCR. The chart
deploys a **StatefulSet** with a **shared RWX PVC** (NFS / EFS / Azure Files / Ceph)
so all pods share the same repo cache and job state, with **KEDA autoscaling**
based on Redis queue depth and **graceful drain** on pod shutdown.

```
Internet ──► LoadBalancer :8200
                    │
     ┌──────────────▼──────────────────────────┐
     │  Agenticore StatefulSet (0..N pods)      │
     │  Work-stealing from Redis queue          │
     └──────────┬──────────────────────────────┘
                │                │
         ┌──────▼───────┐  ┌─────▼───────────┐
         │  Redis        │  │  Shared RWX PVC  │
         │  jobs · locks │  │  /shared/        │
         │  KEDA queue   │  │  ├─ repos/       │
         └───────────────┘  │  ├─ jobs/        │
                            │  └─ job-state/   │
         KEDA ScaledObject  └─────────────────┘
         watches Redis queue
```

### Install

```bash
# 1. Create the Kubernetes Secret (once per cluster)
kubectl create secret generic agenticore-secrets \
  --from-literal=redis-url="redis://:password@redis:6379" \
  --from-literal=redis-address="redis:6379" \
  --from-literal=anthropic-api-key="sk-ant-..." \
  --from-literal=github-token="ghp_..."

# 2. Install the chart
helm install agenticore \
  oci://ghcr.io/the-cloud-clock-work/charts/agenticore \
  --set storage.className=your-rwx-storage-class
```

### Key values

| Value | Default | Description |
|-------|---------|-------------|
| `storage.className` | `nfs-client` | RWX storage class (required) |
| `storage.size` | `100Gi` | PVC size |
| `replicas` | `2` | Static replica count (ignored when KEDA enabled) |
| `image.tag` | `latest` | Container image tag |
| `config.defaultProfile` | `code` | Default execution profile |
| `config.maxParallelJobs` | `3` | Max Claude subprocesses per pod |
| `keda.enabled` | `false` | Enable KEDA autoscaling |
| `keda.minReplicas` | `1` | KEDA min replicas |
| `keda.maxReplicas` | `10` | KEDA max replicas |
| `ingress.enabled` | `false` | Enable Ingress resource |
| `ingress.host` | `agenticore.example.com` | Ingress hostname |

### Autoscaling with KEDA

```bash
helm upgrade agenticore \
  oci://ghcr.io/the-cloud-clock-work/charts/agenticore \
  --set keda.enabled=true \
  --set keda.redisAddress=redis:6379 \
  --set keda.maxReplicas=20
```

A `ScaledObject` watches the Redis job queue and adds one pod per 5 pending jobs.

### Graceful drain

StatefulSet pods run `agenticore drain --timeout 270` as a PreStop hook. Drain:
1. Marks the pod as draining in Redis (new jobs route elsewhere)
2. Waits for all in-progress jobs to finish
3. Exits cleanly — Kubernetes then sends SIGTERM

Full Kubernetes guide: [Kubernetes Deployment](docs/deployment/kubernetes.md)

---

## Docker

```bash
# Local dev — full stack (Agenticore + Redis + PostgreSQL + OTEL Collector)
cp .env.example .env
docker compose up --build -d

# Production — Agenticore only (point at your managed services)
docker run -d \
  -p 8200:8200 \
  -e AGENTICORE_TRANSPORT=sse \
  -e AGENTICORE_HOST=0.0.0.0 \
  -e ANTHROPIC_AUTH_TOKEN=sk-ant-... \
  -e REDIS_URL=redis://your-redis:6379/0 \
  -e GITHUB_TOKEN=ghp_... \
  tccw/agenticore
```

---

## Local Development

`docker-compose.dev.yml` emulates a Kubernetes deployment on your machine — same
Dockerfile, same entrypoint, same profile materialisation via agentihooks. A shared
named volume at `/shared` mirrors the K8s RWX PVC, and `HOME=/shared` makes the
entrypoint write `.claude/` config, profiles, and bashrc into it.

**Requirements:**

- A `.env` file in the project root (or `$HOME`). See
  [Docker Compose docs](docs/deployment/docker-compose.md#dev-compose-docker-composeyml)
  for the full variable reference.
- Docker with Compose v2.

**Quick start:**

```bash
# Start the dev stack
agenticore agent --compose-up

# Shell into the running container
agenticore agent --compose-enter

# Follow logs
agenticore agent --compose-logs

# Tear down
agenticore agent --compose-down
```

**Shell aliases** — run `bash automation/alias_setup.sh` to install `ac_*` shortcuts:

| Alias | Command |
|-------|---------|
| `ac_build_agent` | `agenticore agent --build` |
| `ac_run_agent` | `agenticore agent --run` |
| `ac_enter_agent` | `agenticore agent --enter` |
| `ac_stop_agent` | `agenticore agent --stop` |
| `ac_logs_agent` | `agenticore agent --logs` |
| `ac_compose_up` | `agenticore agent --compose-up` |
| `ac_compose_down` | `agenticore agent --compose-down` |
| `ac_compose_enter` | `agenticore agent --compose-enter` |
| `ac_compose_logs` | `agenticore agent --compose-logs` |
| `ac_push_main` | `agenticore push --main` |

Full CLI reference: [CLI Commands](docs/reference/cli-commands.md).
Full compose details: [Docker Compose](docs/deployment/docker-compose.md).

---

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_TRANSPORT` | `stdio` | `sse` for HTTP server, `stdio` for MCP pipe |
| `AGENTICORE_HOST` | `127.0.0.1` | Bind address |
| `AGENTICORE_PORT` | `8200` | Server port |
| `AGENTICORE_API_KEYS` | _(empty)_ | Comma-separated API keys (optional) |
| `ANTHROPIC_AUTH_TOKEN` | _(empty)_ | Anthropic API key passed to Claude |
| `REDIS_URL` | _(empty)_ | Redis URL — omit for file-based fallback |
| `GITHUB_TOKEN` | _(empty)_ | GitHub token for auto-PR |
| `AGENTICORE_DEFAULT_PROFILE` | `code` | Profile when none specified |
| `AGENTICORE_CLAUDE_TIMEOUT` | `3600` | Max job runtime in seconds |
| `AGENTICORE_AGENTIHOOKS_PATH` | _(empty)_ | Explicit path to agentihooks repo (skips cloning) |
| `AGENTICORE_AGENTIHOOKS_URL` | _(empty)_ | Git URL to clone agentihooks from (supports `GITHUB_TOKEN`) |
| `AGENTICORE_AGENTIHOOKS_SYNC_INTERVAL` | `300` | Agentihooks hot-reload interval in seconds (`0` disables) |
| `AGENTICORE_AGENTIHOOKS_BUNDLE_URL` | _(empty)_ | Git URL to clone the agentihooks bundle repo |
| `AGENTICORE_AGENTIHOOKS_BUNDLE_SYNC_INTERVAL` | `300` | Bundle hot-reload interval in seconds (`0` disables) |
| `AGENTICORE_AGENTIHUB_URL` | _(empty)_ | Git URL for agentihub repo (agent mode) |
| `AGENTICORE_AGENTIHUB_PATH` | _(empty)_ | Explicit path to agentihub repo (skips cloning) |
| `AGENTICORE_AGENTIHUB_SYNC_INTERVAL` | `300` | Agentihub hot-reload interval in seconds (`0` disables) |
| `AGENTIHUB_AGENT` | _(empty)_ | Agent name to load in agent mode (matches `agents/{name}/`) |
| `AGENTICORE_SHARED_FS_ROOT` | _(empty)_ | Shared FS root (Kubernetes mode) |
| `CLAUDE_CODE_HOME_DIR` | `$HOME` | Home dir root — Claude uses `$CLAUDE_CODE_HOME_DIR/.claude/` |

Full reference: [Configuration docs](docs/reference/configuration.md)

---

## Authentication

Agenticore resolves Claude credentials in order:

1. **`CLAUDE_CODE_OAUTH_TOKEN`** — Long-lived OAuth token (set as a K8s secret or env var). When present, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are removed so Claude Code talks directly to Anthropic.
2. **Static env** — `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL` as configured (typically pointing at a LiteLLM proxy).

GitHub token resolution:

1. **GitHub App** — `GITHUB_APP_ID` + private key + `GITHUB_APP_INSTALLATION_ID` (short-lived, auto-rotated).
2. **Static `GITHUB_TOKEN`** — PAT (classic or fine-grained).
3. **None** — public repos only, no PRs.

---

## OTEL Observability

Every job produces a Langfuse trace with spans for each Claude turn, including
prompts, tool calls, and token counts.

```bash
# Enable in .env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_HOST=https://cloud.langfuse.com

AGENTICORE_OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

The bundled `docker-compose.yml` includes an OTEL Collector pre-wired to push
traces to both Langfuse (SDK) and PostgreSQL (raw spans).

Full setup: [OTEL Pipeline docs](docs/deployment/otel-pipeline.md)

---

## Documentation

- [Quickstart](docs/getting-started/quickstart.md)
- [Connecting Clients](docs/getting-started/connecting-clients.md)
- [Profile System](docs/architecture/profile-system.md)
- [Agent Mode](docs/architecture/agent-mode.md)
- [Architecture Internals](docs/architecture/internals.md)
- [Job Execution](docs/architecture/job-execution.md)
- [Kubernetes Deployment](docs/deployment/kubernetes.md)
- [Docker Compose](docs/deployment/docker-compose.md)
- [OTEL Pipeline](docs/deployment/otel-pipeline.md)
- [CLI Reference](docs/reference/cli-commands.md)
- [API Reference](docs/reference/api-reference.md)
- [Configuration](docs/reference/configuration.md)

---

## Development

```bash
pip install -e ".[dev]"

# Tests
pytest tests/unit -v -m unit --cov=agenticore

# Lint
ruff check agenticore/ tests/
ruff format --check agenticore/ tests/
```
