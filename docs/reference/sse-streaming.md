---
title: SSE Streaming
nav_order: 4
---

# SSE Streaming

Real-time event streaming for the `/v1/chat/completions` endpoint when `stream=true`. Thinking blocks, tool calls, tool results, and assistant text arrive as live SSE deltas on the same open HTTP connection as the agent produces them — not batched at the end.

## TL;DR

```bash
kubectl port-forward -n anton-dev svc/<agent> 8200:8200 &

# Enable everything (sticky per agent, persists across calls)
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"/show-all"}]}'

# Have a conversation — watch thinking + tool calls stream live
curl -sN http://localhost:8200/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"sonnet","stream":true,"messages":[{"role":"user","content":"List files in /tmp and tell me what you see."}]}'
```

## How it works

When you `POST /v1/chat/completions` with `stream: true`, agenticore:

1. Intercepts any slash tokens in the prompt (see below), strips them, persists the visibility config per agent
2. Spawns the Claude subprocess in background (does not block on completion)
3. Returns a `StreamingResponse` that tails `agenticore:events:{uuid}` on Redis via XREAD
4. As agentihooks hooks fire inside the subprocess, they XADD structured events to that Redis stream
5. Each visible event is mapped to an OpenAI-compatible delta chunk and yielded to the client immediately
6. Stream closes with `data: [DONE]` when the subprocess exits

Non-streaming (`stream: false`) is unchanged — still returns a single `chat.completion` JSON object.

## Slash tokens (visibility toggles)

These are **pseudo-slash commands** embedded in the user message. agenticore strips them before Claude ever sees the prompt, so they are deterministic — the LLM cannot hallucinate, misinterpret, or refuse them.

| Token | Effect |
|---|---|
| `/show-thinking` | Include extended-thinking deltas in the stream |
| `/hide-thinking` | Exclude thinking deltas (default) |
| `/show-tools` | Include tool_use + tool_result deltas |
| `/hide-tools` | Exclude tool deltas (default) |
| `/show-all` | Enable thinking + tools + text |
| `/hide-all` | Back to assistant text only |
| `/stream-status` | Respond inline with current visibility state (no subprocess spawned) |

**Sticky per agent.** The toggle is persisted to Redis at `agenticore:stream_config:{AGENTIHUB_AGENT}` with no TTL. Once you send `/show-thinking` to agent `X`, every subsequent streaming call to that agent includes thinking deltas until you send `/hide-thinking` or `/hide-all`.

**Default visibility** for a new agent: `assistant_text` only. Thinking and tools are opt-in.

Tokens can appear anywhere in the message, mixed with normal text:

```json
{"messages":[{"role":"user","content":"explain X step by step /show-thinking"}]}
```

Unknown `/tokens` pass through untouched (they are not intercepted, Claude sees them normally).

## SSE chunk types

Every chunk is a standard OpenAI `chat.completion.chunk` JSON object prefixed with `data: `. Non-standard event types are identified by the `x_agenticore_event_type` field in `choices[0]`.

### Role open (first chunk)
```
data: {"id":"...","object":"chat.completion.chunk","model":"sonnet",
       "choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}
```

### Thinking delta
```
data: {"choices":[{"index":0,
  "delta":{"content":"Let me break this down..."},
  "finish_reason":null,
  "x_agenticore_event_type":"thinking"}]}
```

### Tool use delta (maps to OpenAI `tool_calls` schema)
```
data: {"choices":[{"index":0,
  "delta":{"tool_calls":[{"index":0,"id":"toolu_01...","type":"function",
    "function":{"name":"Bash","arguments":"{\"command\":\"ls /tmp\"}"}}]},
  "finish_reason":null,
  "x_agenticore_event_type":"tool_use"}]}
```

### Tool result delta (custom — OpenAI has no native tool_result delta)
```
data: {"choices":[{"index":0,
  "delta":{"content":"file1.txt\nfile2.log\n"},
  "finish_reason":null,
  "x_agenticore_event_type":"tool_result",
  "x_agenticore_tool_use_id":"toolu_01...",
  "x_agenticore_is_error":false}]}
```

### Assistant text delta
```
data: {"choices":[{"index":0,
  "delta":{"content":"I see two files: ..."},
  "finish_reason":null}]}
```

### Stream status meta (response to `/stream-status`)
```
data: {"choices":[{"index":0,
  "delta":{"content":"{\"show_thinking\":true,\"show_tools\":true,\"show_text\":true}"},
  "finish_reason":null,
  "x_agenticore_event_type":"stream_config"}]}
```

### Stop chunk + done marker (always last two)
```
data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],
       "usage":{"prompt_tokens":4,"completion_tokens":188,"total_tokens":192}}

data: [DONE]
```

## Client filtering

If you're writing a UI that renders these in a chat window, filter client-side by the event type marker:

```javascript
const resp = await fetch('/v1/chat/completions', {...});
const reader = resp.body.getReader();
const decoder = new TextDecoder();

while (true) {
  const {done, value} = await reader.read();
  if (done) break;
  const lines = decoder.decode(value).split('\n');
  for (const line of lines) {
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice(6);
    if (payload === '[DONE]') return;
    const chunk = JSON.parse(payload);
    const choice = chunk.choices?.[0] ?? {};
    const eventType = choice.x_agenticore_event_type;
    const delta = choice.delta ?? {};

    if (eventType === 'thinking') {
      renderThinkingPanel(delta.content);
    } else if (eventType === 'tool_use') {
      renderToolCall(delta.tool_calls[0]);
    } else if (eventType === 'tool_result') {
      renderToolResult(delta.content, choice.x_agenticore_tool_use_id);
    } else if (delta.content) {
      appendAssistantText(delta.content);
    }
  }
}
```

## Using the OpenAI SDK

Works with any OpenAI-compatible client as long as you ignore the `x_agenticore_event_type` fields or filter on them:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8200/v1", api_key="n/a")

# Toggle on once (sticky)
client.chat.completions.create(
    model="sonnet", stream=True,
    messages=[{"role": "user", "content": "/show-thinking"}],
)

# Now every stream surfaces thinking
stream = client.chat.completions.create(
    model="sonnet", stream=True,
    messages=[{"role": "user", "content": "plan a refactor step by step"}],
)
for chunk in stream:
    choice = chunk.choices[0]
    event_type = getattr(choice, "x_agenticore_event_type", None)
    if event_type == "thinking":
        print(f"[thinking] {choice.delta.content}")
    elif event_type == "tool_use":
        call = choice.delta.tool_calls[0]
        print(f"[tool {call.function.name}] {call.function.arguments}")
    elif event_type == "tool_result":
        print(f"[result] {choice.delta.content[:200]}")
    elif choice.delta.content:
        print(choice.delta.content, end="", flush=True)
```

## Pipeline architecture

```
HTTP client ─POST /v1/chat/completions stream=true─► agenticore
                                                       │
                                                       ├─ stream_config.get_for_request
                                                       │     (strip slash tokens, load sticky state)
                                                       │
                                                       ├─ AgentExecutor.execute_streaming
                                                       │     ├─ spawn claude subprocess
                                                       │     │     env: AGENTICORE_CORRELATION_ID, AGENTICORE_EVENT_STREAM=1
                                                       │     └─ tail Redis stream agenticore:events:{uuid}
                                                       │           via XREAD BLOCK
                                                       │
                                                       └─ StreamingResponse(generator) ◄── held open, flushed per chunk

Meanwhile inside the subprocess:
  Claude runs → hook fires on PostToolUse / Stop / Notification →
    agentihooks/hooks/observability/event_relay.py →
      parse hook payload + transcript JSONL →
        XADD agenticore:events:{correlation_id} MAXLEN 2000
```

Event stream key: `agenticore:events:{correlation_uuid}` (TTL 1h after completion).
Sticky config key: `agenticore:stream_config:{AGENTIHUB_AGENT}` (no TTL).

## Auditing a live agent

To verify the pipeline is working end-to-end on any agent pod, use the audit script:

```bash
./tests/smoke/verify_streaming_pipeline.sh <agent-name>
```

Runs a deterministic conversation, cross-validates events across four layers (client SSE, Redis stream, pod logs, claude transcript), and writes timestamped artifacts to `/tmp/sse-audit/<run-id>/` for later review. Exit 0 = PASS with all 13 checks green.

Replay a past run from disk (no network):
```bash
./tests/smoke/verify_streaming_pipeline.sh <agent> --replay <run-id>
```

See [`tests/smoke/verify_streaming_pipeline.sh`](https://github.com/The-Cloud-Clock-Work/agenticore/blob/dev/tests/smoke/verify_streaming_pipeline.sh) for details.

## Fail modes and diagnostics

| Symptom | What's broken | How to check |
|---|---|---|
| `role_open` + `stop` + `[DONE]` only, no events in between | Hook isn't publishing to Redis | `kubectl exec <pod> -- ls /shared/agentihooks/hooks/observability/event_relay.py` |
| Nothing at all, just timeout | Subprocess spawn failed | pod logs for `Pre-call MCP render` then no subsequent activity |
| Thinking never shows even with `/show-thinking` | Sonnet didn't emit thinking for that prompt (not a bug) | Try a harder prompt — sonnet only thinks when needed |
| Tool events appear but truncated | That's the banner — real tool output follows | Read past the shell profile banner in the content field |
| 401 unauthorized | Auth required on this pod | Set `AGENTICORE_API_KEYS` or add `Authorization: Bearer $KEY` |
| `x_agenticore_event_type` never appears | Pod runs pre-feature image | Check `kubectl get pod <agent>-0 -o jsonpath='{.status.containerStatuses[0].imageID}'` against GHCR `:dev` |

## Related

- [`docs/architecture/agent-mode.md`]({% link architecture/agent-mode.md %}) — agent mode overview
- [`docs/reference/api-reference.md`]({% link reference/api-reference.md %}) — full API surface
- [`docs/getting-started/test-streaming.md`]({% link getting-started/test-streaming.md %}) — step-by-step self-test
