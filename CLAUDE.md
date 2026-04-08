# CLAUDE.md

## Project Overview

**Agenticore** is a Claude Code runner and orchestrator. It manages job lifecycle,
repo cloning/caching, profile-to-CLI-flag mapping, auto-PR creation, and OTEL pipeline.
Claude Code does the heavy lifting (coding, worktrees, telemetry).

## Build & Development

```bash
# Install
pip install -e .

# Start server (SSE transport)
AGENTICORE_TRANSPORT=sse agenticore serve

# Start server (stdio — for Claude Code CLI)
python -m agenticore

# Docker (full stack)
docker compose up --build -d

# Tests
pytest tests/unit -v -m unit --cov=agenticore

# Lint
ruff check agenticore/ tests/
ruff format --check agenticore/ tests/

# CLI
agenticore version
agenticore status
```

## Architecture

```
Request → Router → Clone repo → claude --worktree -p "task" → OTEL → PostgreSQL
                                                             → Auto-PR
                                                             → Job result → Redis
```

## Key Modules

| Module | Purpose |
|--------|---------|
| `server.py` | FastMCP server (5 tools) + REST routes |
| `config.py` | YAML config loader + env var overrides |
| `profiles.py` | Load profile packages → CLI flags |
| `repos.py` | Git clone/fetch with flock |
| `jobs.py` | Job store (Redis + file fallback) |
| `runner.py` | Spawn Claude subprocess with OTEL env |
| `router.py` | Code fast-path + AI fallback |
| `pr.py` | Auto-PR (git push + gh pr create) |
| `cli.py` | CLI tool |

## MCP Tools

- `run_task` — Submit task with repo_url, task, profile
- `get_job` — Job status, output, PR URL
- `list_jobs` — Recent jobs
- `cancel_job` — Cancel running job
- `list_profiles` — Available profiles

## Profile System

Profiles are directory-based packages with `profile.yml` + `.claude/` config.
Profile search dirs are derived from `resolve_repo_paths()` in `hooks.py`:
1. `{agentihooks}/profiles/` — base execution profiles (coding, admin, default)
2. `{agentihooks-bundle}/profiles/` — operator/agent profiles (agenticore, colt, patch-mode)
3. `~/.agenticore/profiles/` — user overrides

Paths are deterministic from `AGENTICORE_SHARED_FS_ROOT` (prod) or explicit env vars (dev mode).

**Dev Mode:** Set `AGENTICORE_DEV_MODE=true` + mount paths via `AGENTICORE_AGENTIHOOKS_PATH`, `AGENTICORE_AGENTIHOOKS_BUNDLE_PATH`, `AGENTICORE_AGENTIHUB_PATH`. Skips cloning and watchers.

**Profile Ownership:** Profiles belong to agentihooks, not agenticore.
`AGENTIHOOKS_PROFILE` → `cfg.agentihooks_profile`. Router/runner fall back to this when no profile specified.

**Agent packages:** `_provision_from_agentihub()` points `package_dir` directly at `{agentihub}/agents/{name}/package/` — no copy to `/app/package/`.

## Concurrency Gate

`MAX_PARALLEL_JOBS` is enforced via `asyncio.Semaphore` in `runner.py`.
When all slots are in use, new jobs are rejected immediately with `status=rejected`.
REST endpoints return HTTP 503 with `retry=true`. The OpenAI-compat `/v1/chat/completions` endpoint also returns 503 at capacity.
Agent mode queue (`completions.py`) checks queue depth before enqueue — rejects if `queue_depth >= max_queue_workers * 2`.
No queuing — callers handle retry (distributed systems responsibility).
Env var: `AGENTICORE_MAX_PARALLEL_JOBS` (default 3, set to 2 in dev Helm chart).

## A2A Agent Discovery (AgentiBridge)

Agenticore self-registers with AgentiBridge on boot for Agent-to-Agent discovery.

**Module:** `agenticore/bridge_client.py`

**Config vars (all optional):**
- `AGENTIBRIDGE_URL` — base URL; empty = disabled
- `AGENTIBRIDGE_API_KEY` — auth token
- `AGENTIBRIDGE_HEARTBEAT_INTERVAL` — seconds between heartbeats (default 60)
- `AGENTIBRIDGE_REGISTRATION_ENABLED` — kill switch (default true)
- `AGENTIBRIDGE_AGENT_ID` — override derived agent ID (default: pod name)

**Startup flow:** after agentihooks + agentihub sync, `_auto_register_with_bridge()` builds an agent card (id, capabilities from profiles + agent_mode, endpoint) and POSTs to `{AGENTIBRIDGE_URL}/agents/register`. A daemon thread heartbeats every 60s. If AgentiBridge restarts and loses registry, heartbeat detects `success: false` and auto-re-registers. All ops are best-effort — never blocks startup.

## Redis + File Fallback

Jobs stored as Redis hashes (`agenticore:job:{id}`) or `~/.agenticore/jobs/{id}.json`.

## MCP Whitelist Rendering (Agent Mode)

Before every `claude -p` call in agent_mode, `render_mcp_whitelist()` runs `agentihooks init --repo <package_dir>` to apply the MCP whitelist from `.agentihooks.json`.

**Flow:**
1. Global `.mcp.json` defines all 17+ MCP servers (fleet catalog, all disabled by default)
2. Agent's `.agentihooks.json` (committed in agentihub) lists `enabledMcpServers` — the lifetime whitelist
3. Pre-call: `agentihooks init --repo` computes `disabled = all_servers - enabled` and writes to `~/.claude.json`
4. `claude -p` starts with only whitelisted tools visible

**Key files:**
- `agenticore/hooks.py` → `render_mcp_whitelist()` — the pre-call hook
- `agenticore/agent_mode/agent.py` → calls `render_mcp_whitelist(cwd)` before every completions request
- `agentihub/agents/<name>/package/.agentihooks.json` — source of truth per agent

**Per-call subtraction:** The completions API accepts `disable_mcp_servers` (list) to narrow the whitelist for a single call:
```json
POST /v1/chat/completions
{ "disable_mcp_servers": ["tools-notifications", "tools-notifications-dev"], ... }
```
This temporarily removes those servers from `.agentihooks.json`, renders, then restores the file.

**Scope:** Agent mode only (completions API). Runner/job dispatch uses worktrees without `.agentihooks.json`.

**Smoke test:** `tests/smoke/test_mcp_whitelist.sh [agent] [--live]`
```
Phase 1 (instant):  data validation — config matches, enabled/disabled correct
Phase 2 (--live):   agent self-reports visible servers
Phase 3 (--live):   per-call subtraction — BEFORE/AFTER ~/.claude.json data proof
Result: 24/24 ALL PASS on anton-agent (2026-04-08)
```
