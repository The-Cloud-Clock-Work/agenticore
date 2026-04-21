---
title: A2A Conventions
nav_order: 7
parent: Reference
---

# Agent-to-Agent Conversation Conventions

When one agent calls another via the OpenAI-compatible `/v1/chat/completions` endpoint, the caller should send these headers to enable conversation persistence:

## Required

```
X-Conversation-Id: <caller-chosen-uuid>
```

The caller generates a UUID for each logical conversation thread and reuses it across turns. The callee maps this to a Claude session and resumes it on subsequent requests.

## Optional

```
X-Caller-Agent: <agent-id>       # identifies the calling agent
X-User-Id: <original-user-id>    # propagate the end-user identity
X-Request-Id: <transport-uuid>   # per-request correlation (separate from conversation)
```

## Example

```bash
CONV=$(uuidgen)

# Turn 1: ask anton-agent a question
curl -X POST http://anton-agent.anton-dev.svc:8200/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "X-Conversation-Id: $CONV" \
  -H "X-Caller-Agent: brain-keeper" \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"set up a monitoring dashboard"}]}'

# Turn 2: follow up in the same conversation
curl -X POST http://anton-agent.anton-dev.svc:8200/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "X-Conversation-Id: $CONV" \
  -H "X-Caller-Agent: brain-keeper" \
  -H "Content-Type: application/json" \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"add alerting rules too"}]}'
```

## Body Alternative

If headers cannot be set (some HTTP clients), use:

```json
{
  "metadata": {
    "conversation_id": "<uuid>"
  }
}
```

This is Tier 2 in the [conversation resolver](conversation-persistence.md) — lower priority than headers but works identically.

## Notes

- Each agent maintains its own session namespace (`conv:{agent_id}:...`). The same `X-Conversation-Id` sent to different agents creates separate sessions.
- `X-User-Id` is used for key scoping — different users with the same conversation ID get different sessions.
- The callee sends only the last user message to Claude when resuming (prior context is in Claude's JSONL session).
