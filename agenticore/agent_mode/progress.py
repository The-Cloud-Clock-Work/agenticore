"""Canonical progress-event pipeline shared by every execution path.

Two raw sources feed into one translator:

1. ``claude -p --output-format stream-json`` stdout (``execute_streaming``)
2. The Redis event stream published by the ``event_relay`` hook
   (``execute`` with a ProgressSink attached).

Both sources produce the same raw event vocabulary (``thinking``,
``tool_use``, ``tool_result``, ``assistant_text``, ``done``, ...), which the
shared state machine converts into canonical ProgressEvents
(``NARRATION`` / ``FINAL`` / ``TOOL_CALL`` / ``TOOL_RESULT`` / ``THINKING``
/ ``USAGE`` / ``ERROR`` / ``DONE``).

The final-vs-narration disambiguation runs in the translator, not per-source:
the last closed text block of the turn is labelled ``FINAL``; all other text
is labelled ``NARRATION``. This means every connector — SSE, Telegram, Slack,
Teams, anything — receives identical event types with identical semantics,
independent of how the raw events reached the executor.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import AsyncIterator, Iterable, Optional

_log = logging.getLogger(__name__)


class ProgressEventType(str, Enum):
    NARRATION = "narration"
    FINAL = "final"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    THINKING = "thinking"
    USAGE = "usage"
    ERROR = "error"
    DONE = "done"


@dataclass
class ProgressEvent:
    """A canonical event delivered to every consumer (SSE adapter or sink)."""

    type: ProgressEventType
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    correlation_id: str = ""
    session_id: str = ""


class CanonicalTranslator:
    """Stateful state machine: raw events in, canonical ProgressEvents out.

    The translator keeps a 1-deep buffer of the last closed content block so
    it can label the final text block correctly at end of turn without
    delaying any narration emissions.

    Usage:

        t = CanonicalTranslator(correlation_id="...")
        for raw in source:
            for evt in t.feed(raw):
                consume(evt)
        for evt in t.flush():
            consume(evt)

    Thread-safety: not safe for concurrent use from multiple coroutines.
    Callers should own one translator per turn.
    """

    def __init__(self, correlation_id: str = "", session_id: str = ""):
        self._correlation_id = correlation_id
        self._session_id = session_id
        # Final-vs-narration disambiguation: track the last closed block.
        self._last_closed_kind: Optional[str] = None  # "text" | "tool_use"
        self._last_closed_text: str = ""
        # Per-tool-block accumulators for the stdout path.
        self._tool_blocks: dict[int, dict] = {}
        # Current text block accumulator (stdout path).
        self._current_text: str = ""
        # Whether flush already emitted FINAL (idempotence).
        self._flushed: bool = False

    # ------------------------------------------------------------------
    # Internals

    def _wrap(self, etype: ProgressEventType, payload: dict) -> ProgressEvent:
        return ProgressEvent(
            type=etype,
            payload=payload,
            ts=time.time(),
            correlation_id=self._correlation_id,
            session_id=self._session_id,
        )

    def _close_text_block(self) -> None:
        if self._current_text:
            self._last_closed_kind = "text"
            self._last_closed_text = self._current_text
        self._current_text = ""

    def _close_tool_use(self) -> None:
        self._last_closed_kind = "tool_use"
        self._last_closed_text = ""

    # ------------------------------------------------------------------
    # stdout (stream-json) source

    def feed_stdout(self, evt: dict) -> Iterable[ProgressEvent]:
        """Consume one stream-json line from claude's stdout."""
        etype = evt.get("type")

        if etype == "stream_event":
            se = evt.get("event", {}) or {}
            se_type = se.get("type")

            if se_type == "content_block_start":
                idx = se.get("index", 0)
                block = se.get("content_block", {}) or {}
                btype = block.get("type")
                if btype == "tool_use":
                    self._tool_blocks[idx] = {
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "partial_json": "",
                    }
                elif btype == "text":
                    self._current_text = ""
                return

            if se_type == "content_block_delta":
                idx = se.get("index", 0)
                delta = se.get("delta", {}) or {}
                dtype = delta.get("type")

                if dtype == "thinking_delta":
                    yield self._wrap(ProgressEventType.THINKING, {"text": delta.get("thinking", "")})
                    return

                if dtype == "text_delta":
                    text = delta.get("text", "")
                    self._current_text += text
                    yield self._wrap(ProgressEventType.NARRATION, {"text": text})
                    return

                if dtype == "input_json_delta":
                    tb = self._tool_blocks.get(idx)
                    if tb is not None:
                        tb["partial_json"] += delta.get("partial_json", "")
                    return

                return

            if se_type == "content_block_stop":
                idx = se.get("index", 0)
                tb = self._tool_blocks.pop(idx, None)
                if tb is not None:
                    try:
                        parsed = json.loads(tb["partial_json"]) if tb["partial_json"] else {}
                    except Exception:
                        parsed = {"_raw": tb["partial_json"]}
                    self._close_tool_use()
                    yield self._wrap(
                        ProgressEventType.TOOL_CALL,
                        {
                            "name": tb["name"],
                            "args": parsed,
                            "tool_use_id": tb["id"],
                        },
                    )
                else:
                    self._close_text_block()
                return

            return

        if etype == "user":
            msg = evt.get("message", {}) or {}
            content = msg.get("content", [])
            if isinstance(content, list):
                for blk in content:
                    if not isinstance(blk, dict):
                        continue
                    if blk.get("type") != "tool_result":
                        continue
                    out = blk.get("content", "")
                    if isinstance(out, list):
                        parts = []
                        for item in out:
                            if isinstance(item, dict) and item.get("type") == "text":
                                parts.append(item.get("text", ""))
                            else:
                                parts.append(str(item))
                        out = "\n".join(parts)
                    yield self._wrap(
                        ProgressEventType.TOOL_RESULT,
                        {
                            "tool_use_id": blk.get("tool_use_id", ""),
                            "content": out if isinstance(out, str) else str(out),
                            "is_error": bool(blk.get("is_error", False)),
                        },
                    )
            return

        if etype == "result":
            # Terminal marker — flush the buffered text as FINAL, then USAGE.
            for ev in self.flush():
                yield ev
            usage = evt.get("usage", {}) or {}
            is_error = bool(evt.get("is_error", False)) or evt.get("subtype") == "error"
            yield self._wrap(
                ProgressEventType.USAGE,
                {
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cost_usd": evt.get("cost_usd", 0.0),
                    "duration_ms": evt.get("duration_ms", 0),
                    "stop_reason": "error" if is_error else "stop",
                    "is_error": is_error,
                },
            )
            return

    # ------------------------------------------------------------------
    # Redis event stream source

    def feed_redis(self, event: dict) -> Iterable[ProgressEvent]:
        """Consume one event dict read from agenticore:events:{uuid}.

        event_types published by event_relay.py: thinking, tool_use,
        tool_result, assistant_text, done.
        """
        et = event.get("event_type", "")
        content = event.get("content", "")

        if et == "thinking":
            yield self._wrap(ProgressEventType.THINKING, {"text": content})
            return

        if et == "assistant_text":
            # In Redis the text arrives as a whole block (not a delta). Emit
            # it as NARRATION and buffer it so flush() can retroactively
            # label it FINAL if nothing else closes after it.
            if content:
                yield self._wrap(ProgressEventType.NARRATION, {"text": content})
            self._current_text = content
            self._close_text_block()
            return

        if et == "tool_use":
            try:
                data = json.loads(content) if isinstance(content, str) else content
            except Exception:
                data = {}
            self._close_tool_use()
            yield self._wrap(
                ProgressEventType.TOOL_CALL,
                {
                    "name": data.get("name", "tool") if isinstance(data, dict) else "tool",
                    "args": data.get("input", {}) if isinstance(data, dict) else {},
                    "tool_use_id": data.get("id", "") if isinstance(data, dict) else "",
                },
            )
            return

        if et == "tool_result":
            try:
                data = json.loads(content) if isinstance(content, str) else content
            except Exception:
                data = {"output": str(content)}
            output = data.get("output", "") if isinstance(data, dict) else str(data)
            if not isinstance(output, str):
                try:
                    output = json.dumps(output, ensure_ascii=False)
                except Exception:
                    output = str(output)
            yield self._wrap(
                ProgressEventType.TOOL_RESULT,
                {
                    "tool_use_id": data.get("tool_use_id", "") if isinstance(data, dict) else "",
                    "content": output,
                    "is_error": bool(data.get("is_error", False)) if isinstance(data, dict) else False,
                },
            )
            return

        if et == "done":
            for ev in self.flush():
                yield ev
            yield self._wrap(ProgressEventType.DONE, {})
            return

    # ------------------------------------------------------------------
    # Flush — called on terminal event to promote the last buffered text
    # block to FINAL. Idempotent: calling twice is a no-op the second time.

    def flush(self) -> Iterable[ProgressEvent]:
        if self._flushed:
            return
        self._flushed = True
        if self._current_text:
            self._close_text_block()
        if self._last_closed_kind == "text" and self._last_closed_text:
            yield self._wrap(ProgressEventType.FINAL, {"text": self._last_closed_text})
            self._last_closed_text = ""
            self._last_closed_kind = None


# ----------------------------------------------------------------------
# Visibility filter — sits between the translator and any consumer.


def progress_is_visible(evt: ProgressEvent, stream_cfg: dict) -> bool:
    """Gate canonical ProgressEvents against the stream_config. Control events
    (usage, error, done) are always visible."""
    from agenticore.agent_mode.stream_config import is_visible

    return is_visible(evt.type.value, stream_cfg)


# ----------------------------------------------------------------------
# Sink dispatcher — converts ProgressEvents into ProgressSink callbacks.


class OnceSink:
    """Wraps any ProgressSink and guarantees on_final / on_usage / on_error
    fire at most once per turn. Lets multiple producers (Redis tailer +
    subprocess fallback) both attempt terminal dispatch without the sink
    receiving duplicate finals. Incremental callbacks (narration, thinking,
    tool_call, tool_result) are forwarded unchanged.
    """

    def __init__(self, inner):
        self._inner = inner
        self._final_sent = False
        self._usage_sent = False
        self._error_sent = False

    async def on_narration(self, text: str) -> None:
        await self._inner.on_narration(text)

    async def on_final(self, text: str) -> None:
        if self._final_sent:
            return
        self._final_sent = True
        await self._inner.on_final(text)

    async def on_tool_call(self, name: str, args: dict, tool_use_id: str) -> None:
        await self._inner.on_tool_call(name, args, tool_use_id)

    async def on_tool_result(self, tool_use_id: str, content: str, is_error: bool) -> None:
        await self._inner.on_tool_result(tool_use_id, content, is_error)

    async def on_thinking(self, text: str) -> None:
        await self._inner.on_thinking(text)

    async def on_usage(self, usage: dict) -> None:
        if self._usage_sent:
            return
        self._usage_sent = True
        await self._inner.on_usage(usage)

    async def on_error(self, message: str, code=None) -> None:
        if self._error_sent:
            return
        self._error_sent = True
        await self._inner.on_error(message, code)


async def dispatch_to_sink(evt: ProgressEvent, sink) -> None:
    """Dispatch one canonical event to the matching ProgressSink callback.

    Swallows callback exceptions and logs them — a misbehaving sink should
    not halt the pipeline.
    """
    try:
        et = evt.type
        pl = evt.payload or {}
        if et is ProgressEventType.NARRATION:
            await sink.on_narration(pl.get("text", ""))
        elif et is ProgressEventType.FINAL:
            await sink.on_final(pl.get("text", ""))
        elif et is ProgressEventType.TOOL_CALL:
            await sink.on_tool_call(
                pl.get("name", ""),
                pl.get("args", {}) or {},
                pl.get("tool_use_id", ""),
            )
        elif et is ProgressEventType.TOOL_RESULT:
            await sink.on_tool_result(
                pl.get("tool_use_id", ""),
                pl.get("content", ""),
                bool(pl.get("is_error", False)),
            )
        elif et is ProgressEventType.THINKING:
            await sink.on_thinking(pl.get("text", ""))
        elif et is ProgressEventType.USAGE:
            await sink.on_usage(pl)
        elif et is ProgressEventType.ERROR:
            await sink.on_error(pl.get("message", ""), pl.get("code"))
        # DONE is an internal sentinel — no sink method.
    except Exception as e:
        _log.warning("sink callback raised for %s: %s", evt.type.value, e)


# ----------------------------------------------------------------------
# Redis-driven producer — subscribes to agenticore:events:{uuid} and yields
# canonical ProgressEvents. Used by execute() when a sink is attached.


async def tail_canonical_from_redis(
    correlation_id: str,
    stream_cfg: dict,
    process_handle: Optional[asyncio.subprocess.Process] = None,
    timeout_s: int = 600,
    session_id: str = "",
) -> AsyncIterator[ProgressEvent]:
    """Tail Redis events and yield canonical ProgressEvents.

    Honours stream_cfg visibility at the canonical layer — callers do not
    filter again. Terminates on the ``done`` sentinel or subprocess exit.
    """
    from agenticore.agent_mode.event_tailer import tail_event_stream

    translator = CanonicalTranslator(correlation_id=correlation_id, session_id=session_id)
    # Visibility is applied against canonical types on the output side, so
    # pass a permissive config to tail_event_stream to avoid double-filtering
    # (we need the raw events to correctly label FINAL).
    permissive = {"show_thinking": True, "show_tools": True, "show_text": True}

    async for raw in tail_event_stream(correlation_id, permissive, process_handle, timeout_s):
        for canonical in translator.feed_redis(raw):
            if progress_is_visible(canonical, stream_cfg):
                yield canonical
            elif canonical.type in (
                ProgressEventType.USAGE,
                ProgressEventType.ERROR,
                ProgressEventType.DONE,
            ):
                yield canonical

    # Subscriber exited without seeing done — flush whatever we buffered.
    for canonical in translator.flush():
        if progress_is_visible(canonical, stream_cfg):
            yield canonical


__all__ = [
    "ProgressEvent",
    "ProgressEventType",
    "CanonicalTranslator",
    "OnceSink",
    "progress_is_visible",
    "dispatch_to_sink",
    "tail_canonical_from_redis",
]
