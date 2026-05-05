---
title: Docker Compose
nav_order: 1
parent: Deployment
---

# Docker Compose Deployment

Agenticore ships a Docker Compose stack with four services: the Agenticore server,
Redis for job storage, PostgreSQL as the OTEL sink, and an OpenTelemetry Collector.

## Quick Start

```bash
docker compose up --build -d
```

The server is available at `http://localhost:8200`.

## Stack Overview

```
+-------------------+      +-------------------+
|   agenticore      |      |   otel-collector   |
|   :8200           +----->|   :4317 (gRPC)     |
|   (server)        |      |   :4318 (HTTP)     |
+--------+----------+      +--------+----------+
         |                           |
         v                           v
+--------+----------+      +--------+----------+
|   redis           |      |   postgres         |
|   :6379           |      |   :5432            |
|   (job store)     |      |   (OTEL sink)      |
+-------------------+      +-------------------+
```

## Services

| Service | Image | Port | Volume | Healthcheck |
|---------|-------|------|--------|-------------|
| `agenticore` | Built from `Dockerfile` | 8200 | `repos-data`, `jobs-data` | (none) |
| `redis` | `redis:7-alpine` | 6379 | `redis-data` | `redis-cli ping` |
| `postgres` | `postgres:16-alpine` | 5432 | `pg-data` | `pg_isready -U agenticore` |
| `otel-collector` | `otel/opentelemetry-collector-contrib:latest` | 4317, 4318 | config mount, `env_file: .env` | (none) |

### Startup Order

```
postgres (healthy) --> otel-collector
redis (healthy)    --> agenticore
```

The `agenticore` service waits for Redis to be healthy. The `otel-collector`
waits for PostgreSQL to be healthy.

## Volumes

| Volume | Mount Point | Purpose |
|--------|-------------|---------|
| `repos-data` | `/root/agenticore-repos` | Cloned repository cache |
| `jobs-data` | `/root/.agenticore/jobs` | Job JSON files (fallback store) |
| `redis-data` | `/data` | Redis persistence |
| `pg-data` | `/var/lib/postgresql/data` | PostgreSQL data |

## Dockerfile Walkthrough

The `Dockerfile` builds the Agenticore image:

```
python:3.12-slim
    |
    +--> Install git, curl
    +--> Install gh CLI (for auto-PR)
    +--> Copy pyproject.toml, agenticore/, defaults/
    +--> pip install -e .
    +--> Create dirs: /app/logs, jobs, profiles, repos
    +--> Set env: TRANSPORT=sse, HOST=0.0.0.0, PORT=8200
    +--> EXPOSE 8200
    +--> CMD: python -m agenticore
```

## Environment Variables

The compose file passes these to the `agenticore` service:

| Variable | Value | Description |
|----------|-------|-------------|
| `AGENTICORE_TRANSPORT` | `sse` | HTTP transport |
| `AGENTICORE_HOST` | `0.0.0.0` | Bind all interfaces |
| `AGENTICORE_PORT` | `8200` | Listen port |
| `AGENTICORE_API_KEYS` | from `.env` | Auth keys (optional) |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection |
| `REDIS_KEY_PREFIX` | `agenticore` | Redis key namespace |
| `AGENTICORE_REPOS_ROOT` | `/root/agenticore-repos` | Repos volume |
| `AGENTIHOOKS_PROFILE` | from `.env` | Active profile (set by agentihooks) |
| `AGENTICORE_CLAUDE_BINARY` | from `.env` | Claude binary path |
| `AGENTICORE_OTEL_ENABLED` | from `.env` | Enable OTEL |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://otel-collector:4317` | Collector endpoint |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` | OTLP protocol |
| `GITHUB_TOKEN` | from `.env` | GitHub token for auto-PR |
| `LANGFUSE_HOST` | from `.env` | Langfuse API host |
| `LANGFUSE_PUBLIC_KEY` | from `.env` | Langfuse public key (SDK) |
| `LANGFUSE_SECRET_KEY` | from `.env` | Langfuse secret key (SDK) |
| `LANGFUSE_BASIC_AUTH` | from `.env` | Base64 auth for OTEL collector |
| `AGENTICORE_AGENTIHOOKS_PATH` | from `.env` | Path to agentihooks repo |

The `otel-collector` service also reads from `.env` (via `env_file: .env`) to
get `LANGFUSE_HOST` and `LANGFUSE_BASIC_AUTH` for the Langfuse OTEL exporter.

Variables with `${VAR:-default}` syntax are sourced from a `.env` file at the
project root. The `.env` file must exist (even if empty).

## PostgreSQL Configuration

The OTEL sink PostgreSQL instance uses these defaults:

| Setting | Value |
|---------|-------|
| Database | `agenticore` |
| User | `agenticore` |
| Password | `agenticore` |

Override these for production deployments.

## Customization

### Change the exposed port

```bash
AGENTICORE_PORT=9000 docker compose up -d
```

### Add API key auth

Create a `.env` file:

```bash
AGENTICORE_API_KEYS=your-secret-key-1,your-secret-key-2
GITHUB_TOKEN=ghp_...
```

### Use an external Redis

Remove the `redis` service from the compose file and set `REDIS_URL` to your
external Redis instance.

## Troubleshooting

**Redis connection refused on startup:**
The `agenticore` service depends on Redis with `condition: service_healthy`.
If Redis takes too long to start, the health check retries (5 attempts, 5s
interval). Check `docker compose logs redis`.

**Missing `.env` file:**
Docker Compose expects a `.env` file for variable substitution. Create an empty
one if you don't need custom values:

```bash
touch .env
```

**Build errors with buildx:**
Ensure Docker buildx version is >= 0.17.0:

```bash
docker buildx version
```

---

## Dev Compose (`docker-compose.dev.yml`)

A lightweight single-service compose file that emulates a Kubernetes deployment
locally — same Dockerfile, same entrypoint, same profile materialisation via
agentihooks.

### What it runs

```yaml
services:
  agenticore:
    build: .
    ports: ["8200:8200"]
    volumes:
      - agenticore-shared:/shared              # mirrors K8s RWX PVC
      - ~/dev/agentihooks:/opt/agentihooks-src # dev loopback — AGENTICORE_AGENTIHOOKS_PATH=/opt/agentihooks-src triggers editable install over the PyPI version
      - ./docker/gateway-mcp.json:/shared/gateway-mcp.json:ro
    env_file: [.env]
    environment:
      HOME: /shared
      AGENTIHOOKS_MCP_FILE: /shared/gateway-mcp.json
```

| Concept | Production (K8s) | Dev Compose |
|---------|-----------------|-------------|
| Shared storage | RWX PVC at `/shared` | Named volume `agenticore-shared` |
| Agentihooks | PyPI dependency baked into the image | PyPI in image + optional bind-mount override at `~/dev/agentihooks` (set `AGENTICORE_AGENTIHOOKS_PATH`) |
| MCP gateway | ConfigMap mount | Bind-mounted `gateway-mcp.json` |
| `HOME` | `/shared` | `/shared` |

### How `HOME=/shared` works

The entrypoint sets `HOME=/shared` so all writes land in the shared volume:

1. `agentihooks global --profile <profile>` writes `.claude/` config, hooks, skills, agents, and `CLAUDE.md` into `$HOME/.claude/`
2. Gateway MCP servers are installed via `agentihooks --mcp`
3. Shell functions are appended to `$HOME/.bashrc` for `exec` sessions

This mirrors exactly what happens in Kubernetes where the shared PVC is mounted
at `/shared`.

### `.env` file

A `.env` file is **required** in the project root. It supplies all runtime
configuration. The variables below are grouped by category (names only — no
secret values should appear in docs):

| Section | Variables |
|---------|-----------|
| Agent Configuration | `IS_EVALUATION`, `PR_EVALUATION`, `EMAIL_EVALUATION`, `READ_ONLY` |
| API Server | `API_HOST`, `API_PORT`, `REMOTE_RUNNER_URL` |
| Authentication | `AGENT_API_KEY`, `AGENT_KEYS`, `REST_KEYS` |
| Claude CLI | `CLAUDE_TIMEOUT`, `CLAUDE_OUTPUT_FORMAT`, `CLAUDE_MAX_TURNS`, `MCP_TIMEOUT`, `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, `BASH_MAX_TIMEOUT_MS`, `MAX_THINKING_TOKENS` |
| Anthropic / LLM | `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL` |
| GitHub | `GITHUB_APP_ID`, `GITHUB_INSTALLATION_ID`, `GITHUB_SECRET_ID`, `GITHUB_TOKEN`, `GIT_USER_NAME`, `GIT_USER_EMAIL` |
| Database | `DATABASE_URL`, `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_NAME`, `POSTGRES_USERNAME`, `POSTGRES_PASSWORD` |
| Logging | `LOG_LEVEL`, `CLAUDE_LOGS`, `FASTMCP_LOG_LEVEL`, `LOG_USE_COLORS` |
| Agentihooks | `AGENTIHOOKS_PROFILE`, `AGENTIHOOKS_MCP_FILE`, `LITELLM_MCP_GATEWAY_KEY`, `LITELLM_MCP_GATEWAY_URL` |
| MCP Gateway | `LITELLM_URL`, `LITELLM_MASTER_KEY`, `LANGFUSE_*`, `GRAFANA_*`, `MATRIX_*` |

### CLI aliases

Run `bash automation/alias_setup.sh` to install shell shortcuts:

| Alias | Equivalent |
|-------|------------|
| `ac_compose_up` | `agenticore agent --compose-up` |
| `ac_compose_down` | `agenticore agent --compose-down` |
| `ac_compose_enter` | `agenticore agent --compose-enter` |
| `ac_compose_logs` | `agenticore agent --compose-logs` |
| `ac_build_agent` | `agenticore agent --build` |
| `ac_run_agent` | `agenticore agent --run` |
| `ac_enter_agent` | `agenticore agent --enter` |
| `ac_stop_agent` | `agenticore agent --stop` |
| `ac_logs_agent` | `agenticore agent --logs` |
| `ac_push_main` | `agenticore push --main` |

### Workflow

```bash
# 1. Start the dev stack
ac_compose_up

# 2. Iterate on code (changes to agentihooks are live via bind mount)

# 3. Inspect the running container
ac_compose_enter

# 4. Debug with logs
ac_compose_logs

# 5. Tear down when done
ac_compose_down
```

---

**OTEL collector not receiving data:**
Verify the collector is running and the endpoint is reachable from the
agenticore container:

```bash
docker compose exec agenticore curl -s http://otel-collector:4317
```
