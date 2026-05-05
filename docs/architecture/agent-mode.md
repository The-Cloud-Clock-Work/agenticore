---
title: Agent Mode
nav_order: 5
parent: Architecture
---

# Agent Mode

Agent Mode transforms Agenticore from a job orchestrator into a **purpose-built
agent container**. Where standard Agenticore clones repos and creates PRs, Agent
Mode runs a **pre-configured package** — a directory with a system prompt, MCP
servers, hooks, and skills — and exposes it as a completions API. The package
*is* the agent's identity.

## Philosophy: Packages Are Agents

Agent Mode extends the same philosophy that drives the
[Profile System](profile-system.md), but inverts the relationship:

**Profiles** configure *how* Agenticore runs Claude on a repo. They are
transient — materialized at job start, discarded at job end. The repo is the
star; the profile is a tool.

**Packages** configure *what* the agent is. They are permanent — mounted into
the container at startup and define the agent's personality, capabilities, and
integration points. The package is the star; the repo is optional.

```
Standard Mode (profiles):
  Request → clone repo → materialize profile → claude --worktree → PR

Agent Mode (packages):
  Request → conversation_key resolver → load package → claude -p "task" → SSE / result
```

Both use the same `.claude/` directory convention. Both use the same
`settings.json`, `CLAUDE.md`, `.mcp.json` files. An agentihooks profile *can*
become an Agent Mode package — the structure is identical. The difference is
lifecycle and purpose.

### The Agentihub Connection

Agent packages come from an **agentihub** — a separate repo holding agent identities (CLAUDE.md, prompts, evaluation).

> **Public example**: [`agentihub-example`](https://github.com/The-Cloud-Clockwork/agentihub-example) ships a working `publishing` agent that walks through the full layout (`agent.yml` + `package/` + `evaluation/`). Clone it, point Agenticore at it, and you have a fully provisioned Agent Mode container. Fork it to author your own.

On startup, `agent_mode/initializer.py` clones the hub (via `AGENTICORE_AGENTIHUB_URL`)
and copies `agents/{name}/package/` → `/app/package/`. The container then
validates the package, runs startup scripts, caches the system prompt, and
waits for requests. A background watcher re-fetches the hub every
`AGENTICORE_AGENTIHUB_SYNC_INTERVAL` seconds (default 300, `0` disables).

[Agentihooks](https://github.com/The-Cloud-Clockwork/agentihooks) provides the
Claude Code runner (hooks, MCP tools, guardrails) but is **not** involved in
agent provisioning.

## Architecture Overview

```
POST /v1/chat/completions
        │
        ▼
conversation_key resolver          ← 4-tier: header → body → content_hash → ephemeral
        │
        ▼
session_registry lookup            ← Redis: conv:{agent_id}:{user}:{key}
  first turn  →  --session-id <new-uuid>
  subsequent  →  --resume <claude_session_id>
        │
        ▼
AgentExecutor.execute_streaming()
        │
        ▼
spawn claude -p \
  --output-format stream-json \
  --verbose \
  --include-partial-messages
        │
  read proc.stdout line-by-line
        │
  parse stream-json events:
    thinking_delta    → reasoning_content SSE chunk
    text_delta        → content SSE chunk
    tool_use          → fenced ```tool_use block
    tool_result       → fenced ```tool_result block
    result            → stop chunk + usage
        │
        ▼
OpenAI-format SSE chunks → client
```

Non-streaming requests (`stream=false`) use `AgentExecutor.execute()`, which
reads the final `result` event from the subprocess and returns a single JSON
response. The Redis event bus (`agenticore:events:{uuid}` via
`event_relay.py`) is preserved for non-streaming observability subscribers
only — it is **not** in the streaming hot path.

## Two Execution Modes

### Streaming (`stream=true`, default for OpenAI-compat clients)

The caller opens a persistent HTTP connection to `POST /v1/chat/completions`
and receives OpenAI-format SSE chunks as the model produces them. Agenticore
spawns Claude with `--output-format stream-json --verbose
--include-partial-messages` and reads `proc.stdout` line-by-line, translating
each JSONL event into an SSE chunk.

This is the primary path for LibreChat, OpenWebUI, and any OpenAI-compat
chat client.

### Synchronous (`wait=true`, legacy completions API)

The caller blocks on `POST /completions` until Claude finishes. The server
spawns `AgentExecutor.execute()` directly and returns the full result — cost,
turns, session ID, tool uses — in the response body.

### Asynchronous (`wait=false`, legacy completions API)

The caller gets a `202 Queued` immediately with a `poll_url`. The request is
serialized and pushed onto a Redis list (`agenticore:cq`). A separate **worker
process** (`python -m agenticore.agent_mode`) pops requests and processes them.

```
Caller                    Redis Queue              Worker
  │                           │                       │
  │── POST /completions ──►   │                       │
  │◄── 202 {poll_url} ───    │                       │
  │                           │                       │
  │   LPUSH agenticore:cq ──►│                       │
  │                           │◄── BRPOP ────────────│
  │                           │                       │
  │                           │   AgentExecutor       │
  │                           │   spawns Claude       │
  │                           │                       │
  │── GET /completions/{uuid} │                       │
  │◄── {status, result, ...}  │                       │
```

When Redis is unavailable, the async path degrades gracefully: the completion
is executed inline as a background task with proper state tracking.

## Conversation Persistence

Agenticore maintains sticky Claude sessions across multi-turn conversations
without requiring clients to manage session IDs explicitly.

### 4-Tier Key Resolver (`conversation_key.py`)

For every request, `resolve_conversation_key()` derives a stable storage key
from the request headers and body, in priority order:

| Tier | Source | Example |
|------|--------|---------|
| `header` | `x-conversation-id`, `x-librechat-conversation-id`, `x-openwebui-chat-id` | LibreChat injects automatically |
| `body` | `metadata.conversation_id` or `body.user` (if UUID) | A2A callers, raw API |
| `content_hash` | SHA-256 of system prompt + first user message (first 16 hex chars) | Stateless curl clients |
| `ephemeral` | Random UUID — no session persistence | Single-turn requests |

The composed storage key format is `conv:{agent_id}:{user_hint}:{key}`, where
`user_hint` is extracted from `x-user-id` / `x-openwebui-user-id` headers or
the `user` body field.

### Session Lifecycle

On the first turn for a key, Agenticore generates a new Claude session UUID and
passes `--session-id <uuid>` to the subprocess. On subsequent turns it passes
`--resume <claude_session_id>` so Claude picks up the transcript. The Claude
session ID is stored in `session_registry` keyed on the conversation key.

```
Turn 1:  resolve conv_key → no session → spawn claude --session-id <new-uuid>
         → capture session_id from result event → store in registry

Turn 2+: resolve conv_key → lookup session_id → spawn claude --resume <session_id>
         → stream continues the existing session
```

When `tier == "ephemeral"` the request is treated as stateless — no session is
registered and each turn spawns a fresh Claude process.

### Redis Storage

| Key | Type | Description |
|-----|------|-------------|
| `conv:{agent_id}:{user}:{key}` | HASH | `claude_session_id`, `created_at`, `last_used` |

File fallback: `~/.agenticore/sessions/{conv_key_hash}.json`.

## Completion Lifecycle

```
         ┌────────┐
         │ queued  │ ── create_completion() + enqueue_completion()
         └───┬────┘
             │
      worker dequeues
             │
         ┌───▼────┐
         │ running │ ── update_completion(status="running")
         └───┬────┘
             │
      Claude executes
             │
     ┌───────┴───────┐
     │               │
 ┌───▼─────┐   ┌────▼───┐
 │completed│   │ failed  │
 └─────────┘   └─────────┘
```

**Completion data model:**

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | string | Caller-provided correlation ID |
| `status` | string | `queued`, `running`, `completed`, `failed` |
| `message` | string | The task/prompt |
| `result` | string | Claude's output text |
| `session_id` | string | Claude session ID |
| `cost_usd` | float | Total cost |
| `duration_ms` | int | Wall-clock execution time |
| `num_turns` | int | Agentic turns used |
| `is_error` | bool | Whether result is an error |
| `request_params` | dict | Full executor kwargs (for worker replay) |
| `created_at` | string | ISO 8601 timestamp |
| `started_at` | string | ISO 8601 timestamp |
| `ended_at` | string | ISO 8601 timestamp |

Stored as Redis hashes (`agenticore:completion:{uuid}`) with file fallback
(`~/.agenticore/completions/{uuid}.json`). Same Redis+file pattern as `jobs.py`.

## Stream Visibility (Slash Tokens)

The streaming path supports per-session visibility toggles via slash tokens
embedded in the last user message. Tokens are stripped server-side before
Claude sees them.

| Token | Effect |
|-------|--------|
| `/show-thinking` / `/hide-thinking` | Toggle extended thinking chunks |
| `/show-tools` / `/hide-tools` | Toggle `tool_use` + `tool_result` blocks |
| `/show-all` / `/hide-all` | Toggle all non-text content |
| `/stream-status` | Return current visibility config inline (no Claude spawn) |

Visibility config is stored in Redis at `agenticore:stream_config:{agent_id}`
(file fallback `~/.agenticore/stream_config/{agent_id}.json`). Defaults:
all content types visible (`show_all`).

When a request contains only slash tokens with no other message content,
Agenticore returns the resolved config as a `stream_config` meta SSE event
without spawning Claude.

## Package Directory

The package directory is the agent's identity. It follows the same `.claude/`
convention as agentihub packages:

```
/app/package/
├── CLAUDE.md             # System instructions (agent personality)
├── system.md             # System prompt (appended or replaced)
├── .claude/
│   ├── settings.json     # Permissions, hooks, tool allowlists
│   ├── hooks/            # Claude Code hooks (PostToolUse, etc.)
│   ├── agents/           # Custom subagents
│   └── skills/           # Custom slash commands
├── .mcp.json             # MCP server definitions
└── runners/              # Numbered startup scripts (00-install.sh, etc.)
```

At container startup, `initializer.py` runs four steps:

1. **Clone** package repo if `PACKAGE_REPO_URL` is set
2. **Validate** package directory exists
3. **Run** startup scripts from `runners/`
4. **Cache** system prompt from `system.md`

## API Surface

### POST /v1/chat/completions (OpenAI-compat, primary)

```json
{
    "model": "sonnet",
    "messages": [
        {"role": "user", "content": "Fix the login bug"}
    ],
    "stream": true,
    "user": "user-uuid-here",
    "metadata": {"conversation_id": "conv-abc123"},
    "disable_mcp_servers": ["tools-notifications"]
}
```

Returns OpenAI-format SSE when `stream=true`, or a single JSON response when
`stream=false`. Conversation key is resolved from headers/body automatically.

### POST /completions (legacy, async/sync)

```json
{
    "message": "Fix the login bug",
    "uuid": "correlation-123",
    "wait": false,
    "stateless": true,
    "model": "sonnet",
    "max_turns": 80,
    "meta": {"platform": "teams", "user": "john"}
}
```

**Response (`wait=false`):**

```json
{
    "success": true,
    "status": "queued",
    "uuid": "correlation-123",
    "poll_url": "/completions/correlation-123"
}
```

**Response (`wait=true`):** Direct result with `result`, `cost_usd`,
`duration_ms`, `num_turns`, `session_id`, etc.

### GET /completions/{uuid}

Poll for completion status and result.

```json
{
    "success": true,
    "completion": {
        "uuid": "correlation-123",
        "status": "completed",
        "result": "Fixed the login bug by...",
        "cost_usd": 0.12,
        "duration_ms": 45000,
        "num_turns": 5
    }
}
```

### GET /completions

List completions with optional `?status=` filter and `?limit=` param.

### MCP Tool: agent_completions

Same parameters as POST /completions, available as an MCP tool for AI clients.

## Worker Process

The worker is a standalone process that can run as a sidecar:

```bash
python -m agenticore.agent_mode
```

It runs a BRPOP loop against `agenticore:cq`, processes one completion at a
time (configurable via `AGENT_MODE_MAX_QUEUE_WORKERS`), and delivers results
to the completion store.

**Docker Compose sidecar:**

```yaml
worker:
    build:
        context: .
        dockerfile: docker/agent.dockerfile
    command: ["python", "-m", "agenticore.agent_mode"]
    environment:
        - AGENT_MODE=true
        - AGENT_MODE_PACKAGE_DIR=/app/package
        - REDIS_URL=redis://redis:6379/0
    depends_on:
        - redis
```

## Redis Key Structure

| Key | Type | TTL | Description |
|-----|------|-----|-------------|
| `agenticore:cq` | LIST | none | Completion queue (FIFO) |
| `agenticore:completion:{uuid}` | HASH | session_ttl | Completion state + result |
| `agenticore:events:{uuid}` | STREAM | session_ttl | Observability event bus (non-streaming path) |
| `agenticore:agent_state:{uuid}` | HASH | session_ttl | Hook context |
| `agenticore:stream_config:{agent_id}` | HASH | none | Stream visibility preferences |
| `conv:{agent_id}:{user}:{key}` | HASH | session_ttl | Conversation → Claude session mapping |

## File Fallback Matrix

| Component | Redis | File Fallback | No-Redis Behavior |
|-----------|-------|---------------|-------------------|
| Queue | LPUSH/BRPOP `agenticore:cq` | N/A | Inline execution |
| Completion Store | HASH `completion:{uuid}` | `~/.agenticore/completions/{uuid}.json` | File-only CRUD |
| Stream Config | HASH `stream_config:{agent_id}` | `~/.agenticore/stream_config/{agent_id}.json` | File-only |
| Conversation Sessions | HASH `conv:{agent_id}:*` | `~/.agenticore/sessions/*.json` | File-only |

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_MODE` | `false` | Enable agent mode |
| `AGENT_MODE_PACKAGE_DIR` | `/app/package` | Package directory path |
| `AGENT_MODE_MODEL` | `sonnet` | Default Claude model |
| `AGENT_MODE_MAX_TURNS` | `80` | Default agentic turn limit |
| `AGENT_MODE_TIMEOUT` | `3600` | Max execution time (seconds) |
| `AGENT_MODE_SESSION_TTL` | `86400` | Redis key TTL |
| `AGENT_MODE_QUEUE_ENABLED` | `true` | Enable completion queue |
| `AGENT_MODE_MAX_QUEUE_WORKERS` | `1` | Max concurrent worker tasks |
| `AGENT_MODE_PERMISSION_MODE` | `bypassPermissions` | Claude permission mode |
| `AGENT_MODE_APPEND_SYSTEM_PROMPT` | `true` | Append vs replace system.md |
| `AGENT_MODE_CONV_HASH_FALLBACK` | `true` | Enable content-hash tier in key resolver |
| `PACKAGE_REPO_URL` | _(empty)_ | Git URL to clone package from |
| `PACKAGE_REPO_BRANCH` | `main` | Branch to clone |

## Module Map

| Module | Purpose |
|--------|---------|
| `agent_mode/agent.py` | `AgentExecutor` — builds CLI command, runs subprocess, streams stdout |
| `agent_mode/completions.py` | `Completion` dataclass, Redis+file CRUD, queue LPUSH/BRPOP |
| `agent_mode/conversation_key.py` | 4-tier conversation key resolver for sticky session routing |
| `agent_mode/openai_compat.py` | OpenAI SSE chunk formatters, message flattening, request ID extraction |
| `agent_mode/stream_config.py` | Stream visibility config (thinking/tools/all), slash token parsing, Redis storage |
| `agent_mode/worker.py` | Standalone queue worker, `_process_completion()`, inline fallback |
| `agent_mode/initializer.py` | Package validation, startup scripts |
| `agent_mode/state.py` | Per-request state for hooks (uuid, wait mode, meta) |
| `agent_mode/session_registry.py` | Conversation key ↔ Claude session ID mapping |
| `agent_mode/session_manager.py` | Retry detection and composition |
| `agent_mode/event_tailer.py` | Non-streaming observability: tails Redis event bus |

## Relationship to Standard Mode

Agent Mode and Standard Mode are **complementary**, not competing:

| Concern | Standard Mode | Agent Mode |
|---------|---------------|------------|
| **Identity** | The repo | The package |
| **Config source** | agentihooks profiles | agentihub packages (provisioned by initializer.py) |
| **Lifecycle** | Per-job (materialize → execute → discard) | Per-container (mount → startup → serve) |
| **Output** | PR on a repo | SSE stream or completion result |
| **Execution** | `claude --worktree -p "task"` | `claude -p "task"` (no worktree) |
| **State** | Job store (`jobs.py`) | Completion store + session registry |
| **Async delivery** | Poll `GET /jobs/{id}` | Poll `GET /completions/{uuid}` |
| **Real-time events** | None | OpenAI-compat SSE stream |
| **API path** | `/jobs` | `/completions`, `/v1/chat/completions` |
| **MCP tool** | `run_task` | `agent_completions` |

Both share the same server process, same config system, same Redis+file
fallback pattern, and same profile/package directory convention from
agentihooks. An organisation can run both simultaneously — standard mode
for repo-based coding tasks, agent mode for conversational or task-specific
agents.
