"""Unit tests for the canonical progress-event pipeline."""

import json
import pytest

from agenticore.agent_mode.progress import (
    CanonicalTranslator,
    OnceSink,
    ProgressEvent,
    ProgressEventType,
    dispatch_to_sink,
    progress_is_visible,
)
from agenticore.connectors.base import ProgressSink


pytestmark = pytest.mark.unit


# ----------------------------------------------------------------------
# Stdout (stream-json) path


class TestStdoutTranslator:
    def test_text_delta_emits_narration(self):
        t = CanonicalTranslator()
        events = list(
            t.feed_stdout(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": "hi"},
                    },
                }
            )
        )
        assert len(events) == 1
        assert events[0].type is ProgressEventType.NARRATION
        assert events[0].payload == {"text": "hi"}

    def test_thinking_delta_emits_thinking(self):
        t = CanonicalTranslator()
        events = list(
            t.feed_stdout(
                {
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "thinking_delta", "thinking": "hmm..."},
                    },
                }
            )
        )
        assert len(events) == 1
        assert events[0].type is ProgressEventType.THINKING
        assert events[0].payload == {"text": "hmm..."}

    def test_tool_use_block_emits_tool_call(self):
        t = CanonicalTranslator()
        feed = [
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "tu_1", "name": "bash"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": '{"cmd"'},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": ': "ls"}'},
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
        ]
        events = []
        for raw in feed:
            events.extend(t.feed_stdout(raw))
        assert len(events) == 1
        assert events[0].type is ProgressEventType.TOOL_CALL
        assert events[0].payload == {"name": "bash", "args": {"cmd": "ls"}, "tool_use_id": "tu_1"}

    def test_tool_result_from_user_message(self):
        t = CanonicalTranslator()
        raw = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tu_1",
                        "content": "ok",
                        "is_error": False,
                    }
                ]
            },
        }
        events = list(t.feed_stdout(raw))
        assert len(events) == 1
        assert events[0].type is ProgressEventType.TOOL_RESULT
        assert events[0].payload["tool_use_id"] == "tu_1"
        assert events[0].payload["content"] == "ok"
        assert events[0].payload["is_error"] is False

    def test_final_vs_narration_disambiguation(self):
        """The last closed text block must be labelled FINAL; earlier text
        blocks stay as NARRATION only. This is the whole point of the
        translator's 1-deep buffer."""
        t = CanonicalTranslator()
        feed = [
            # first text block — narration (followed by a tool call)
            {
                "type": "stream_event",
                "event": {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": "checking… "},
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            # tool call
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "tu_1", "name": "bash"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "input_json_delta", "partial_json": "{}"},
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 1}},
            # second (and final) text block
            {
                "type": "stream_event",
                "event": {"type": "content_block_start", "index": 2, "content_block": {"type": "text"}},
            },
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "index": 2, "delta": {"type": "text_delta", "text": "done."}},
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 2}},
            # terminal
            {"type": "result", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
        events = []
        for raw in feed:
            events.extend(t.feed_stdout(raw))
        types = [e.type.value for e in events]
        # narration (delta) → tool_call → narration (delta) → final (flushed) → usage
        assert types == ["narration", "tool_call", "narration", "final", "usage"]
        final_events = [e for e in events if e.type is ProgressEventType.FINAL]
        assert len(final_events) == 1
        assert final_events[0].payload == {"text": "done."}

    def test_no_final_when_last_block_was_tool_use(self):
        """If the turn ends after a tool_use (no trailing text), no FINAL
        event should be emitted — only USAGE."""
        t = CanonicalTranslator()
        feed = [
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "tu_1", "name": "bash"},
                },
            },
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "input_json_delta", "partial_json": "{}"},
                },
            },
            {"type": "stream_event", "event": {"type": "content_block_stop", "index": 0}},
            {"type": "result", "usage": {}},
        ]
        events = []
        for raw in feed:
            events.extend(t.feed_stdout(raw))
        types = [e.type.value for e in events]
        assert "final" not in types
        assert types == ["tool_call", "usage"]


# ----------------------------------------------------------------------
# Redis path


class TestRedisTranslator:
    def test_assistant_text_emits_narration_and_final(self):
        t = CanonicalTranslator()
        events = []
        for raw in [
            {"event_type": "assistant_text", "content": "hello"},
            {"event_type": "done", "content": ""},
        ]:
            events.extend(t.feed_redis(raw))
        types = [e.type.value for e in events]
        # narration (live), final (flush on done), done
        assert types == ["narration", "final", "done"]
        assert events[1].payload == {"text": "hello"}

    def test_tool_use_from_redis(self):
        t = CanonicalTranslator()
        raw = {
            "event_type": "tool_use",
            "content": json.dumps({"id": "tu_9", "name": "grep", "input": {"pat": "x"}}),
        }
        events = list(t.feed_redis(raw))
        assert len(events) == 1
        assert events[0].type is ProgressEventType.TOOL_CALL
        assert events[0].payload == {"name": "grep", "args": {"pat": "x"}, "tool_use_id": "tu_9"}

    def test_done_with_no_text_emits_no_final(self):
        t = CanonicalTranslator()
        events = list(t.feed_redis({"event_type": "done", "content": ""}))
        types = [e.type.value for e in events]
        assert types == ["done"]

    def test_flush_is_idempotent(self):
        t = CanonicalTranslator()
        list(t.feed_redis({"event_type": "assistant_text", "content": "x"}))
        first = list(t.flush())
        second = list(t.flush())
        assert len(first) == 1
        assert first[0].type is ProgressEventType.FINAL
        assert second == []


# ----------------------------------------------------------------------
# Visibility


class TestVisibility:
    def test_narration_gated_by_show_narration(self):
        evt = ProgressEvent(type=ProgressEventType.NARRATION, payload={"text": "x"})
        assert progress_is_visible(evt, {"show_narration": True}) is True
        assert progress_is_visible(evt, {"show_narration": False}) is False

    def test_final_gated_by_show_final(self):
        evt = ProgressEvent(type=ProgressEventType.FINAL, payload={"text": "x"})
        assert progress_is_visible(evt, {"show_final": True}) is True
        assert progress_is_visible(evt, {"show_final": False}) is False

    def test_narration_falls_back_to_show_text(self):
        evt = ProgressEvent(type=ProgressEventType.NARRATION, payload={"text": "x"})
        # no show_narration set — should use show_text
        assert progress_is_visible(evt, {"show_text": True}) is True
        assert progress_is_visible(evt, {"show_text": False}) is False

    def test_tool_call_gated_by_show_tools(self):
        """Regression: canonical 'tool_call' must map to show_tools (was
        silently filtered because is_visible only knew 'tool_use')."""
        evt = ProgressEvent(type=ProgressEventType.TOOL_CALL, payload={"name": "bash"})
        assert progress_is_visible(evt, {"show_tools": True}) is True
        assert progress_is_visible(evt, {"show_tools": False}) is False

    def test_tool_result_gated_by_show_tools(self):
        evt = ProgressEvent(type=ProgressEventType.TOOL_RESULT, payload={"content": "x"})
        assert progress_is_visible(evt, {"show_tools": True}) is True
        assert progress_is_visible(evt, {"show_tools": False}) is False


# ----------------------------------------------------------------------
# Sink dispatching + OnceSink


class RecordingSink(ProgressSink):
    def __init__(self):
        self.calls = []

    async def on_narration(self, text):
        self.calls.append(("narration", text))

    async def on_final(self, text):
        self.calls.append(("final", text))

    async def on_tool_call(self, name, args, tool_use_id):
        self.calls.append(("tool_call", name, args, tool_use_id))

    async def on_tool_result(self, tool_use_id, content, is_error):
        self.calls.append(("tool_result", tool_use_id, content, is_error))

    async def on_thinking(self, text):
        self.calls.append(("thinking", text))

    async def on_usage(self, usage):
        self.calls.append(("usage", usage))

    async def on_error(self, message, code=None):
        self.calls.append(("error", message, code))


class TestDispatchAndOnceSink:
    @pytest.mark.asyncio
    async def test_dispatch_narration(self):
        sink = RecordingSink()
        await dispatch_to_sink(
            ProgressEvent(type=ProgressEventType.NARRATION, payload={"text": "x"}),
            sink,
        )
        assert sink.calls == [("narration", "x")]

    @pytest.mark.asyncio
    async def test_once_sink_dedups_final(self):
        inner = RecordingSink()
        once = OnceSink(inner)
        await once.on_final("first")
        await once.on_final("second")
        finals = [c for c in inner.calls if c[0] == "final"]
        assert finals == [("final", "first")]

    @pytest.mark.asyncio
    async def test_once_sink_dedups_usage(self):
        inner = RecordingSink()
        once = OnceSink(inner)
        await once.on_usage({"a": 1})
        await once.on_usage({"a": 2})
        usages = [c for c in inner.calls if c[0] == "usage"]
        assert usages == [("usage", {"a": 1})]

    @pytest.mark.asyncio
    async def test_once_sink_passes_through_narration(self):
        inner = RecordingSink()
        once = OnceSink(inner)
        await once.on_narration("a")
        await once.on_narration("b")
        narrs = [c for c in inner.calls if c[0] == "narration"]
        assert narrs == [("narration", "a"), ("narration", "b")]
