---
title: Progress Sink
parent: Architecture
nav_order: 9
---
# ProgressSink — canonical progress-event contract for connectors

## Why this exists

Without this abstraction, every connector (Telegram, Slack, Teams, Discord,
custom HTTP relays) has to rediscover how to stream an agent turn: when to
show narration, how to tag the final answer, whether to render tool calls,
how to rate-limit edits. The result is drift — the Telegram bot appears
silent during long multi-tool turns while the same agent streams progress
fine through SSE, because the two consumption paths evolved separately.

`ProgressSink` collapses that: a single canonical event vocabulary, emitted
once by `AgentExecutor`, consumed identically by every transport. Write a
30-line sink subclass and the connector inherits interleaved narration,
final-answer tagging, tool-call chips, and centralized visibility control.

## Canonical event vocabulary

| Type            | Payload                                    | Meaning                                                                 |
|-----------------|--------------------------------------------|-------------------------------------------------------------------------|
| `narration`     | `text: str`                                | Interleaved assistant text between tool calls ("checking logs…")        |
| `final`         | `text: str`                                | The authoritative final answer. Fires exactly once per turn             |
| `tool_call`     | `name, args, tool_use_id`                  | The agent invoked a tool                                                |
| `tool_result`   | `tool_use_id, content, is_error`           | Tool output paired with its call                                        |
| `thinking`      | `text: str`                                | Extended-thinking reasoning delta                                       |
| `usage`         | `input_tokens, output_tokens, ...`         | Token counts and stop metadata, end of turn                             |
| `error`         | `message, code`                            | Subprocess failure, parse error, or upstream error. Turn is terminated  |
| `done`          | `{}`                                       | Internal sentinel — no sink method                                      |

Final vs narration distinction: in Anthropic's streaming protocol every
assistant text is just a `text` content block. The agenticore translator
tags the **last closed text block** of the turn as `final`; everything
earlier is `narration`. This means:

- A turn can have 0, 1, or many `narration` events.
- A turn has **at most one** `final` event (none if the turn ends after a
  tool call with no trailing text).
- `final` fires **after** the narration of the same text, so connectors
  that track narration live still get the authoritative finish signal.

## ProgressSink contract

```python
from agenticore.connectors.base import ProgressSink

class MySink(ProgressSink):
    async def on_narration(self, text: str) -> None: ...
    async def on_final(self, text: str) -> None: ...
    async def on_tool_call(self, name: str, args: dict, tool_use_id: str) -> None: ...
    async def on_tool_result(self, tool_use_id: str, content: str, is_error: bool) -> None: ...
    async def on_thinking(self, text: str) -> None: ...
    async def on_usage(self, usage: dict) -> None: ...
    async def on_error(self, message: str, code=None) -> None: ...
```

All callbacks are coroutines. Default implementations are no-ops — override
only what you render. Callbacks must not block: if you need slow I/O (a
rate-limited API edit, audio synthesis), use background tasks or internal
debouncing. Visibility is already filtered against the agent's `stream_config`
before events reach the sink.

### Guarantees

| Guarantee                                     | Enforced by                         |
|-----------------------------------------------|-------------------------------------|
| `on_final` fires at most once per turn        | `OnceSink` (wraps user sink)        |
| `on_usage` fires at most once per turn        | `OnceSink`                          |
| `on_error` fires at most once per turn        | `OnceSink`                          |
| `on_final` OR `on_error` always fires         | Subprocess fallback in `_run_subprocess` |
| Events ordered as emitted by the translator   | Single-task dispatch loop           |
| Sink exceptions do not halt the pipeline      | `dispatch_to_sink` swallows + logs  |

## Attaching a sink

```python
from agenticore.agent_mode.agent import AgentExecutor

executor = AgentExecutor()
sink = MySink(...)

result = await executor.execute(
    message="do the thing",
    external_uuid="user-123",
    sink=sink,              # NEW — canonical events dispatched here
    stream_cfg=None,        # optional; otherwise loaded from AGENTIHUB_AGENT
)
```

`result` has the same shape as before: `{result, session_id, cost_usd,
duration_ms, num_turns, is_error, usage, tool_uses}`. The sink is a
**parallel** consumer — it sees the intermediate events as they land, while
`result["result"]` still gives you the aggregated final string for logging,
TTS, or replay.

## Visibility — `stream_config`

Sink callbacks are gated by the agent-scoped `stream_config` hash in Redis,
keyed `agenticore:stream_config:{AGENTIHUB_AGENT}`. Keys:

| Key               | Default | Gates                       |
|-------------------|---------|-----------------------------|
| `show_narration`  | `True`  | `narration`                 |
| `show_final`      | `True`  | `final`                     |
| `show_tools`      | `False` | `tool_call`, `tool_result`  |
| `show_thinking`   | `False` | `thinking`                  |
| `show_text`       | `True`  | legacy fallback for both narration + final when they aren't explicitly set |

Slash tokens (`/show-narration`, `/hide-narration`, `/show-final`,
`/hide-final`, `/show-all`, `/hide-all`, …) in the last user message toggle
these at runtime; the change persists stickily across calls for that
`AGENTIHUB_AGENT`. See `docs/reference/sse-streaming.md` for the complete
token list.

Connectors never filter — filtering happens once, inside the executor.
Flipping `show_narration=false` at the agent level turns narration off in
SSE, Telegram, Slack, and any future connector simultaneously.

## Writing a new connector — ~30 lines

```python
from agenticore.connectors.base import ProgressSink
from agenticore.agent_mode.agent import AgentExecutor

class SlackProgressSink(ProgressSink):
    def __init__(self, web_client, channel, thread_ts):
        self._web = web_client
        self._channel = channel
        self._thread_ts = thread_ts
        self._live_ts = None
        self._buffer = ""

    async def on_narration(self, text):
        self._buffer += text
        if self._live_ts is None:
            resp = await self._web.chat_postMessage(
                channel=self._channel, text=self._buffer, thread_ts=self._thread_ts)
            self._live_ts = resp["ts"]
        else:
            await self._web.chat_update(
                channel=self._channel, ts=self._live_ts, text=self._buffer[-3000:])

    async def on_final(self, text):
        await self._web.chat_postMessage(
            channel=self._channel, text=text, thread_ts=self._thread_ts)

    async def on_error(self, message, code=None):
        await self._web.chat_postMessage(
            channel=self._channel, text=f":warning: {message}", thread_ts=self._thread_ts)
```

That's the whole thing. `on_tool_call` / `on_tool_result` / `on_thinking`
stay as no-ops until the connector wants to render them.

## Why a sink and not a generator?

A `ProgressSink` subclass is the right shape because connectors are
state-heavy: Telegram tracks a live message ID, Slack tracks a thread
timestamp, Teams tracks an Adaptive Card activity ID. An async-generator
interface forces the connector to consume events in a loop, losing access to
`self`. The sink is callback-style — the executor drives the loop, the sink
holds the state, and every callback has a clean single-responsibility shape.

SSE `/v1/chat/completions` is the one consumer that actually is a generator
— it stays that way via `execute_streaming()`, which runs the same
canonical translator and emits SSE chunks instead of calling sink methods.

## Related files

- `agenticore/agent_mode/progress.py` — `ProgressEvent`, `ProgressEventType`,
  `CanonicalTranslator`, `OnceSink`, `dispatch_to_sink`,
  `tail_canonical_from_redis`
- `agenticore/connectors/base.py` — `ProgressSink`, `NullSink`
- `agenticore/connectors/telegram.py` — `TelegramProgressSink`, reference
  implementation
- `agenticore/agent_mode/agent.py` — `AgentExecutor.execute(..., sink=...)`
- `agenticore/agent_mode/stream_config.py` — visibility config + slash tokens
- `docs/reference/sse-streaming.md` — SSE chunk schema
