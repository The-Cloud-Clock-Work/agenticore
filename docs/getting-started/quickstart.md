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
git clone https://github.com/The-Cloud-Clockwork/agenticore.git
cd agenticore
pip install -e .
```

### With Docker

```bash
git clone https://github.com/The-Cloud-Clockwork/agenticore.git
cd agenticore
touch .env
docker compose up --build -d
```

## Start the Server

### SSE transport (HTTP — recommended)

```bash
agenticore serve
```

The server starts at `http://127.0.0.1:8200` with MCP, REST, and health
endpoints.

### Custom host/port

```bash
agenticore serve --host 0.0.0.0 --port 9000
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
agenticore run "add a hello world endpoint" \
  --repo https://github.com/your-org/your-repo

# Wait for result
agenticore run "fix the typo in README.md" \
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

## Agent Mode Quick Start

When an agent pod is running (e.g. via `docker compose up` or on Kubernetes), it exposes an OpenAI-compatible `/v1/chat/completions` endpoint. You can chat with it directly using `curl`.

### Streaming conversation

```bash
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "sonnet",
    "stream": true,
    "messages": [{"role": "user", "content": "List the files in /tmp and tell me what you see."}]
  }'
```

Chunks arrive as `data: {...}` SSE lines. Thinking blocks arrive in `delta.reasoning_content`; tool events arrive as fenced markdown blocks (`` ```tool_use:Bash `` / `` ```tool_result ``) also in `delta.reasoning_content`. The final answer arrives in `delta.content`.

### Conversation persistence via `X-Conversation-Id`

Pass a stable conversation ID to resume a session across multiple requests:

```bash
# First turn
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Conversation-Id: my-session-42' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"Remember: my favourite colour is blue."}]}'

# Second turn — agent resumes the same Claude session
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -H 'X-Conversation-Id: my-session-42' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"What is my favourite colour?"}]}'
```

When no header is supplied, agenticore falls back to a content-hash of the conversation for session matching.

### Enable thinking + tool visibility

By default all event types are visible (as of v1.3.1). To toggle:

```bash
# Check current config
curl -sN http://localhost:8200/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/stream-status"}]}'

# Show all (thinking + tools + text)
curl -sN http://localhost:8200/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/show-all"}]}'
```

See [Test Streaming](test-streaming.md) for a full five-minute walkthrough and [Conversation Persistence](conversation-persistence.md) for the full session-resume reference.

## Next Steps

- [Test Streaming](test-streaming.md) — Port-forward an agent and watch events arrive live
- [Conversation Persistence](conversation-persistence.md) — Multi-turn sessions via X-Conversation-Id
- [Connecting Clients](connecting-clients.md) — Set up MCP, REST, and CLI clients
- [Configuration Reference](../reference/configuration.md) — All env vars and YAML config
- [Profile System](../architecture/profile-system.md) — Customize execution profiles
- [Docker Compose Deployment](../deployment/docker-compose.md) — Full-stack setup
