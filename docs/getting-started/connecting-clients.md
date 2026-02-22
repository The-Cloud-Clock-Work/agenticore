# Connecting Clients

Agenticore supports three client interfaces: MCP for AI agents, REST for
programmatic access, and CLI for terminal use. All connect to the same server
and access the same job store.

## Client Connection Overview

```
+-------------------+     +-------------------+     +-------------------+
|  MCP Client       |     |  REST Client      |     |  CLI Client       |
|  (Claude, etc.)   |     |  (curl, httpx)    |     |  (agenticore)     |
+--------+----------+     +--------+----------+     +--------+----------+
         |                         |                         |
         |  /sse or /mcp           |  /jobs, /profiles       |  /jobs, /health
         |                         |                         |
         +-------------------------+-------------------------+
                                   |
                           +-------v-------+
                           |  Agenticore   |
                           |  :8200        |
                           +---------------+
```

## MCP Client Setup

### SSE Transport (recommended)

Add to your client's `.mcp.json`:

```json
{
  "mcpServers": {
    "agenticore": {
      "url": "http://localhost:8200/sse"
    }
  }
}
```

### Streamable HTTP Transport

```json
{
  "mcpServers": {
    "agenticore": {
      "url": "http://localhost:8200/mcp"
    }
  }
}
```

### With authentication

```json
{
  "mcpServers": {
    "agenticore": {
      "url": "http://localhost:8200/sse?api_key=your-secret-key"
    }
  }
}
```

### Available tools

Once connected, the MCP client has access to 5 tools:

| Tool | Description |
|------|-------------|
| `run_task` | Submit a task for execution |
| `get_job` | Get job status and output |
| `list_jobs` | List recent jobs |
| `cancel_job` | Cancel a running job |
| `list_profiles` | List execution profiles |

See [API Reference](../reference/api-reference.md) for parameter details.

## REST API Client

### curl

```bash
# Submit a job
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{"task": "fix the bug", "repo_url": "https://github.com/org/repo"}'

# List jobs
curl http://localhost:8200/jobs

# Get job details
curl http://localhost:8200/jobs/<job_id>

# Cancel a job
curl -X DELETE http://localhost:8200/jobs/<job_id>

# List profiles
curl http://localhost:8200/profiles

# Health check
curl http://localhost:8200/health
```

### Python (httpx)

```python
import httpx

base = "http://localhost:8200"

# Submit a job
resp = httpx.post(f"{base}/jobs", json={
    "task": "fix the bug",
    "repo_url": "https://github.com/org/repo",
    "profile": "code",
})
job = resp.json()["job"]

# Check status
resp = httpx.get(f"{base}/jobs/{job['id']}")
print(resp.json()["job"]["status"])
```

### With authentication

```bash
# Header method
curl -H "X-Api-Key: your-secret-key" http://localhost:8200/jobs

# Query parameter method
curl "http://localhost:8200/jobs?api_key=your-secret-key"
```

```python
# Python
headers = {"X-Api-Key": "your-secret-key"}
resp = httpx.get(f"{base}/jobs", headers=headers)
```

## CLI Client

The CLI communicates with a running server over REST.

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENTICORE_HOST` | `127.0.0.1` | Server host |
| `AGENTICORE_PORT` | `8200` | Server port |

```bash
# Connect to local server (default)
agenticore jobs

# Connect to remote server
AGENTICORE_HOST=10.0.0.5 AGENTICORE_PORT=9000 agenticore jobs
```

### Common commands

```bash
agenticore submit "fix the bug" --repo https://github.com/org/repo
agenticore jobs
agenticore job <job_id>
agenticore cancel <job_id>
agenticore profiles
agenticore status
```

See [CLI Reference](../reference/cli-commands.md) for the full command list.

## Authentication Setup

Authentication is optional. When enabled, all endpoints except `/health` require
an API key.

### Enable authentication

Set `AGENTICORE_API_KEYS` with one or more comma-separated keys:

```bash
# Environment variable
export AGENTICORE_API_KEYS="key-1,key-2"
agenticore run

# YAML config
# ~/.agenticore/config.yml
server:
  api_keys:
    - "key-1"
    - "key-2"
```

### Client authentication methods

| Method | Format | Example |
|--------|--------|---------|
| Header | `X-Api-Key: <key>` | `curl -H "X-Api-Key: abc123" ...` |
| Query param | `?api_key=<key>` | `curl "...?api_key=abc123"` |

The header method is preferred. Query parameters are supported for clients that
cannot set custom headers (e.g., some MCP client configurations).

## Transport Modes

| Transport | Endpoint | Protocol | Use Case |
|-----------|----------|----------|----------|
| SSE | `/sse` | Server-Sent Events | MCP clients (most compatible) |
| Streamable HTTP | `/mcp` | HTTP | MCP clients (newer protocol) |
| REST | `/jobs`, `/profiles`, `/health` | HTTP JSON | Programmatic access |
| stdio | (stdin/stdout) | MCP stdio | Direct Claude Code integration |

SSE and Streamable HTTP are MCP transports. REST is a standard JSON API.
When using `AGENTICORE_TRANSPORT=stdio`, only MCP tools are available (no REST
endpoints).

## Troubleshooting

**Connection refused:**
Ensure the server is running: `agenticore status` or `curl http://localhost:8200/health`.

**401 Unauthorized:**
API keys are configured but not provided in the request. Add the `X-Api-Key`
header or `api_key` query parameter.

**MCP client not discovering tools:**
1. Verify the URL in `.mcp.json` is correct (`/sse` or `/mcp`)
2. Check server logs for connection attempts
3. Try the health endpoint to confirm the server is reachable

**CLI commands fail with "Is the server running?":**
The CLI needs a running server. Start it with `agenticore run` or
`docker compose up -d`.
