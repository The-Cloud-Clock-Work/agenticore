# Agenticore

Claude Code runner and orchestrator. Submit a task, get a PR.

![Agenticore Architecture](docs/media/agenticore-readme-banner.png)

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/blob/main/LICENSE)
[![Tests](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml/badge.svg)](https://github.com/The-Cloud-Clock-Work/agenticore/actions/workflows/test.yml)
[![Docker](https://img.shields.io/docker/v/tccw/agenticore?label=Docker%20Hub)](https://hub.docker.com/r/tccw/agenticore)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://python.org)

```
Client (MCP / REST / CLI)
    │
    ▼
Router → Clone repo → claude --worktree -p "task" → Auto-PR → Job result (Redis)
                                    │
                                    └──► OTEL Collector → Langfuse / PostgreSQL
```

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

## Start the Server

```bash
agenticore serve
```

Starts on `http://127.0.0.1:8200`. Both MCP and REST are available on the same port.

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

```bash
# Submit a task
agenticore run "fix the null pointer in auth.py" \
  --repo https://github.com/org/repo \
  --profile code

# Wait for result and see output
agenticore run "add unit tests for the parser" \
  --repo https://github.com/org/repo \
  --wait

# Check a specific job
agenticore job a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

---

## MCP Tools

Connect any MCP-compatible client and use these 5 tools:

| Tool | Parameters | Description |
|------|-----------|-------------|
| `run_task` | `task`, `repo_url`, `profile`, `base_ref`, `wait`, `session_id` | Submit a task for Claude Code execution |
| `get_job` | `job_id` | Get status, output, and PR URL for a job |
| `list_jobs` | `limit`, `status` | List recent jobs |
| `cancel_job` | `job_id` | Cancel a running or queued job |
| `list_profiles` | — | List available execution profiles |

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

# Get job status and output
curl http://localhost:8200/jobs/{job_id}

# List jobs (with optional filters)
curl "http://localhost:8200/jobs?limit=10&status=running"

# Cancel a job
curl -X DELETE http://localhost:8200/jobs/{job_id}

# List profiles
curl http://localhost:8200/profiles

# Health check (no auth required)
curl http://localhost:8200/health
```

---

## Authentication

Authentication is **optional**. When disabled, all endpoints are public.

### API Keys (simple)

```bash
# Via environment variable (comma-separated for multiple keys)
AGENTICORE_API_KEYS="key-1,key-2" agenticore serve
```

Pass the key in requests:

| Method | Example |
|--------|---------|
| Header | `curl -H "X-Api-Key: key-1" http://localhost:8200/jobs` |
| Query param | `curl "http://localhost:8200/jobs?api_key=key-1"` |
| Bearer token | `curl -H "Authorization: Bearer key-1" http://localhost:8200/jobs` |

### OAuth 2.1 (for claude.ai and MCP clients)

Set `OAUTH_ISSUER_URL` to enable full OAuth 2.1 + PKCE. This lets claude.ai and other compliant MCP clients connect without a static API key — they go through the standard authorization code flow and get short-lived access tokens (1h) with automatic refresh (30 days).

```bash
OAUTH_ISSUER_URL=https://agenticore.example.com \
OAUTH_CLIENT_ID=my-client \
OAUTH_CLIENT_SECRET=my-secret \
OAUTH_ALLOWED_REDIRECT_URIS=https://claude.ai/oauth/callback \
agenticore serve
```

| Variable | Description |
|----------|-------------|
| `OAUTH_ISSUER_URL` | Master switch — your public server URL |
| `OAUTH_CLIENT_ID` | Pre-configured client ID (optional — open registration if unset) |
| `OAUTH_CLIENT_SECRET` | Pre-configured client secret |
| `OAUTH_ALLOWED_REDIRECT_URIS` | Comma-separated allowed redirect URIs |
| `OAUTH_ALLOWED_SCOPES` | Space-separated allowed scopes (optional) |
| `OAUTH_RESOURCE_URL` | Resource server URL (defaults to `{issuer}/mcp`) |

When OAuth is enabled, existing API keys still work as Bearer tokens — they are accepted in `load_access_token` as a fallback, so CLI and REST clients need no changes.

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

### With Authentication

Pass the API key in the `headers` object — never in the URL:

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

```bash
AGENTICORE_TRANSPORT=stdio python -m agenticore
```

Or in `.mcp.json`:

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

## Profiles

Profiles define how Claude runs — model, permissions, auto-PR, timeouts.

| Profile | Model | Auto-PR | Description |
|---------|-------|---------|-------------|
| `code` (default) | Sonnet | Yes | Autonomous coding — writes, commits, opens PR |
| `review` | Haiku | No | Read-only code review |

Pass `--profile <name>` to `run_task` or `agenticore run`. Omit it and the router picks for you.

Custom profiles live in `~/.agenticore/profiles/`. See the [Profile System docs](docs/architecture/profile-system.md).

---

## Helm (Kubernetes)

Install from GHCR with a single command:

```bash
# 1. Create the Kubernetes Secret (once per cluster)
kubectl create secret generic agenticore-secrets \
  --from-literal=redis-url="redis://:password@host:6379" \
  --from-literal=anthropic-api-key="sk-ant-..." \
  --from-literal=github-token="ghp_..."

# 2. Install the chart
helm install agenticore \
  oci://ghcr.io/the-cloud-clock-work/charts/agenticore \
  --version 0.1.5 \
  --set storage.className=your-rwx-storage-class
```

Upgrade: `helm upgrade agenticore oci://ghcr.io/the-cloud-clock-work/charts/agenticore --version 0.1.6`

Full configuration: [Kubernetes Deployment](docs/deployment/kubernetes.md)

---

## Docker

```bash
# Local dev — full stack (agenticore + Redis + PostgreSQL + OTEL Collector)
cp .env.example .env
docker compose up --build -d

# Production — agenticore only (point at your managed services)
docker run -d \
  -p 8200:8200 \
  -e AGENTICORE_TRANSPORT=sse \
  -e AGENTICORE_HOST=0.0.0.0 \
  -e REDIS_URL=redis://your-redis:6379/0 \
  -e GITHUB_TOKEN=ghp_... \
  tccw/agenticore
```

---

## Key Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_TRANSPORT` | `stdio` | `sse` for HTTP server, `stdio` for MCP pipe |
| `AGENTICORE_HOST` | `127.0.0.1` | Bind address |
| `AGENTICORE_PORT` | `8200` | Server port |
| `AGENTICORE_API_KEYS` | _(empty)_ | Comma-separated API keys (optional) |
| `REDIS_URL` | _(empty)_ | Redis URL — omit for file-based fallback |
| `GITHUB_TOKEN` | _(empty)_ | GitHub token for auto-PR |
| `AGENTICORE_DEFAULT_PROFILE` | `code` | Profile when none specified |
| `AGENTICORE_CLAUDE_TIMEOUT` | `3600` | Max job runtime in seconds |

Full reference: [Configuration docs](docs/reference/configuration.md)

---

## Documentation

- [Quickstart](docs/getting-started/quickstart.md)
- [Connecting Clients](docs/getting-started/connecting-clients.md)
- [CLI Reference](docs/reference/cli-commands.md)
- [API Reference](docs/reference/api-reference.md)
- [Configuration](docs/reference/configuration.md)
- [Profile System](docs/architecture/profile-system.md)
- [Kubernetes Deployment](docs/deployment/kubernetes.md)
- [OTEL Pipeline](docs/deployment/otel-pipeline.md)

---

## Development

```bash
pip install -e ".[dev]"

pytest tests/unit -v -m unit --cov=agenticore
ruff check agenticore/ tests/
```
