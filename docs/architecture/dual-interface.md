---
title: Dual Interface
nav_order: 2
---

# Dual Interface: MCP + REST

Agenticore serves two interfaces from a single ASGI application: MCP for AI clients
and REST for programmatic access. Both share the same tool implementations and
return identical JSON response structures.

## ASGI App Architecture

The server composes three sub-applications into a single ASGI app with path-based
routing:

```
Incoming Request
       |
       v
+------+------+
| Auth Middle |  <-- _ApiKeyMiddleware (if api_keys configured)
| ware        |      Skips /health (always public)
+------+------+
       |
       v
+------+---------------------------------+
|           combined_app (ASGI)          |
|                                        |
|  Path routing:                         |
|  /mcp          -> http_app (MCP)       |
|  /sse          -> sse_app  (MCP SSE)   |
|  /messages/*   -> sse_app  (MCP SSE)   |
|  /*            -> rest_app (Starlette) |
+----------------------------------------+
```

## Request Routing

```
+----------+     +----------+     +-----------+
| /mcp     +---->| FastMCP  |     | Streamable|
|          |     | http_app +---->| HTTP      |
+----------+     +----------+     | Transport |
                                  +-----------+

+----------+     +----------+     +-----------+
| /sse     +---->| FastMCP  |     | SSE       |
| /messages|     | sse_app  +---->| Transport |
+----------+     +----------+     +-----------+

+----------+     +----------+     +-----------+
| /jobs    +---->| Starlette|     | REST API  |
| /profiles|     | rest_app +---->| JSON      |
| /health  |     +----------+     | responses |
+----------+                      +-----------+
```

## MCP Tools

| Tool | Parameters | Description |
|------|-----------|-------------|
| `run_task` | task, repo_url, profile, base_ref, wait, session_id | Submit a task |
| `get_job` | job_id | Get job details |
| `list_jobs` | limit, status | List recent jobs |
| `cancel_job` | job_id | Cancel a job |
| `list_profiles` | (none) | List profiles |

MCP tools are registered with `@mcp.tool()` on a `FastMCP("agenticore")` instance.
The FastMCP server is configured with `json_response=True`.

## REST Endpoints

| Method | Path | Maps To | Description |
|--------|------|---------|-------------|
| `POST` | `/jobs` | `run_task` | Submit a task |
| `GET` | `/jobs` | `list_jobs` | List recent jobs |
| `GET` | `/jobs/{job_id}` | `get_job` | Get job details |
| `DELETE` | `/jobs/{job_id}` | `cancel_job` | Cancel a job |
| `GET` | `/profiles` | `list_profiles` | List profiles |
| `GET` | `/health` | (direct) | Health check |

REST routes are built with Starlette. Each route handler calls the corresponding
MCP tool function directly and wraps the result in a `JSONResponse`.

## Authentication

The `_ApiKeyMiddleware` wraps the combined ASGI app when `server.api_keys` is
configured. It checks:

1. `X-Api-Key` header
2. `api_key` query parameter (fallback)

The `/health` endpoint is always public (no auth required). Unauthenticated
requests to protected endpoints receive a `401` response.

```python
# Requests must include one of:
# Header:  X-Api-Key: your-key
# Query:   ?api_key=your-key
```

## Transport Selection

The `AGENTICORE_TRANSPORT` env var (or `server.transport` in YAML) controls how
the server starts:

| Transport | Behavior |
|-----------|----------|
| `sse` | Starts uvicorn with the full ASGI app (MCP + REST + auth) |
| `stdio` | Runs FastMCP in stdio mode (MCP tools only, no REST) |

When using `sse` transport, all three interfaces (MCP Streamable HTTP, MCP SSE,
and REST) are available simultaneously on the same port.

## Lifespan Management

The ASGI app manages the MCP session manager lifecycle through ASGI lifespan
events. The session manager starts on `lifespan.startup` and shuts down on
`lifespan.shutdown`.

See [API Reference](../reference/api-reference.md) for detailed parameter
schemas and response formats.
