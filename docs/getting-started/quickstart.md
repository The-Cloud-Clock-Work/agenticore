---
title: Quickstart
nav_order: 1
---

# Quickstart

Get Agenticore running and submit your first job in under 5 minutes.

## Prerequisites

| Tool | Required | Purpose |
|------|----------|---------|
| Python 3.10+ | Yes | Runtime |
| Claude CLI | Yes | Task execution engine |
| Git | Yes | Repo cloning |
| `gh` CLI | For auto-PR | GitHub PR creation |
| Redis | Optional | Job store (file fallback available) |
| Docker | Optional | Full-stack deployment |

## Install

### From source (development)

```bash
git clone https://github.com/The-Cloud-Clock-Work/agenticore.git
cd agenticore
pip install -e .
```

### With Docker

```bash
git clone https://github.com/The-Cloud-Clock-Work/agenticore.git
cd agenticore
touch .env
docker compose up --build -d
```

## Start the Server

### SSE transport (HTTP — recommended)

```bash
agenticore run
```

The server starts at `http://127.0.0.1:8200` with MCP, REST, and health
endpoints.

### Custom host/port

```bash
agenticore run --host 0.0.0.0 --port 9000
```

### stdio transport (for Claude Code CLI integration)

```bash
AGENTICORE_TRANSPORT=stdio python -m agenticore
```

### Docker

```bash
docker compose up -d
```

## Submit Your First Job

### Via CLI

```bash
# Fire-and-forget
agenticore submit "add a hello world endpoint" \
  --repo https://github.com/your-org/your-repo

# Wait for result
agenticore submit "fix the typo in README.md" \
  --repo https://github.com/your-org/your-repo \
  --wait
```

### Via REST API

```bash
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "task": "add a hello world endpoint",
    "repo_url": "https://github.com/your-org/your-repo"
  }'
```

### Via MCP (from an AI client)

Configure your MCP client to connect to `http://localhost:8200/sse`, then use
the `run_task` tool:

```json
{
  "task": "add a hello world endpoint",
  "repo_url": "https://github.com/your-org/your-repo"
}
```

## Check Job Status

```bash
# List all jobs
agenticore jobs

# Get specific job details
agenticore job <job_id>

# Via REST
curl http://localhost:8200/jobs/<job_id>
```

## Next Steps

- [Connecting Clients](connecting-clients.md) — Set up MCP, REST, and CLI clients
- [Configuration Reference](../reference/configuration.md) — All env vars and YAML config
- [Profile System](../architecture/profile-system.md) — Customize execution profiles
- [Docker Compose Deployment](../deployment/docker-compose.md) — Full-stack setup
