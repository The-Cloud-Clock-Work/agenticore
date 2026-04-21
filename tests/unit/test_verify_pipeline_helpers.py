"""Unit tests for the SSE pipeline audit helpers."""

import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).parent.parent / "smoke"))

from verify_pipeline_helpers import (  # noqa: E402
    count_by,
    cross_validate,
    marker_in_redis,
    marker_in_sse_tool_result,
    marker_in_sse_tool_use,
    marker_in_transcript,
    parse_redis_event,
    parse_sse_chunk,
    parse_sse_stream,
    parse_transcript_blocks,
    check_ordering,
    render_report_md,
)


MARKER = "SSE-AUDIT-TEST-12345678"
UUID = "corr-uuid-abc"


def _sse_line(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n"


def _chunk_role_open() -> str:
    return _sse_line(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    )


def _chunk_text(text: str) -> str:
    return _sse_line(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
    )


def _chunk_tool_use(tool_id: str, name: str, args: str) -> str:
    return _sse_line(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": tool_id,
                                "type": "function",
                                "function": {"name": name, "arguments": args},
                            }
                        ]
                    },
                    "finish_reason": None,
                    "x_agenticore_event_type": "tool_use",
                }
            ],
        }
    )


def _chunk_tool_result(text: str, tool_use_id: str = "") -> str:
    return _sse_line(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": None,
                    "x_agenticore_event_type": "tool_result",
                    "x_agenticore_tool_use_id": tool_use_id,
                    "x_agenticore_is_error": False,
                }
            ],
        }
    )


def _chunk_stop() -> str:
    return _sse_line(
        {
            "id": "x",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "m",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8},
        }
    )


def _done_marker() -> str:
    return "data: [DONE]\n"


class TestParseSseChunk:
    def test_role_open(self):
        c = parse_sse_chunk(_chunk_role_open().strip())
        assert c["event_type"] == "role_open"

    def test_text_delta(self):
        c = parse_sse_chunk(_chunk_text("hello").strip())
        assert c["event_type"] == "assistant_text"
        assert c["content"] == "hello"

    def test_tool_use(self):
        c = parse_sse_chunk(_chunk_tool_use("tu_1", "Bash", '{"command":"ls"}').strip())
        assert c["event_type"] == "tool_use"
        assert c["tool_calls"][0]["name"] == "Bash"
        assert c["tool_calls"][0]["id"] == "tu_1"

    def test_tool_result(self):
        c = parse_sse_chunk(_chunk_tool_result("done\n", "tu_1").strip())
        assert c["event_type"] == "tool_result"
        assert c["content"] == "done\n"
        assert c["tool_use_id"] == "tu_1"

    def test_stop(self):
        c = parse_sse_chunk(_chunk_stop().strip())
        assert c["event_type"] == "stop_chunk"
        assert c["finish_reason"] == "stop"
        assert c["usage"]["total_tokens"] == 8

    def test_done_marker(self):
        c = parse_sse_chunk(_done_marker().strip())
        assert c["event_type"] == "done_marker"

    def test_non_data_line_returns_none(self):
        assert parse_sse_chunk("event: thinking") is None
        assert parse_sse_chunk("") is None
        assert parse_sse_chunk(": heartbeat") is None

    def test_malformed_json_returns_none(self):
        assert parse_sse_chunk("data: {broken") is None


class TestParseSseStream:
    def test_full_conversation(self):
        text = (
            _chunk_role_open()
            + _chunk_tool_use("tu_1", "Bash", f'{{"command":"echo {MARKER}"}}')
            + _chunk_tool_result(f"{MARKER}\n", "tu_1")
            + _chunk_text("done")
            + _chunk_stop()
            + _done_marker()
        )
        chunks = parse_sse_stream(text)
        types = [c["event_type"] for c in chunks]
        assert types == [
            "role_open",
            "tool_use",
            "tool_result",
            "assistant_text",
            "stop_chunk",
            "done_marker",
        ]


class TestParseRedisEvent:
    def test_basic(self):
        ev = parse_redis_event(
            "1234-0",
            {
                "event_type": "tool_use",
                "content": "x",
                "ts": "2026-04-13T00:00:00Z",
                "session_id": "s1",
                "correlation_id": UUID,
            },
        )
        assert ev["event_type"] == "tool_use"
        assert ev["correlation_id"] == UUID


class TestParseTranscriptBlocks:
    def test_assistant_with_tool_use(self):
        text = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking", "thinking": "thinking here"},
                            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": f"echo {MARKER}"}},
                        ]
                    },
                }
            )
            + "\n"
        )
        blocks = parse_transcript_blocks(text)
        assert [b["block_type"] for b in blocks] == ["thinking", "tool_use"]
        assert MARKER in blocks[1]["preview"]

    def test_user_tool_result_list(self):
        text = (
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": [{"type": "text", "text": f"{MARKER}\n"}],
                            },
                        ]
                    },
                }
            )
            + "\n"
        )
        blocks = parse_transcript_blocks(text)
        assert blocks[0]["block_type"] == "tool_result"
        assert MARKER in blocks[0]["preview"]

    def test_skips_non_user_assistant(self):
        text = (
            json.dumps({"type": "system", "content": "whatever"})
            + "\n"
            + json.dumps({"type": "file-history-snapshot"})
            + "\n"
        )
        assert parse_transcript_blocks(text) == []

    def test_malformed_lines_skipped(self):
        text = (
            "not json\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "ok"}]},
                }
            )
            + "\n"
        )
        blocks = parse_transcript_blocks(text)
        assert len(blocks) == 1
        assert blocks[0]["block_type"] == "text"


class TestMarkerChecks:
    def test_marker_in_sse_tool_use_hit(self):
        chunks = parse_sse_stream(_chunk_tool_use("tu", "Bash", f'{{"command":"echo {MARKER}"}}'))
        assert marker_in_sse_tool_use(chunks, MARKER) is True

    def test_marker_in_sse_tool_use_miss(self):
        chunks = parse_sse_stream(_chunk_tool_use("tu", "Bash", '{"command":"ls"}'))
        assert marker_in_sse_tool_use(chunks, MARKER) is False

    def test_marker_in_sse_tool_result(self):
        chunks = parse_sse_stream(_chunk_tool_result(f"{MARKER}\n", "tu_1"))
        assert marker_in_sse_tool_result(chunks, MARKER) is True

    def test_marker_in_redis(self):
        events = [
            parse_redis_event(
                "1-0",
                {
                    "event_type": "tool_use",
                    "content": json.dumps({"name": "Bash", "input": {"command": f"echo {MARKER}"}}),
                },
            )
        ]
        assert marker_in_redis(events, "tool_use", MARKER) is True
        assert marker_in_redis(events, "tool_result", MARKER) is False

    def test_marker_in_transcript(self):
        blocks = [{"block_type": "tool_use", "preview": f'{{"input":{{"command":"echo {MARKER}"}}}}'}]
        assert marker_in_transcript(blocks, "tool_use", MARKER) is True
        assert marker_in_transcript(blocks, "tool_result", MARKER) is False


class TestOrdering:
    def test_correct(self):
        text = (
            _chunk_role_open()
            + _chunk_tool_use("t", "B", "{}")
            + _chunk_tool_result("o", "t")
            + _chunk_text("hi")
            + _chunk_stop()
            + _done_marker()
        )
        ordering = check_ordering(parse_sse_stream(text))
        assert ordering["has_role_open_first"] is True
        assert ordering["no_events_after_done"] is True
        assert ordering["stop_before_done"] is True

    def test_event_after_done_flagged(self):
        text = _chunk_role_open() + _chunk_stop() + _done_marker() + _chunk_text("leaked")
        ordering = check_ordering(parse_sse_stream(text))
        assert ordering["no_events_after_done"] is False


class TestCrossValidate:
    def _full_happy(self):
        sse_text = (
            _chunk_role_open()
            + _chunk_tool_use("tu_1", "Bash", f'{{"command":"echo {MARKER}"}}')
            + _chunk_tool_result(f"{MARKER}\n", "tu_1")
            + _chunk_text("done")
            + _chunk_stop()
            + _done_marker()
        )
        sse_chunks = parse_sse_stream(sse_text)
        redis_events = [
            parse_redis_event(
                "1-0",
                {
                    "event_type": "tool_use",
                    "content": json.dumps({"id": "tu_1", "name": "Bash", "input": {"command": f"echo {MARKER}"}}),
                    "correlation_id": UUID,
                },
            ),
            parse_redis_event(
                "2-0",
                {
                    "event_type": "tool_result",
                    "content": json.dumps({"tool_use_id": "tu_1", "is_error": False, "output": f"{MARKER}\n"}),
                    "correlation_id": UUID,
                },
            ),
            parse_redis_event(
                "3-0",
                {
                    "event_type": "assistant_text",
                    "content": "done",
                    "correlation_id": UUID,
                },
            ),
            parse_redis_event(
                "4-0",
                {
                    "event_type": "done",
                    "content": "",
                    "correlation_id": UUID,
                },
            ),
        ]
        transcript = (
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": f"echo {MARKER}"}},
                        ]
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu_1",
                                "content": [{"type": "text", "text": f"{MARKER}\n"}],
                            },
                        ]
                    },
                }
            )
            + "\n"
            + json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "done"}]},
                }
            )
            + "\n"
        )
        blocks = parse_transcript_blocks(transcript)
        return sse_chunks, redis_events, blocks

    def test_happy_path_passes(self):
        sse, redis, blocks = self._full_happy()
        report = cross_validate(sse, redis, blocks, MARKER, UUID)
        assert report["overall_pass"] is True
        assert report["failing_checks"] == []
        assert all(report["marker_propagation"].values())

    def test_marker_missing_from_sse_fails_cleanly(self):
        sse, redis, blocks = self._full_happy()
        # Strip the marker from the SSE tool_use
        sse_mod = []
        for c in sse:
            if c["event_type"] == "tool_use":
                c = dict(c)
                c["tool_calls"] = [{"id": "tu_1", "name": "Bash", "arguments": '{"command":"ls"}'}]
            sse_mod.append(c)
        report = cross_validate(sse_mod, redis, blocks, MARKER, UUID)
        assert report["overall_pass"] is False
        assert "marker_propagation.sse_tool_use" in report["failing_checks"]

    def test_count_mismatch_flagged(self):
        sse, redis, blocks = self._full_happy()
        # Duplicate the tool_use redis event to create a mismatch
        redis_dup = redis + [redis[0]]
        report = cross_validate(sse, redis_dup, blocks, MARKER, UUID)
        assert report["overall_pass"] is False
        assert "consistency.sse_tool_use_count_matches_redis" in report["failing_checks"]

    def test_wrong_correlation_id_flagged(self):
        sse, redis, blocks = self._full_happy()
        for e in redis:
            e["correlation_id"] = "different-uuid"
        report = cross_validate(sse, redis, blocks, MARKER, UUID)
        assert report["overall_pass"] is False
        assert "correlation_ids_in_redis" in report["failing_checks"]

    def test_done_missing_flagged(self):
        sse, redis, blocks = self._full_happy()
        sse_no_done = [c for c in sse if c["event_type"] != "done_marker"]
        report = cross_validate(sse_no_done, redis, blocks, MARKER, UUID)
        assert report["overall_pass"] is False
        assert "has_done_marker" in report["failing_checks"]


class TestRenderReportMd:
    def test_pass_verdict(self):
        report = {
            "overall_pass": True,
            "event_counts": {"sse": {"role_open": 1}, "redis": {}, "transcript": {}},
            "marker_propagation": {"sse_tool_use": True},
            "ordering": {"has_role_open_first": True},
            "consistency": {"sse_tool_use_count_matches_redis": True},
            "failing_checks": [],
        }
        md = render_report_md(report, {"run_id": "r1", "agent": "a", "correlation_uuid": "u"})
        assert "**PASS**" in md
        assert "r1" in md
        assert "✓ `sse_tool_use`" in md

    def test_fail_lists_failures(self):
        report = {
            "overall_pass": False,
            "event_counts": {"sse": {}, "redis": {}, "transcript": {}},
            "marker_propagation": {"sse_tool_use": False},
            "ordering": {"has_role_open_first": False},
            "consistency": {},
            "failing_checks": ["marker_propagation.sse_tool_use", "ordering.has_role_open_first"],
        }
        md = render_report_md(report, {"run_id": "r1"})
        assert "**FAIL**" in md
        assert "## Failing checks" in md
        assert "marker_propagation.sse_tool_use" in md


class TestCountBy:
    def test_counts(self):
        items = [{"event_type": "a"}, {"event_type": "a"}, {"event_type": "b"}]
        assert count_by(items, "event_type") == {"a": 2, "b": 1}
