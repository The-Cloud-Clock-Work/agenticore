---
title: Internals
nav_order: 1
---

# Architecture Internals

Agenticore is a thin orchestration layer for Claude Code. It manages job lifecycle,
repo cloning, profile materialization, auto-PR creation, and OTEL pipeline setup.
Claude Code does the actual work: coding, worktree management, and telemetry emission.

## Design Philosophy

**Thin orchestration.** Agenticore does not reinvent features that Claude Code
already provides natively:

| Concern | Owner |
|---------|-------|
| Worktree management | Claude Code (`--worktree`) |
| Coding work | Claude Code |
| Telemetry emission | Claude Code (native OTEL) |
| Job lifecycle | Agenticore |
| Repo cloning + caching | Agenticore |
| Profile to CLI flags | Agenticore |
| Profile materialization | Agenticore |
| Auto-PR creation | Agenticore |
| OTEL pipeline to Langfuse + PostgreSQL | Agenticore (collector config + SDK) |

## Module Map

| Module | Purpose | Key Functions |
|--------|---------|---------------|
| `server.py` | FastMCP server + REST routes | `run_task()`, `_build_asgi_app()` |
| `config.py` | YAML config + env var overrides | `load_config()`, `get_config()` |
| `profiles.py` | Load profile packages, build CLI args, materialize `.claude/` | `load_profiles()`, `build_cli_args()`, `materialize_profile()` |
| `repos.py` | Git clone/fetch with flock or Redis lock | `ensure_clone()`, `repo_dir()` |
| `jobs.py` | Job store (Redis + file fallback) | `create_job()`, `get_job()`, `update_job()` |
| `runner.py` | Spawn Claude subprocess | `submit_job()`, `run_job()` |
| `telemetry.py` | Langfuse trace lifecycle + transcripts | `start_job_trace()`, `ship_transcript()` |
| `router.py` | Profile routing (code + AI fallback) | `route()`, `ai_route()` |
| `pr.py` | Auto-PR via `gh` CLI | `create_auto_pr()` |
| `cli.py` | CLI entrypoint | `main()`, 11 subcommands |

## Data Flow

```
+------------------+     +------------------+     +------------------+
|  MCP Client      |     |  REST Client     |     |  CLI             |
|  (AI agent)      |     |  (curl/httpx)    |     |  (agenticore)    |
+--------+---------+     +--------+---------+     +--------+---------+
         |                        |                        |
         v                        v                        v
+--------+------------------------+------------------------+---------+
|                      server.py (ASGI app)                          |
|  /mcp, /sse  ->  MCP tools     |  /jobs, /profiles  ->  REST API  |
+----------------------------+---+-----------------------------------+
                             |
                             v
                    +--------+--------+
                    |   router.py     |
                    |  route(profile) |
                    +--------+--------+
                             |
              +--------------+--------------+
              |                             |
              v                             v
     +--------+--------+          +--------+--------+
     |   repos.py       |          |   profiles.py   |
     |  ensure_clone()  |          |  build_cli_args()|
     |  (flock or       |          |  materialize_   |
     |   Redis lock)    |          |   profile()     |
     +--------+--------+          +--------+--------+
              |                             |
              +-------------+---------------+
                            |
                            v
                   +--------+--------+
                   |   runner.py     |
                   |  run_job()      |
                   |  claude --worktree -p "task" --model X ...
                   |  CLAUDE_CONFIG_DIR=/shared/jobs/{id}
                   +--------+--------+
                            |
              +-------------+-------------+
              |             |             |
              v             v             v
     +--------+--+  +-------+------+  +---+----------+  +-------------+
     |  jobs.py   |  |   pr.py      |  |  OTEL        |  | telemetry.py|
     | update_job |  | auto-PR      |  |  Collector   |  | Langfuse SDK|
     | Redis+file |  | gh pr create |  |  -> Langfuse |  | job traces  |
     +------------+  +--------------+  +-------+------+  +-------------+
```

## Redis + File Fallback

Every stateful operation works with and without Redis:

1. Try Redis first (if `REDIS_URL` is set and reachable)
2. Always write to filesystem as fallback
3. On read, prefer Redis; fall back to file

```
                  +----------+
                  | save_job |
                  +----+-----+
                       |
            +----------+----------+
            |                     |
            v                     v
  +---------+--------+   +-------+--------+
  |  Redis hash      |   |  JSON file     |
  |  agenticore:     |   |  AGENTICORE_   |
  |   job:{id}       |   |  JOBS_DIR/     |
  |  (with TTL)      |   |   {id}.json    |
  +---------+--------+   +-------+--------+
            |                     |
            +----------+----------+
                       |
                  +----+-----+
                  | get_job  |
                  +----------+
                  Redis first, file fallback
```

Redis keys use the namespace `{REDIS_KEY_PREFIX}:job:{id}`. Jobs stored as Redis
hashes receive a TTL matching `job_ttl_seconds` (default: 24h). File-based jobs
have no automatic expiry.

## Repo Cache Layout

### Local / Docker

Repos are cloned once and reused. Each repo is identified by a SHA-256 hash of its
URL (first 12 chars). Concurrent access is serialized with `flock`.

```
~/agenticore-repos/
├── a1b2c3d4e5f6/
|   ├── .lock             ← flock file (per-repo, single-host)
|   └── repo/             ← git clone
├── f6e5d4c3b2a1/
|   ├── .lock
|   └── repo/
...
```

### Kubernetes / shared FS

On shared NFS/EFS, `fcntl` advisory locks are not enforced across hosts.
Instead, the runner uses a Redis `SET NX` distributed lock
(`agenticore:lock:clone:{hash}`). Clone/fetch is idempotent, so a timeout
on the lock falls through safely.

```
/shared/repos/
├── a1b2c3d4e5f6/
|   └── repo/             ← git clone (shared across pods)
...
```

## Execution Modes

| Mode | Trigger | Behavior |
|------|---------|----------|
| Fire-and-forget | `wait=false` (default) | Returns job ID immediately, runs in background |
| Sync | `wait=true` | Holds connection until job completes |
| Stateful | `session_id` provided | Passes `--resume <id>` to Claude |
| Stateless | Default | Fresh Claude session per job |

## Config Precedence

```
env var  >  YAML file  >  built-in default
```

The config is loaded once as a module-level singleton (`get_config()`). Call
`reset_config()` to reload (used in tests).

See [Configuration Reference](../reference/configuration.md) for the full variable list.
