---
title: Conversation Persistence
nav_order: 6
parent: Reference
---

# Conversation Persistence

Agenticore's `/v1/chat/completions` endpoint supports sticky multi-turn sessions. The same Claude session (with KV-cache, prompt cache, and full conversation history) is reused across turns of the same logical conversation.

## How It Works

Every request passes through a **4-tier conversation key resolver** that infers conversation identity from the request:

| Tier | Source | Mechanism |
|---|---|---|
| 1 — Header | `X-Conversation-Id`, `X-LibreChat-Conversation-Id`, `X-OpenWebUI-Chat-Id` | Explicit, highest priority |
| 2 — Body | `body.metadata.conversation_id` or `body.user` (UUID-shaped only) | SDK / A2A convention |
| 3 — Content hash | `sha256(system_prompt + first_user_message)[:16]` | Zero-config fallback |
| 4 — Ephemeral | `uuid4()` | Stateless, one-shot |

The composed storage key is `conv:{agent_id}:{user_hint}:{key}`, scoped by agent and user to prevent cross-contamination.

## First Turn vs Resume

- **First turn** (no existing session): agenticore generates a Claude session UUID, passes `--session-id <uuid>` to create a persistent session, and stores the mapping in Redis + file fallback.
- **Subsequent turns** (session exists): agenticore passes `--resume <uuid>` and sends **only the last user message** (Claude already has prior context from the JSONL).

## Client Configuration

### LibreChat

Add `headers` to your custom endpoint in `librechat.yaml`:

```yaml
endpoints:
  custom:
    - name: "LiteLLM"
      headers:
        X-Conversation-Id: "{% raw %}{{conversationId}}{% endraw %}"
        X-User-Id: "{% raw %}{{user}}{% endraw %}"
```

### Raw curl

```bash
CONV=$(uuidgen)
# Turn 1
curl -N -H "X-Conversation-Id: $CONV" \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"my name is colt"}]}' \
  http://localhost:8200/v1/chat/completions
# Turn 2 — same conv ID, claude remembers
curl -N -H "X-Conversation-Id: $CONV" \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"what is my name?"}]}' \
  http://localhost:8200/v1/chat/completions
```

### Agent-to-Agent

See [A2A Conventions](a2a-conventions.md).

### No Header (Tier 3 Fallback)

If the client sends no conversation header, agenticore hashes `system_prompt + first_user_message` to derive a stable key. Clients that replay full `messages[]` history each turn (standard OpenAI-compat behavior) will hit the same session automatically.

**Collision risk**: two different threads with identical system prompt AND first user message will merge. Disable with `AGENTICORE_CONV_HASH_FALLBACK=false`.

## Configuration

| Env Var | Default | Description |
|---|---|---|
| `AGENTICORE_CONV_HASH_FALLBACK` | `true` | Enable/disable Tier 3 content-hash fallback |
| `AGENT_MODE_SESSION_TTL` | `86400` | TTL in seconds for session mappings in Redis |

## Storage

- **Redis**: `agenticore:session:conv:{agent_id}:{user}:{key}` hash with TTL
- **File fallback**: `~/.agenticore/agent_sessions.json`
- **Claude JSONL**: `~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl` (on the agent pod's PVC)

## Observability

Every request logs: `conv_key=... tier=... agent=... stateless=...`

Filter logs by tier to see which clients are using which resolution strategy:
```bash
kubectl logs anton-agent-0 -c agenticore | grep 'tier='
```
