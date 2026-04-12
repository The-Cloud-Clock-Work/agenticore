"""Tests for agenticore.agent_mode.event_tailer."""

import asyncio

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def fake_redis(monkeypatch):
    import fakeredis
    fake = fakeredis.FakeStrictRedis(decode_responses=True)
    monkeypatch.setattr(
        "agenticore.agent_mode.event_tailer._get_redis",
        lambda: fake,
    )
    return fake


def _seed(fake_redis, uuid: str, events: list[dict]):
    from agenticore.agent_mode.event_tailer import stream_key
    key = stream_key(uuid)
    for ev in events:
        fake_redis.xadd(key, ev)


class TestParseStreamEntry:
    def test_basic(self):
        from agenticore.agent_mode.event_tailer import parse_stream_entry
        ev = parse_stream_entry(
            "1234-0",
            {
                "event_type": "thinking",
                "content": "thoughts",
                "ts": "2026-04-12T00:00:00Z",
                "session_id": "s1",
                "correlation_id": "c1",
            },
        )
        assert ev["id"] == "1234-0"
        assert ev["event_type"] == "thinking"
        assert ev["content"] == "thoughts"


class TestTailEventStream:
    @pytest.mark.asyncio
    async def test_replays_past_events(self, fake_redis):
        from agenticore.agent_mode.event_tailer import tail_event_stream_until_done
        _seed(fake_redis, "u1", [
            {"event_type": "thinking", "content": "t1", "ts": "x", "session_id": "s", "correlation_id": "u1"},
            {"event_type": "assistant_text", "content": "hi", "ts": "x", "session_id": "s", "correlation_id": "u1"},
            {"event_type": "done", "content": "", "ts": "x", "session_id": "s", "correlation_id": "u1"},
        ])
        cfg = {"show_thinking": True, "show_tools": False, "show_text": True}
        events = await tail_event_stream_until_done("u1", cfg, timeout_s=5)
        types = [e["event_type"] for e in events]
        assert types == ["thinking", "assistant_text", "done"]

    @pytest.mark.asyncio
    async def test_filters_thinking_when_disabled(self, fake_redis):
        from agenticore.agent_mode.event_tailer import tail_event_stream_until_done
        _seed(fake_redis, "u2", [
            {"event_type": "thinking", "content": "hidden", "ts": "x", "session_id": "s", "correlation_id": "u2"},
            {"event_type": "assistant_text", "content": "shown", "ts": "x", "session_id": "s", "correlation_id": "u2"},
            {"event_type": "done", "content": "", "ts": "x", "session_id": "s", "correlation_id": "u2"},
        ])
        cfg = {"show_thinking": False, "show_tools": False, "show_text": True}
        events = await tail_event_stream_until_done("u2", cfg, timeout_s=5)
        types = [e["event_type"] for e in events]
        assert "thinking" not in types
        assert "assistant_text" in types

    @pytest.mark.asyncio
    async def test_filters_tools_when_disabled(self, fake_redis):
        from agenticore.agent_mode.event_tailer import tail_event_stream_until_done
        _seed(fake_redis, "u3", [
            {"event_type": "tool_use", "content": "{}", "ts": "x", "session_id": "s", "correlation_id": "u3"},
            {"event_type": "tool_result", "content": "{}", "ts": "x", "session_id": "s", "correlation_id": "u3"},
            {"event_type": "assistant_text", "content": "ok", "ts": "x", "session_id": "s", "correlation_id": "u3"},
            {"event_type": "done", "content": "", "ts": "x", "session_id": "s", "correlation_id": "u3"},
        ])
        cfg = {"show_thinking": False, "show_tools": False, "show_text": True}
        events = await tail_event_stream_until_done("u3", cfg, timeout_s=5)
        types = [e["event_type"] for e in events]
        assert "tool_use" not in types
        assert "tool_result" not in types

    @pytest.mark.asyncio
    async def test_show_all_passes_everything(self, fake_redis):
        from agenticore.agent_mode.event_tailer import tail_event_stream_until_done
        _seed(fake_redis, "u4", [
            {"event_type": "thinking", "content": "t", "ts": "x", "session_id": "s", "correlation_id": "u4"},
            {"event_type": "tool_use", "content": "{}", "ts": "x", "session_id": "s", "correlation_id": "u4"},
            {"event_type": "tool_result", "content": "{}", "ts": "x", "session_id": "s", "correlation_id": "u4"},
            {"event_type": "assistant_text", "content": "hi", "ts": "x", "session_id": "s", "correlation_id": "u4"},
            {"event_type": "done", "content": "", "ts": "x", "session_id": "s", "correlation_id": "u4"},
        ])
        cfg = {"show_thinking": True, "show_tools": True, "show_text": True}
        events = await tail_event_stream_until_done("u4", cfg, timeout_s=5)
        types = [e["event_type"] for e in events]
        assert types == ["thinking", "tool_use", "tool_result", "assistant_text", "done"]

    @pytest.mark.asyncio
    async def test_done_sentinel_terminates(self, fake_redis):
        from agenticore.agent_mode.event_tailer import tail_event_stream_until_done
        _seed(fake_redis, "u5", [
            {"event_type": "assistant_text", "content": "first", "ts": "x", "session_id": "s", "correlation_id": "u5"},
            {"event_type": "done", "content": "", "ts": "x", "session_id": "s", "correlation_id": "u5"},
            {"event_type": "assistant_text", "content": "after-done", "ts": "x", "session_id": "s", "correlation_id": "u5"},
        ])
        cfg = {"show_thinking": False, "show_tools": False, "show_text": True}
        events = await tail_event_stream_until_done("u5", cfg, timeout_s=5)
        contents = [e["content"] for e in events if e["event_type"] == "assistant_text"]
        assert contents == ["first"]

    @pytest.mark.asyncio
    async def test_no_events_times_out(self, fake_redis):
        from agenticore.agent_mode.event_tailer import tail_event_stream_until_done
        cfg = {"show_thinking": False, "show_tools": False, "show_text": True}
        events = await asyncio.wait_for(
            tail_event_stream_until_done("u6", cfg, timeout_s=2),
            timeout=4,
        )
        assert events == []
