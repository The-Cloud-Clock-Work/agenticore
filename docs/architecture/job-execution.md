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
| `job_config_dir` | string | Profile directory used for this job (informational) |

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
 6. Create bespoke worktree (if profile.claude.worktree: true)
    - create_worktree(repo_dir, job_id, base_ref)
    - Branch: agenticore-{job_id[:8]}
    - Path: ~/.agenticore/worktrees/{job_id}
    - Locked immediately with reason "agenticore: job {job_id}"
    - Record worktree_path on job
    - Set cwd = worktree_path (Claude runs here)
 7. Materialize profile (resolve path for tracking)
    - Simple profiles: return profile directory as-is (zero I/O)
    - Extends profiles: merge into /shared/jobs/{job-id}/ for .mcp.json
    - Record job_config_dir on job (informational)
 8. Inject MCP configs (default + job.file_path) into cwd/.mcp.json
 9. Build template variables (TASK, REPO_URL, BASE_REF, JOB_ID, PROFILE)
10. Build CLI args from profile (build_cli_args) — --worktree is NEVER passed
11. Construct command: [claude_binary] + cli_args
12. Append --resume session_id (if session_id provided)
13. Build environment (inherit + OTEL vars + CLAUDE_CODE_HOME_DIR + GITHUB_TOKEN)
14. Spawn subprocess (asyncio.create_subprocess_exec) with cwd = worktree
15. Store PID in job record
16. Wait for completion with timeout (profile.claude.timeout)
17. Extract session_id from Claude's JSON stdout (scan for sessionId field)
18. Parse result: stdout → output, stderr → error, returncode → status
19. Auto-PR on success (if profile.auto_pr and repo_url set)
    - Branch name is deterministic: agenticore-{job_id[:8]}
    - Stage untracked files, push branch, gh pr create
20. (finally) Ship Claude session transcript to Langfuse as spans
21. (finally) Finalize Langfuse trace with status, exit_code, pr_url
```

!!! important "One Job = One PR"
    Each job creates an independent worktree, branch, and PR. There is no
    iteration on existing PRs. Submitting "fix PR #2" creates a new PR #3
    branched from main. The PR review gate (human or GitHub Copilot) is the
    quality control point before merge.

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
+-------------+     +-----------+     +----------+     +-----------+
| Deterministic|---->| Stage +   |---->| Push     |---->| Create PR |
| branch name  |     | commit    |     | branch   |     | gh pr     |
| agenticore-  |     | untracked |     | to       |     | create    |
| {id[:8]}     |     | files     |     | origin   |     |           |
+--------------+     +-----------+     +----------+     +----+------+
                                                             |
                                                             v
                                                        PR URL stored
                                                        in job record
```

The branch name is deterministic (`agenticore-{job_id[:8]}`) — no heuristic
search needed. A fallback (`_get_worktree_branch()`) exists for legacy jobs
that used Claude's internal branch naming.

**Requirements:**
- `GITHUB_TOKEN` must be set (or GitHub App configured)
- `gh` CLI must be installed
- The worktree branch must have commits ahead of the default branch

If any step fails, auto-PR is skipped silently (logged to stderr).

### PR as Quality Gate

The one-job-one-PR model is intentional. Each PR is a clean-room result:

- Branched from `origin/{base_ref}` (usually `main`)
- No contamination from previous jobs or parallel jobs
- PR review is the integration point — configure GitHub branch protection:
  - Require PR reviews (human approval)
  - Require status checks (CI/CD)
  - Enable GitHub Copilot review for automated first-pass
  - Require conversation resolution before merge

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
