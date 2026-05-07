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
| `agent_mode/conversation_key.py` | 4-tier conversation resolver |
| `agent_mode/stream_config.py` | Slash token parsing + sticky visibility |

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

**Path layout:** Clones live at `<CLONE_ROOT>/<dir-from-url>`. `AGENTICORE_CLONE_ROOT` is `/app/clones` (emptyDir, per-pod ephemeral) in k8s; falls back to `AGENTICORE_SHARED_FS_ROOT` when unset (local dev / legacy single-mount). State (`$HOME`, `.claude/`, `job-state/`) stays on `SHARED_FS_ROOT` (`/shared`, PVC). URLs determine what AND where. Bundle is optional. Hub is required for agent mode. Resolved paths land on `cfg.runtime.{agentihooks_dir,bundle_dir,hub_dir}`. Refresh via `agenticore hooks sync`, `POST /admin/sync`, or pod restart — pod restart wipes the emptyDir and re-clones cleanly. `render_mcp_whitelist` re-runs `agentihooks init --repo` before every agent-mode completion.

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

## Real-Time SSE Streaming (Agent Mode)

Every agenticore-backed agent is a **fully auditable, traceable, real-time observable agent**. Any chat client (LibreChat, OpenWebUI, raw `curl -N`) holds one open HTTP connection to `/v1/chat/completions` with `stream=true` and watches the agent's reasoning, tool calls, tool results, and final answer flow as live OpenAI-format SSE chunks — token-by-token, as the model produces them.

**Streaming hot path** (`agenticore/agent_mode/agent.py::execute_streaming`):
1. Strip slash tokens (server-side, deterministic — claude never sees them); load sticky visibility config from `agenticore:stream_config:{AGENTIHUB_AGENT}`
2. Spawn `claude -p ... --output-format stream-json --verbose --include-partial-messages`
3. Read `proc.stdout` line-by-line in an async loop; parse each JSONL `stream_event`:
   - `thinking_delta` → `format_thinking_delta` → `delta.reasoning_content` (rendered in reasoning panel)
   - `text_delta` → `format_text_delta` → `delta.content`
   - `content_block_start(tool_use)` + `input_json_delta` → accumulate args, emit fenced ` ```tool_use:NAME ` block on `content_block_stop`
   - `tool_result` (next user message) → fenced ` ```tool_result ` block paired below the call
4. Filter every event through `is_visible(event_type, stream_cfg)` before yielding
5. On `result` event: capture usage tokens, emit stop chunk + `data: [DONE]`

No transcript polling, no Redis event bus in the streaming path. The Redis bus (`agenticore:events:{uuid}` via `agentihooks/hooks/observability/event_relay.py`) is preserved for the non-streaming `execute()` path and any cross-process observability subscribers.

**Slash tokens** — intercepted in `stream_config.get_for_request` against the **last user message** (multi-turn aware, stripped before claude sees them):
- `/show-thinking` / `/hide-thinking` — toggle thinking visibility
- `/show-tools` / `/hide-tools` — toggle tool_use + tool_result visibility
- `/show-all` / `/hide-all` — toggle all
- `/stream-status` — return current visibility as inline meta SSE (no subprocess spawn)

When a request is **toggle-only** (slash tokens with empty cleaned message), agenticore returns the resolved config inline as a `stream_config` meta event without spawning claude — see `server.py::post_openai_chat_completions` `if found_tokens and not last_clean.strip()`.

**Sticky storage**: `agenticore:stream_config:{AGENTIHUB_AGENT}` Redis hash, no TTL, file fallback `~/.agenticore/stream_config/{agent_id}.json`. Defaults: `assistant_text` only; thinking and tools are opt-in.

**Auditing**: `tests/smoke/verify_streaming_pipeline.sh <agent>` — deterministic conversation against live pod, cross-validates SSE + Redis + logs + transcript. `tests/smoke/test_conversation_e2e.sh` — multi-turn persistence test through LiteLLM chain.

**Reference**: `docs/reference/sse-streaming.md` (chunk schema, fail modes), `docs/getting-started/test-streaming.md` (self-test walkthrough).

**LiteLLM / LibreChat integration**: agents are onboarded as openai-compatible models pointing at `http://<agent>.anton-dev.svc:8200/v1`. The `librechat-dev` LiteLLM unit's models allowlist controls which agents appear in LibreChat's model picker.

## Conversation Persistence (Agent Mode)

Every agenticore-backed agent supports **multi-turn conversation continuity** across the `/v1/chat/completions` endpoint. A 4-tier conversation key resolver infers conversation identity from the request, maps it to a Claude `--resume` session, and sends only the last user message on subsequent turns (Claude has prior context in its JSONL).

**4-tier resolver** (`agenticore/agent_mode/conversation_key.py`):
1. **Header** — `X-Conversation-Id`, `X-LibreChat-Conversation-Id`, `X-OpenWebUI-Chat-Id`
2. **Body** — `body.metadata.conversation_id` or `body.user` (UUID-shaped only)
3. **Content hash** — `sha256(system_prompt + first_user_message)[:16]` (zero-config fallback, toggleable via `AGENTICORE_CONV_HASH_FALLBACK`)
4. **Ephemeral** — `uuid4()` (stateless, one-shot)

**Storage key**: `conv:{agent_id}:{user_hint}:{key}` — agent-scoped and user-scoped. Redis hash `agenticore:session:conv:...` with `session_ttl` TTL (default 86400s). File fallback `~/.agenticore/agent_sessions.json`.

**Session lifecycle**: first turn → `--session-id <pre-picked-uuid>` creates persistent session. Subsequent turns → `--resume <uuid>`. Persistent sessions stay `active` across turns (not marked `completed` until explicitly closed or TTL expires).

**LibreChat integration**: `librechat.yaml` custom endpoint injects `X-Conversation-Id: {{conversationId}}` + `X-User-Id: {{user}}`. LiteLLM forwards via `forward_client_headers_to_llm_api: true` + `allowed_client_headers` whitelist.

**A2A convention**: caller sends `X-Conversation-Id: <caller-chosen-uuid>` header. See `docs/reference/a2a-conventions.md`.

**Reference docs**:
- `docs/reference/conversation-persistence.md` — full resolver spec, client config, Redis keys
- `docs/reference/a2a-conventions.md` — agent-to-agent header convention
- `tests/smoke/test_conversation_e2e.sh` — end-to-end test through LiteLLM chain
