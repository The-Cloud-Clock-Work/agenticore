---
title: Job Execution
nav_order: 4
---

# Job Execution

Jobs are the core unit of work in Agenticore. Each job represents a single Claude
Code invocation with a defined lifecycle, from submission through execution to
completion and optional PR creation.

## Job State Machine

```
               +--------+
               | queued |
               +---+----+
                   |
            submit_job()
                   |
               +---v----+
               | running|
               +---+----+
                   |
         +---------+---------+---------+
         |         |         |         |
    +----v---+ +---v----+ +-v--------+ +---v------+
    |succeeded| | failed | |cancelled | | expired  |
    +----+---+ +--------+ +----------+ +----------+
         |
    (auto_pr?)
         |
    +----v---+
    | PR URL |
    +--------+
```

**Statuses:**

| Status | Description |
|--------|-------------|
| `queued` | Job created, waiting to run |
| `running` | Claude subprocess is executing |
| `succeeded` | Exit code 0 |
| `failed` | Non-zero exit code, timeout, or error |
| `cancelled` | User cancelled (SIGTERM sent) |
| `expired` | TTL exceeded (Redis only) |

## Job Data Model

| Field | Type | Description |
|-------|------|-------------|
| `id` | string | UUID (auto-generated) |
| `repo_url` | string | GitHub repo URL (empty for local tasks) |
| `base_ref` | string | Base branch (default: `main`) |
| `task` | string | Task description |
| `profile` | string | Profile name (default: `code`) |
| `status` | string | Current status |
| `mode` | string | `fire_and_forget` or `sync` |
| `exit_code` | int/null | Claude process exit code |
| `session_id` | string/null | Claude session ID (for resume) |
| `pr_url` | string/null | Auto-created PR URL |
| `output` | string/null | Claude stdout (truncated to 50KB) |
| `error` | string/null | Error message or stderr (truncated to 10KB) |
| `created_at` | string | ISO 8601 timestamp |
| `started_at` | string/null | ISO 8601 timestamp |
| `ended_at` | string/null | ISO 8601 timestamp |
| `ttl_seconds` | int | Job TTL (default: 86400) |
| `pid` | int/null | OS process ID of Claude subprocess |
| `pod_name` | string | Pod that ran this job (Kubernetes) |
| `worktree_path` | string | Absolute path to worktree on shared FS |
| `job_config_dir` | string | `CLAUDE_CONFIG_DIR` used for this job |

`pod_name` is recorded from `AGENTICORE_POD_NAME` (or `hostname` as fallback) at
job start. `worktree_path` and `job_config_dir` are populated in Kubernetes mode.

## Runner Pipeline

The `run_job()` function in `runner.py` executes the following steps:

```
 1. Load profile by name
 2. Record pod_name (AGENTICORE_POD_NAME or hostname) on job
 3. Mark job as "running" (update started_at)
 4. Start Langfuse trace — non-fatal, returns None if unconfigured
 5. Clone or fetch repo (if repo_url provided)
    - Local/Docker: ensure_clone() with fcntl flock per repo
    - Kubernetes:   ensure_clone() with Redis distributed lock (NFS-safe)
    - Detect default branch if base_ref not set
    - Record worktree_path on job (computed path, Claude creates it later)
 6. Materialize profile's .claude/ package
    - Local/Docker: copy into repo cwd
    - Kubernetes:   copy into /shared/jobs/{job-id}/ (CLAUDE_CONFIG_DIR)
    - Record job_config_dir on job
 7. Build template variables (TASK, REPO_URL, BASE_REF, JOB_ID, PROFILE)
 8. Build CLI args from profile (build_cli_args)
 9. Construct command: [claude_binary] + cli_args
10. Append --resume session_id (if session_id provided)
11. Build environment (inherit + OTEL vars + CLAUDE_CONFIG_DIR + GITHUB_TOKEN)
12. Spawn subprocess (asyncio.create_subprocess_exec)
13. Store PID in job record
14. Wait for completion with timeout (profile.claude.timeout)
15. Extract session_id from Claude's JSON stdout (scan for sessionId field)
16. Parse result: stdout → output, stderr → error, returncode → status
17. Auto-PR on success (if profile.auto_pr and repo_url set)
18. (finally) Ship Claude session transcript to Langfuse as spans
19. (finally) Finalize Langfuse trace with status, exit_code, pr_url
```

## OTEL Environment Variables

When OTEL is enabled, these variables are injected into the Claude subprocess
environment:

| Variable | Value | Description |
|----------|-------|-------------|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` | Enable Claude telemetry |
| `OTEL_METRICS_EXPORTER` | `otlp` | Metrics exporter type |
| `OTEL_LOGS_EXPORTER` | `otlp` | Logs exporter type |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | from config | Protocol (grpc/http) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | from config | Collector endpoint |
| `OTEL_LOG_USER_PROMPTS` | `0`/`1` | Log prompts in telemetry |
| `OTEL_LOG_TOOL_DETAILS` | `0`/`1` | Log tool details |

## Auto-PR Pipeline

When a job succeeds (`exit_code == 0`), `profile.auto_pr` is `true`, and a
`repo_url` was provided, the auto-PR pipeline runs:

```
Job succeeded
     |
     v
+----+-----+     +----------+     +----------+     +-----------+
| Find     |---->| Check    |---->| Push     |---->| Create PR |
| worktree |     | commits  |     | branch   |     | gh pr     |
| branch   |     | ahead of |     | to       |     | create    |
| (cc-*)   |     | origin   |     | origin   |     |           |
+----------+     +----------+     +----------+     +----+------+
                                                        |
                                                        v
                                                   PR URL stored
                                                   in job record
```

**Requirements:**
- `GITHUB_TOKEN` must be set
- `gh` CLI must be installed
- The worktree branch must have commits ahead of the default branch

If any step fails, auto-PR is skipped silently (logged to stderr).

## Cancellation

1. `cancel_job(job_id)` retrieves the job
2. If status is `queued` or `running`, proceed
3. If `pid` is set, send `os.kill(pid, 15)` (SIGTERM)
4. Update status to `cancelled`, set `ended_at`

`ProcessLookupError` is caught silently (process already exited).

## Concurrency

- Repo cloning is serialized per-repo:
  - **Local/Docker**: `fcntl.flock` (single host)
  - **Kubernetes**: Redis `SET NX` distributed lock (NFS-safe, cross-pod)
- Jobs run as `asyncio.create_task()` in fire-and-forget mode
- Work-stealing from Redis queue — any pod picks up the next job

## Submission Flow

```python
submit_job(task, profile, repo_url, wait=False)
```

| Parameter | Effect |
|-----------|--------|
| `wait=False` | Creates job, launches `run_job()` as background task, returns immediately |
| `wait=True` | Creates job, awaits `run_job()`, returns completed job |

See [Profile System](profile-system.md) for how profiles are resolved and
converted to CLI arguments.
