---
title: API Reference
nav_order: 3
---

# API Reference

Agenticore exposes the same functionality through two interfaces: MCP tools for
AI clients and REST endpoints for programmatic access. Both return identical
JSON response structures. When Agent Mode is enabled, additional completions
endpoints and the `agent_completions` MCP tool become available.

## MCP Tools

### run_task

Submit a task for Claude Code execution.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `task` | string | yes | | What Claude should do |
| `repo_url` | string | no | `""` | GitHub repo URL to clone |
| `profile` | string | no | `""` | Execution profile (auto-routed if empty) |
| `base_ref` | string | no | `"main"` | Base branch for PR |
| `session_id` | string | no | `""` | Claude session ID to resume |
| `file_path` | string | no | `""` | Path to a .mcp.json on the shared FS to inject into the job config |
| `create_repo` | boolean | no | `false` | Auto-create GitHub repo if it doesn't exist |
| `private` | boolean | no | `true` | Create repo as private |
| `worktree_id` | string | no | `""` | Reuse a pre-prepared worktree (from `prepare_worktree`) |

**Returns:** JSON with `success`, `job` (Job object). Always async (fire-and-forget) — poll with `get_job`.

```json
{
  "success": true,
  "job": {
    "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
    "status": "queued",
    "task": "fix the auth bug",
    "profile": "code",
    "repo_url": "https://github.com/org/repo",
    "base_ref": "main",
    "mode": "fire_and_forget",
    "created_at": "2025-01-15T10:30:00+00:00"
  }
}
```

### get_job

Get job status, output, and artifacts.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `job_id` | string | yes | Job UUID |

**Returns:** JSON with `success`, `job` (full Job object including output)

### list_jobs

List recent jobs with status.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | no | `20` | Max jobs to return |
| `status` | string | no | `""` | Filter by status |

**Returns:** JSON with `success`, `count`, `jobs` (list of Job objects)

### cancel_job

Cancel a running or queued job. Sends SIGTERM to the Claude subprocess.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `job_id` | string | yes | Job UUID to cancel |

**Returns:** JSON with `success`, `job` (updated Job object)

### list_profiles

List available execution profiles.

**Parameters:** None

**Returns:** JSON with `success`, `count`, `profiles` (list of Profile objects)

```json
{
  "success": true,
  "count": 2,
  "profiles": [
    {
      "name": "code",
      "description": "Autonomous coding worker",
      "model": "sonnet",
      "max_turns": 80,
      "worktree": true,
      "auto_pr": true,
      "permission_mode": "dangerously-skip-permissions"
    }
  ]
}
```



### prepare_worktree

Clone repo, create worktree, validate readiness. Returns worktree_id.
Phase 1 of two-phase workflow. Use the returned worktree_id with .

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
|  | string | yes | | GitHub repo URL to clone |
|  | string | no |  | Base branch |

**Returns:** JSON with , , , 

### get_worktree

Get worktree details by ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
|  | string | yes | Worktree UUID from  |

**Returns:** JSON with worktree details including status, path, branch

### list_worktrees

List all worktrees across cached repos with age, size, and push status.

**Parameters:** None

**Returns:** JSON with worktrees list (worktree_id, repo_key, branch, age_hours, size_bytes, pushed)

### cleanup_worktrees

Remove worktrees by path or age threshold.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
|  | string | no |  | Comma-separated list of worktree paths to remove |
|  | integer | no |  | Remove all worktrees older than N hours (0 = disabled) |

At least one of  or  must be provided.

**Returns:** JSON with removal results per worktree

## REST Endpoints

### POST /jobs

Submit a task. Maps to `run_task`.

```bash
curl -X POST http://localhost:8200/jobs \
  -H "Content-Type: application/json" \
  -d '{
    "task": "fix the auth bug",
    "repo_url": "https://github.com/org/repo",
    "profile": "code",
    "base_ref": "main",
    "wait": false
  }'
```

| Status | Condition |
|--------|-----------|
| `200` | Job created successfully |
| `400` | Invalid request |
| `401` | Missing or invalid API key |

### GET /jobs

List recent jobs. Maps to `list_jobs`.

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `limit` | int | `20` | Max jobs |
| `status` | string | (all) | Filter by status |

```bash
curl "http://localhost:8200/jobs?limit=10&status=running"
```

| Status | Condition |
|--------|-----------|
| `200` | Success |
| `401` | Missing or invalid API key |

### GET /jobs/{job_id}

Get job details. Maps to `get_job`.

```bash
curl http://localhost:8200/jobs/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

| Status | Condition |
|--------|-----------|
| `200` | Job found |
| `404` | Job not found |
| `401` | Missing or invalid API key |

### DELETE /jobs/{job_id}

Cancel a job. Maps to `cancel_job`.

```bash
curl -X DELETE http://localhost:8200/jobs/a1b2c3d4-e5f6-7890-abcd-ef1234567890
```

| Status | Condition |
|--------|-----------|
| `200` | Job cancelled |
| `404` | Job not found |
| `401` | Missing or invalid API key |

### GET /profiles

List profiles. Maps to `list_profiles`.

```bash
curl http://localhost:8200/profiles
```

| Status | Condition |
|--------|-----------|
| `200` | Success |
| `401` | Missing or invalid API key |

### GET /health

Health check. Always public (no auth required).

```bash
curl http://localhost:8200/health
```

```json
{"status": "ok", "service": "agenticore"}
```

| Status | Condition |
|--------|-----------|
| `200` | Server is running |

### POST /admin/sync

Trigger an on-demand sync of companion repos. Requires auth when API keys are configured.

```bash
# Sync all repos
curl -X POST http://localhost:8200/admin/sync

# Sync a specific repo
curl -X POST "http://localhost:8200/admin/sync?target=agentihub"
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `target` | query | `all` | Which repo to sync: `all`, `agentihooks` (URL-override only), `bundle`, `agentihub` |

The `agentihooks` target is only meaningful when `AGENTICORE_AGENTIHOOKS_URL`
is set (the override escape hatch). Under the default install — where
agentihooks is a PyPI dependency — the response is
`"agentihooks": "skipped (no url)"` and a pod restart is the upgrade path.

```json
{"agentihooks": "skipped (no url)", "bundle": "ok", "agentihub": "ok"}
```

| Status | Condition |
|--------|-----------|
| `200` | Sync completed (check per-repo status in response) |
| `400` | Invalid target parameter |

## Job Schema

The full Job object returned by all endpoints:

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | UUID |
| `repo_url` | string | GitHub repo URL |
| `base_ref` | string | Base branch |
| `task` | string | Task description |
| `profile` | string | Profile name used |
| `status` | string | `queued`, `running`, `succeeded`, `failed`, `cancelled`, `expired` |
| `mode` | string | `fire_and_forget` (sync mode removed in v0.11.0) |
| `exit_code` | int/null | Claude process exit code |
| `session_id` | string/null | Claude session ID |
| `pr_url` | string/null | Auto-created PR URL |
| `output` | string/null | Claude stdout (truncated to 50KB) |
| `error` | string/null | Error message or stderr |
| `created_at` | string | ISO 8601 timestamp |
| `started_at` | string/null | ISO 8601 timestamp |
| `ended_at` | string/null | ISO 8601 timestamp |
| `ttl_seconds` | int | Job TTL (default: 86400) |
| `pid` | int/null | OS process ID |
| `pod_name` | string | Pod that ran this job (Kubernetes) |
| `worktree_path` | string | Absolute path to worktree (local disk, not shared FS) |
| `job_config_dir` | string | Profile directory used for this job (informational) |

## Profile Schema

The Profile object returned by `list_profiles` / `GET /profiles`:

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Profile identifier |
| `description` | string | Human-readable description |
| `model` | string | Claude model (`sonnet`, `haiku`, `opus`) |
| `max_turns` | int | Max agentic turns |
| `worktree` | bool | Use `--worktree` flag |
| `auto_pr` | bool | Create PR on success |
| `permission_mode` | string | Permission mode for Claude |

## Authentication

When `AGENTICORE_API_KEYS` is set, all endpoints except `/health` require
authentication.

**Header method:**

```bash
curl -H "X-Api-Key: your-secret-key" http://localhost:8200/jobs
```

**Query parameter method:**

```bash
curl "http://localhost:8200/jobs?api_key=your-secret-key"
```

Unauthenticated requests return:

```json
{"error": "Invalid or missing API key"}
```

with HTTP status `401`.

## Agent Mode Endpoints

These endpoints are available when `AGENT_MODE=true`. See
[Agent Mode](../architecture/agent-mode.md) for the full architecture.

### POST /v1/chat/completions

OpenAI-compatible chat completions endpoint. Supports real-time SSE streaming,
conversation persistence, and slash-token visibility controls.

**Request headers:**

| Header | Required | Description |
|--------|----------|-------------|
| `Authorization: Bearer <key>` | yes (when auth enabled) | API key |
| `X-Conversation-Id` | no | Sticky session ID — agent resumes this conversation |
| `X-User-Id` | no | Caller identity for logging |
| `X-Request-Id` | no | Idempotency / tracing token |

**Request body:**

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `model` | string | yes | | Agent model identifier (e.g. `anton-agent`) |
| `messages` | array | yes | | OpenAI-format message array |
| `stream` | boolean | no | `false` | Stream SSE chunks in real time |
| `timeout` | int | no | config default | Max execution time (seconds) |
| `disable_mcp_servers` | array | no | `[]` | MCP server names to suppress for this call |
| `metadata.conversation_id` | string | no | | Alternate conversation ID (lower priority than header) |

**Conversation persistence — 4-tier resolver:**

1. `X-Conversation-Id` header (highest priority)
2. `metadata.conversation_id` in request body
3. Content hash of the first user message (deterministic resume)
4. Ephemeral — no session resume (lowest priority)

**Streaming (`stream=true`):**

Returns `text/event-stream`. Each SSE chunk is an OpenAI `chat.completion.chunk`
with `delta` carrying one of:

| Delta field | Event type | Description |
|-------------|------------|-------------|
| `content` | `text_delta` | Visible assistant text |
| `reasoning_content` | `thinking_delta` | Model reasoning (rendered in thinking panel) |
| `content` (fenced) | `tool_use_delta` | Tool call with args, emitted on `content_block_stop` |
| `content` (fenced) | `tool_result_delta` | Tool result paired below the call |

Stream ends with a stop chunk followed by `data: [DONE]`.

**Slash tokens** — intercepted from the last user message before the model sees it:

| Token | Effect |
|-------|--------|
| `/show-thinking` | Show model reasoning in stream |
| `/hide-thinking` | Hide model reasoning |
| `/show-tools` | Show tool calls and results |
| `/hide-tools` | Hide tool calls and results |
| `/show-all` | Show thinking + tools |
| `/hide-all` | Hide thinking + tools |
| `/stream-status` | Return current visibility config as inline meta SSE (no subprocess) |

Visibility settings are sticky per agent (stored in Redis / file fallback, no TTL).
Toggle-only requests (slash token with no other content) return the resolved config
inline without spawning a Claude subprocess.

**Non-streaming response:** standard OpenAI `chat.completion` object.

```bash
# Non-streaming
curl -X POST http://localhost:8200/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model": "anton-agent", "messages": [{"role": "user", "content": "hello"}]}'

# Streaming with sticky session
curl -N -X POST http://localhost:8200/v1/chat/completions \
  -H "Authorization: Bearer $API_KEY" \
  -H "X-Conversation-Id: my-session-123" \
  -H "Content-Type: application/json" \
  -d '{"model": "anton-agent", "messages": [{"role": "user", "content": "continue"}], "stream": true}'
```

| Status | Condition |
|--------|-----------|
| `200` | Success (streaming or complete) |
| `401` | Missing or invalid API key |
| `503` | Capacity full — retry |

### MCP Tool: agent_completions

Call the agent with a message. Supports both synchronous (`wait=true`) and
asynchronous (`wait=false`) execution.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `message` | string | yes | | Task/prompt for the agent |
| `uuid` | string | yes | | External correlation ID |
| `wait` | boolean | no | `true` | Block until completion |
| `stateless` | boolean | no | `false` | Fresh session (no resume) |
| `model` | string | no | config default | Claude model |
| `max_turns` | int | no | config default | Agentic turn limit |
| `system_prompt` | string | no | `""` | Inline system prompt override |
| `effort` | string | no | `""` | `low`, `medium`, `high` |
| `timeout` | int | no | config default | Max execution time (seconds) |
| `meta` | string | no | `"{}"` | Platform metadata JSON |

### POST /completions

> **Deprecated** — use `/v1/chat/completions` instead.

Submit a completion request. Maps to `agent_completions`.

```bash
# Synchronous (block until done)
curl -X POST http://localhost:8200/completions \
  -H "Content-Type: application/json" \
  -d '{"message": "fix the bug", "uuid": "req-1", "wait": true}'

# Asynchronous (returns 202, polls via GET /completions/{uuid})
curl -X POST http://localhost:8200/completions \
  -H "Content-Type: application/json" \
  -d '{"message": "fix the bug", "uuid": "req-1", "wait": false}'
```

**Response (`wait=false`):**

```json
{
    "success": true,
    "status": "queued",
    "uuid": "req-1",
    "poll_url": "/completions/req-1"
}
```

| Status | Condition |
|--------|-----------|
| `200` | Sync completion succeeded |
| `202` | Async completion queued |
| `400` | Missing `message` or `uuid` |
| `500` | Sync completion failed |

### GET /completions/{uuid}

Poll for completion status and result.

```bash
curl http://localhost:8200/completions/req-1
```

```json
{
    "success": true,
    "completion": {
        "uuid": "req-1",
        "status": "completed",
        "result": "Fixed the bug by...",
        "session_id": "claude-session-456",
        "cost_usd": 0.12,
        "duration_ms": 45000,
        "num_turns": 5,
        "is_error": false,
        "created_at": "2026-03-03T12:00:00Z",
        "ended_at": "2026-03-03T12:00:45Z"
    }
}
```

| Status | Condition |
|--------|-----------|
| `200` | Completion found |
| `404` | Completion not found |

### GET /completions

List completions.

| Query Param | Type | Default | Description |
|-------------|------|---------|-------------|
| `limit` | int | `20` | Max results |
| `status` | string | (all) | Filter by status |

```bash
curl "http://localhost:8200/completions?status=completed&limit=5"
```

