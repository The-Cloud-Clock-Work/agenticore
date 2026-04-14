---
title: SSE Streaming
nav_order: 4
---

# SSE Streaming

Real-time event streaming for the `/v1/chat/completions` endpoint when `stream=true`. **Thinking blocks, tool calls, tool results, and assistant text arrive token-by-token** as live SSE deltas on the same open HTTP connection — not batched at the end of the turn.

This turns every agenticore-backed agent into a **fully auditable and traceable agent**: any chat client (LibreChat, OpenWebUI, custom UI, raw `curl -N`) can watch the agent's reasoning, tool invocations, tool results, and final answer **as they happen**, in OpenAI-compatible SSE chunks, with deterministic visibility controls. Every event the model produces is observable on the wire, on disk (transcript), and in Redis (when needed for cross-process consumers) — three independent layers that can be cross-validated via the bundled audit script.

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
2. Spawns the Claude subprocess with `--output-format stream-json --verbose --include-partial-messages` so claude emits one raw API event per stdout line as the model generates each token
3. Reads `proc.stdout` line-by-line in an async loop, parses each JSONL event, and dispatches:
   - `thinking_delta` → `delta.reasoning_content` (rendered in the client's reasoning panel)
   - `text_delta` → `delta.content` (assistant text)
   - `content_block_start` + `input_json_delta` → accumulates tool_use args until `content_block_stop`, then emits a fenced ` ```tool_use:NAME ` markdown block
   - `tool_result` (returned in the next user-role message) → fenced ` ```tool_result ` block paired below the call
4. Filters every event through the sticky visibility config (`is_visible`) before yielding
5. On `result` event: captures usage tokens, yields a stop chunk, then `data: [DONE]`

No transcript polling, no Redis event bus, no JSONL flush race — the streaming hot path reads claude's stdout pipe directly. Thinking tokens reach the client in the same instant the model emits them.

Non-streaming (`stream: false`) is unchanged — still returns a single `chat.completion` JSON object built from the buffered final result.

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

### Thinking delta (streamed token-by-token)
Uses `delta.reasoning_content` so OpenAI-compatible reasoning-aware clients (LibreChat, OpenWebUI, etc.) render thinking in a dedicated reasoning panel separate from the assistant text.
```
data: {"choices":[{"index":0,
  "delta":{"reasoning_content":"Let me break"},
  "finish_reason":null,
  "x_agenticore_event_type":"thinking"}]}
```

### Tool use delta (rendered as fenced markdown)
Emitted once per tool call when the input JSON is fully assembled. Uses `delta.content` with a ` ```tool_use:NAME ` fence so chat clients render it inline as a code block. We deliberately do **not** use OpenAI's `delta.tool_calls` schema — that would tell the client to execute the function locally, and clients without agenticore's tool registry fail with "Tool not found".
```
data: {"choices":[{"index":0,
  "delta":{"content":"\n\n```tool_use:Bash\n{\n  \"command\": \"ls /tmp\"\n}\n```\n"},
  "finish_reason":null,
  "x_agenticore_event_type":"tool_use",
  "x_agenticore_tool_name":"Bash",
  "x_agenticore_tool_use_id":"toolu_01..."}]}
```

### Tool result delta
Wrapped in a ` ```tool_result ` fenced block (or ` ```tool_result:error `) so it visually pairs with the preceding `tool_use` block.
```
data: {"choices":[{"index":0,
  "delta":{"content":"\n```tool_result\nfile1.txt\nfile2.log\n```\n"},
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
      // delta.reasoning_content is the thinking token (token-by-token)
      renderThinkingPanel(delta.reasoning_content);
    } else if (eventType === 'tool_use') {
      // delta.content holds the fenced ```tool_use:NAME block
      renderToolCall(delta.content, choice.x_agenticore_tool_name);
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
    delta = choice.delta
    if event_type == "thinking":
        # reasoning_content is a custom field; pull it off the raw model_dump
        thought = getattr(delta, "reasoning_content", None) or delta.model_dump().get("reasoning_content", "")
        print(f"[thinking] {thought}", end="", flush=True)
    elif event_type == "tool_use":
        print(f"[tool] {delta.content}")
    elif event_type == "tool_result":
        print(f"[result] {delta.content[:200]}")
    elif delta.content:
        print(delta.content, end="", flush=True)
```

## Pipeline architecture

```
HTTP client ─POST /v1/chat/completions stream=true─► agenticore
                                                       │
                                                       ├─ stream_config.get_for_request
                                                       │     (strip slash tokens, load sticky state)
                                                       │
                                                       ├─ AgentExecutor.execute_streaming
                                                       │     ├─ spawn claude with --output-format stream-json
                                                       │     │                    --verbose --include-partial-messages
                                                       │     ├─ async loop: read proc.stdout line-by-line
                                                       │     │     parse each JSONL stream_event
                                                       │     │     dispatch to format_*_delta
                                                       │     │     filter through is_visible(event_type, stream_cfg)
                                                       │     └─ on `result` event: emit stop chunk + [DONE]
                                                       │
                                                       └─ StreamingResponse(generator) ◄── held open, flushed per token
```

Three observation surfaces are populated for every streaming call:

1. **Wire**: every visible token reaches the HTTP client as an OpenAI-format SSE chunk
2. **Disk**: claude's transcript JSONL is still written to `~/.claude/projects/<encoded>/<session>.jsonl` for the post-mortem audit trail (see audit script below)
3. **Redis** *(non-streaming path only)*: agentihooks `event_relay.py` continues to XADD events to `agenticore:events:{correlation_uuid}` (MAXLEN 2000, TTL 1h after `done` sentinel) for cross-process consumers like the brain bus

The streaming hot path bypasses Redis entirely — there is no XADD/XREAD round-trip in the critical path. The Redis bus is preserved for the non-streaming `execute()` path and any fleet-wide observability subscribers that want to tail multiple agents at once.

Sticky config key: `agenticore:stream_config:{AGENTIHUB_AGENT}` (no TTL, file fallback at `~/.agenticore/stream_config/{agent_id}.json`).

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

- [`docs/architecture/agent-mode.md`]({% link docs/architecture/agent-mode.md %}) — agent mode overview
- [`docs/reference/api-reference.md`]({% link docs/reference/api-reference.md %}) — full API surface
- [`docs/getting-started/test-streaming.md`]({% link docs/getting-started/test-streaming.md %}) — step-by-step self-test

## Milestones

### 2026-04-14 — 100% green: token-by-token thinking via stream-json (`d72c201`)

`feat/stream-json-direct` merged into `dev`. The streaming hot path now reads claude's stdout directly with `--output-format stream-json --verbose --include-partial-messages`, dispatching `thinking_delta` / `text_delta` / `tool_use` / `tool_result` events to SSE formatters as they arrive on the pipe. Validated 6/6 in LibreChat against `streaming-test`:

1. `/show-all` → inline `{"show_thinking":true,"show_tools":true,"show_text":true}` meta
2. `/stream-status` → same inline meta on a multi-turn conversation (turn 2+)
3. `is 17077 prime? think hard` → thinking renders **token-by-token** in LibreChat's reasoning panel as the model generates
4. `run bash: ls -lh /tmp` → tool_use + tool_result fenced blocks stream live, then assistant summary
5. `/hide-tools` then `run bash: date` → tool blocks suppressed, only the assistant text reaches the client
6. Sticky toggles persist across turns and across pod restarts (Redis-backed)

What this unlocks: every agenticore-backed agent is now a **fully auditable, traceable, real-time observable agent**. A chat client holds a single open HTTP connection and watches the agent's reasoning, tool calls, tool results, and final answer flow through in OpenAI-compatible SSE chunks, with deterministic per-agent visibility controls and zero LLM-side ambiguity (slash tokens are stripped server-side before claude ever sees them).

Pipeline images at this milestone:
```
ghcr.io/the-cloud-clock-work/agenticore:dev-d72c201
ghcr.io/the-cloud-clock-work/agenticore:dev   (floating)
```

### 2026-04-14 — 95% green in LibreChat (`b88b3e8`)

Validated end-to-end on `llm.dev.homeofanton.com` via LibreChat against `anton-agent`, `finops-agent`, `notebooklm-agent`:

- All seven slash tokens intercepted server-side, sticky per agent, multi-turn aware, no Claude spawn.
- Thinking rendered in LibreChat's reasoning panel via `delta.reasoning_content`.
- Tool calls + results rendered as fenced markdown blocks (not OpenAI `delta.tool_calls`).

Known gap at this milestone: thinking arrived in one delta at the end of the turn, not progressively token-by-token, because the pipeline still used the transcript-JSONL hook + Redis relay path. Closed by the `d72c201` milestone above.
