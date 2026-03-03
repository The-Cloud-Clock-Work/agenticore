---
title: Agent Mode
nav_order: 5
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
  Request → load package → claude -p "task" → result (+ notifications)
```

Both use the same `.claude/` directory convention. Both use the same
`settings.json`, `CLAUDE.md`, `.mcp.json` files. An agentihooks profile *can*
become an Agent Mode package — the structure is identical. The difference is
lifecycle and purpose.

### The Agentihooks Connection

[Agentihooks](https://github.com/The-Cloud-Clock-Work/agentihooks) is the
authoritative source for Claude Code configuration packages. It owns:

- **Profile authoring** — `profiles/_base/settings.base.json`, per-profile
  overrides, and `build_profiles.py` that merges them into deployable packages
- **Hook wiring** — permission rules, tool allowlists, custom hooks
- **MCP server categories** — which MCP servers each profile/package gets
- **System prompts** — `CLAUDE.md` and `system.md` templates

In standard mode, Agenticore discovers agentihooks profiles at
`{AGENTICORE_AGENTIHOOKS_PATH}/profiles/` and materializes them per-job.

In Agent Mode, the agentihooks output is the **package directory** — pre-built,
pre-wired, mounted at `/app/package`. The container starts, validates the
package, runs startup scripts, caches the system prompt, and waits for requests.

The build pipeline is the same. The deployment model differs.

## Architecture Overview

```
                   POST /completions
                         │
                   ┌─────▼─────┐
                   │  server.py │
                   │  (REST +   │
                   │   MCP)     │
                   └─────┬─────┘
                         │
              ┌──────────┴──────────┐
              │ wait=true           │ wait=false
              │                     │
              ▼                     ▼
     ┌────────────────┐    ┌───────────────┐
     │ AgentExecutor   │    │ Completion     │
     │ (inline, sync)  │    │ Queue          │
     │                 │    │ (Redis LPUSH)  │
     └────────┬───────┘    └───────┬───────┘
              │                     │
              │              ┌──────▼──────┐
              │              │   Worker     │
              │              │   (BRPOP)    │
              │              │ AgentExecutor│
              │              └──────┬──────┘
              │                     │
              └──────────┬──────────┘
                         │
                    ┌────▼────┐
                    │  Claude  │
                    │  Code    │
                    │  (subprocess)
                    └────┬────┘
                         │
              ┌──────────┼──────────┐
              │          │          │
              ▼          ▼          ▼
         PostToolUse  Thinking  Notification
              │          │          │
              └──────────┼──────────┘
                         │
                    hook_notifier.py
                         │
                    POST callback_url
                    (notification streaming)
```

## Two Execution Modes

### Synchronous (`wait=true`)

The caller blocks until Claude finishes. The server spawns
`AgentExecutor.execute()` directly and returns the full result — cost, turns,
session ID, tool uses — in the response body. This is the simple path: request
in, result out.

### Asynchronous (`wait=false`)

The caller gets a `202 Queued` immediately with a `poll_url`. The request is
serialized and pushed onto a Redis list (`agenticore:cq`). A separate **worker
process** (`python -m agenticore.agent_mode`) pops requests and processes them.
During execution, real-time events (tool calls, thinking, status changes) are
streamed to a `callback_url` via HTTP POST.

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
  │◄── POST callback (status: started) ──────────────│
  │◄── POST callback (tool_call: Write) ─────────────│  ← via hook
  │◄── POST callback (thinking: "analyzing...") ─────│  ← via hook
  │◄── POST callback (status: completed) ────────────│
  │◄── POST callback (result: {...}) ────────────────│
  │                           │                       │
  │── GET /completions/{uuid} │                       │
  │◄── {status, result, ...}  │                       │
```

When Redis is unavailable, the async path degrades gracefully: the completion
is executed inline as a background task (same as the old `asyncio.create_task`
behavior, but now with proper state tracking).

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
         └───┬────┘    ── notify(status: "started")
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
| `callback_url` | string | Webhook for notifications |
| `request_params` | dict | Full executor kwargs (for worker replay) |
| `created_at` | string | ISO 8601 timestamp |
| `started_at` | string | ISO 8601 timestamp |
| `ended_at` | string | ISO 8601 timestamp |

Stored as Redis hashes (`agenticore:completion:{uuid}`) with file fallback
(`~/.agenticore/completions/{uuid}.json`). Same Redis+file pattern as `jobs.py`.

## Notification Streaming

Notifications deliver real-time events to a `callback_url` during execution.
Three event types, each independently toggleable:

| Event Type | Source | Payload |
|------------|--------|---------|
| `status` | Worker process | `{status: "started\|completed\|failed"}` |
| `tool_call` | Claude Code `PostToolUse` hook | `{tool_name, file_path, description}` |
| `thinking` | Claude Code `SubprocessOutputLine` hook | `{content: "..."}` |
| `result` | Worker process (final) | `{result, cost_usd, duration_ms, ...}` |

**Notification envelope:**

```json
{
    "correlation_id": "uuid-123",
    "event_type": "tool_call",
    "timestamp": "2026-03-03T12:00:00Z",
    "data": {"tool_name": "Write", "file_path": "/src/app.py"}
}
```

**Configuration per request:**

```json
{
    "callback_url": "https://chat-app.example.com/webhook",
    "notifications": {"status": true, "tool_call": true, "thinking": false}
}
```

Notification preferences are stored per-correlation-ID (Redis hash +
file fallback) and can be toggled mid-execution via `PATCH
/completions/{uuid}/notifications`.

### Hook-Based Delivery

Tool call and thinking notifications come from **inside** the Claude subprocess
via Claude Code hooks. The `hook_notifier.py` script:

1. Is installed into the package's `.claude/hooks/` at startup by `initializer.py`
2. Is wired into `.claude/settings.json` for three hook events:
   `PostToolUse`, `SubprocessOutputLine`, `Notification`
3. Runs inside Claude's sandbox — **zero agenticore imports**, stdlib-only HTTP
4. Reads notification config from Redis (or file fallback)
5. Maps hook event to notification type, checks if enabled, POSTs to callback
6. Best-effort: catches all exceptions, exits 0 always

This design means notifications work even when the agent container has no
direct code path from Claude back to the server — the hook is self-contained.

## Package Directory

The package directory is the agent's identity. It follows the same `.claude/`
convention as agentihooks profiles:

```
/app/package/
├── CLAUDE.md             # System instructions (agent personality)
├── system.md             # System prompt (appended or replaced)
├── .claude/
│   ├── settings.json     # Permissions, hooks, tool allowlists
│   ├── hooks/
│   │   └── notifier.py   # ← installed by initializer.py at startup
│   ├── agents/           # Custom subagents
│   └── skills/           # Custom slash commands
├── .mcp.json             # MCP server definitions
└── runners/              # Numbered startup scripts (00-install.sh, etc.)
```

At container startup, `initializer.py` runs five steps:

1. **Clone** package repo if `PACKAGE_REPO_URL` is set
2. **Validate** package directory exists
3. **Run** startup scripts from `runners/`
4. **Cache** system prompt from `system.md`
5. **Install** notification hook into `.claude/hooks/`

## API Surface

### POST /completions

```json
{
    "message": "Fix the login bug",
    "uuid": "correlation-123",
    "wait": false,
    "callback_url": "https://app.example.com/webhook",
    "notifications": {"status": true, "tool_call": true},
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

### PATCH /completions/{uuid}/notifications

Toggle notification types mid-execution.

```json
{"tool_call": false, "thinking": true}
```

### MCP Tool: agent_completions

Same parameters as POST /completions, available as an MCP tool for AI clients.

## Worker Process

The worker is a standalone process that can run as a sidecar:

```bash
python -m agenticore.agent_mode
```

It runs a BRPOP loop against `agenticore:cq`, processes one completion at a
time (configurable via `AGENT_MODE_MAX_QUEUE_WORKERS`), and delivers results
to both the completion store and the callback URL.

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
| `agenticore:notification:{uuid}` | HASH | session_ttl | Notification preferences |
| `agenticore:agent_state:{uuid}` | HASH | session_ttl | Hook context (existing) |

## File Fallback Matrix

| Component | Redis | File Fallback | No-Redis Behavior |
|-----------|-------|---------------|-------------------|
| Queue | LPUSH/BRPOP `agenticore:cq` | N/A | Inline execution |
| Completion Store | HASH `completion:{uuid}` | `~/.agenticore/completions/{uuid}.json` | File-only CRUD |
| Notification Config | HASH `notification:{uuid}` | `~/.agenticore/notification_configs.json` | File-only |
| Hook Config Read | Redis HGETALL | `notification_configs.json` | File-only |

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
| `AGENT_MODE_NOTIFICATION_TIMEOUT` | `5` | HTTP delivery timeout (seconds) |
| `AGENT_MODE_MAX_QUEUE_WORKERS` | `1` | Max concurrent worker tasks |
| `AGENT_MODE_DEFAULT_NOTIFICATIONS` | `status` | Default notification types |
| `AGENT_MODE_PERMISSION_MODE` | `bypassPermissions` | Claude permission mode |
| `AGENT_MODE_APPEND_SYSTEM_PROMPT` | `true` | Append vs replace system.md |
| `PACKAGE_REPO_URL` | _(empty)_ | Git URL to clone package from |
| `PACKAGE_REPO_BRANCH` | `main` | Branch to clone |

## Module Map

| Module | Purpose |
|--------|---------|
| `agent_mode/agent.py` | `AgentExecutor` — builds CLI command, runs subprocess, parses output |
| `agent_mode/completions.py` | `Completion` dataclass, Redis+file CRUD, queue LPUSH/BRPOP |
| `agent_mode/notifications.py` | `NotificationConfig` management, HTTP delivery |
| `agent_mode/worker.py` | Standalone queue worker, `_process_completion()`, inline fallback |
| `agent_mode/hook_notifier.py` | Self-contained Claude Code hook (stdlib-only) |
| `agent_mode/initializer.py` | Package validation, startup scripts, hook installation |
| `agent_mode/state.py` | Per-request state for hooks (uuid, wait mode, meta) |
| `agent_mode/session_registry.py` | Claude session ID ↔ external UUID mapping |
| `agent_mode/session_manager.py` | Retry detection and composition |

## Relationship to Standard Mode

Agent Mode and Standard Mode are **complementary**, not competing:

| Concern | Standard Mode | Agent Mode |
|---------|---------------|------------|
| **Identity** | The repo | The package |
| **Config source** | agentihooks profiles | agentihooks packages |
| **Lifecycle** | Per-job (materialize → execute → discard) | Per-container (mount → startup → serve) |
| **Output** | PR on a repo | Completion result (text, cost, metadata) |
| **Execution** | `claude --worktree -p "task"` | `claude -p "task"` (no worktree) |
| **State** | Job store (`jobs.py`) | Completion store (`completions.py`) |
| **Async delivery** | Poll `GET /jobs/{id}` | Poll or callback webhook |
| **Real-time events** | None | Notification streaming |
| **API path** | `/jobs` | `/completions` |
| **MCP tool** | `run_task` | `agent_completions` |

Both share the same server process, same config system, same Redis+file
fallback pattern, and same profile/package directory convention from
agentihooks. An organisation can run both simultaneously — standard mode
for repo-based coding tasks, agent mode for conversational or task-specific
agents.
