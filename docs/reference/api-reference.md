# API Reference

Agenticore exposes the same functionality through two interfaces: 5 MCP tools for
AI clients and 6 REST endpoints for programmatic access. Both return identical
JSON response structures.

## MCP Tools

### run_task

Submit a task for Claude Code execution.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `task` | string | yes | | What Claude should do |
| `repo_url` | string | no | `""` | GitHub repo URL to clone |
| `profile` | string | no | `""` | Execution profile (auto-routed if empty) |
| `base_ref` | string | no | `"main"` | Base branch for PR |
| `wait` | boolean | no | `false` | Wait for completion before returning |
| `session_id` | string | no | `""` | Claude session ID to resume |

**Returns:** JSON with `success`, `job` (Job object)

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
      "permission_mode": "bypassPermissions"
    }
  ]
}
```

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
| `mode` | string | `fire_and_forget` or `sync` |
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
