"""Unit tests for agenticore.agent_mode.openai_compat."""

import json

import pytest

from agenticore.agent_mode.openai_compat import (
    build_openai_error,
    build_openai_response,
    extract_request_id,
    flatten_messages,
    format_done,
    format_event_as_sse,
    format_role_open_chunk,
    format_status_meta,
    format_stop_chunk,
    format_text_delta,
    format_thinking_delta,
    format_tool_result_delta,
    format_tool_use_delta,
)


@pytest.mark.unit
class TestFlattenMessages:
    def test_single_user_message(self):
        msgs = [{"role": "user", "content": "Hello agent"}]
        assert flatten_messages(msgs) == "Hello agent"

    def test_empty_list(self):
        assert flatten_messages([]) == ""

    def test_multi_turn(self):
        msgs = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello!"},
            {"role": "user", "content": "Do something"},
        ]
        result = flatten_messages(msgs)
        assert result == ("[system] You are helpful\n[user] Hi\n[assistant] Hello!\n[user] Do something")

    def test_system_only(self):
        msgs = [{"role": "system", "content": "Be concise"}]
        assert flatten_messages(msgs) == "Be concise"

    def test_two_messages_get_prefixes(self):
        msgs = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Question"},
        ]
        result = flatten_messages(msgs)
        assert "[system] System prompt" in result
        assert "[user] Question" in result

    def test_missing_content(self):
        msgs = [{"role": "user"}, {"role": "assistant", "content": "ok"}]
        result = flatten_messages(msgs)
        assert "[user] \n[assistant] ok" == result

    def test_missing_role(self):
        msgs = [{"content": "hello"}, {"role": "user", "content": "world"}]
        result = flatten_messages(msgs)
        assert "[user] hello" in result
        assert "[user] world" in result


@pytest.mark.unit
class TestBuildOpenaiResponse:
    def test_success_response(self):
        result = {
            "result": "Here is the draft.",
            "is_error": False,
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        resp = build_openai_response(result, "publishing-agent", "req-123")

        assert resp["id"] == "chatcmpl-req-123"
        assert resp["object"] == "chat.completion"
        assert resp["model"] == "publishing-agent"
        assert resp["choices"][0]["message"]["role"] == "assistant"
        assert resp["choices"][0]["message"]["content"] == "Here is the draft."
        assert resp["choices"][0]["finish_reason"] == "stop"
        assert resp["usage"]["prompt_tokens"] == 100
        assert resp["usage"]["completion_tokens"] == 50
        assert resp["usage"]["total_tokens"] == 150

    def test_error_response(self):
        result = {
            "result": "Something went wrong",
            "is_error": True,
            "usage": {},
        }
        resp = build_openai_response(result, "test-agent", "req-456")
        assert resp["choices"][0]["finish_reason"] == "error"

    def test_missing_usage(self):
        result = {"result": "ok", "is_error": False}
        resp = build_openai_response(result, "agent", "req-789")
        assert resp["usage"]["prompt_tokens"] == 0
        assert resp["usage"]["completion_tokens"] == 0
        assert resp["usage"]["total_tokens"] == 0

    def test_created_is_integer(self):
        result = {"result": "ok"}
        resp = build_openai_response(result, "agent", "req-001")
        assert isinstance(resp["created"], int)
        assert resp["created"] > 0

    def test_empty_result(self):
        resp = build_openai_response({}, "agent", "req-002")
        assert resp["choices"][0]["message"]["content"] == ""
        assert resp["choices"][0]["finish_reason"] == "stop"


@pytest.mark.unit
class TestBuildOpenaiError:
    def test_error_format(self):
        body, status = build_openai_error("something broke", 500)
        assert body["error"]["message"] == "something broke"
        assert body["error"]["type"] == "server_error"
        assert status == 500

    def test_custom_status(self):
        body, status = build_openai_error("bad request", 400)
        assert status == 400

    def test_not_found(self):
        body, status = build_openai_error("not found", 404)
        assert status == 404
        assert body["error"]["message"] == "not found"


@pytest.mark.unit
class TestExtractRequestId:
    def test_from_header(self):
        headers = {"x-request-id": "hdr-123"}
        assert extract_request_id(headers, {}) == "hdr-123"

    def test_generates_uuid_when_missing(self):
        rid = extract_request_id(None, {})
        assert len(rid) == 36  # UUID format

    def test_generates_uuid_for_empty_headers(self):
        rid = extract_request_id({}, {})
        assert len(rid) == 36


def _parse_sse_chunk(s: str) -> dict:
    assert s.startswith("data: ")
    assert s.endswith("\n\n")
    return json.loads(s[6:-2])


@pytest.mark.unit
class TestSseFormatters:
    def test_role_open_chunk(self):
        s = format_role_open_chunk("sonnet", "rid-1")
        d = _parse_sse_chunk(s)
        assert d["object"] == "chat.completion.chunk"
        assert d["choices"][0]["delta"] == {"role": "assistant"}
        assert d["choices"][0]["finish_reason"] is None

    def test_text_delta(self):
        s = format_text_delta("hello world", "sonnet", "rid-2")
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["delta"]["content"] == "hello world"
        assert d["choices"][0]["finish_reason"] is None

    def test_thinking_delta_marked(self):
        s = format_thinking_delta("I should think", "sonnet", "rid-3")
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["delta"]["reasoning_content"] == "I should think"
        assert d["choices"][0]["x_agenticore_event_type"] == "thinking"

    def test_tool_use_delta(self):
        tu = json.dumps({"id": "tu_1", "name": "Bash", "input": {"command": "ls"}})
        s = format_tool_use_delta(tu, "sonnet", "rid-4")
        d = _parse_sse_chunk(s)
        content = d["choices"][0]["delta"]["reasoning_content"]
        assert "tool_use: Bash" in content
        assert "ls" in content
        assert d["choices"][0]["x_agenticore_event_type"] == "tool_use"
        assert d["choices"][0]["x_agenticore_tool_name"] == "Bash"
        assert d["choices"][0]["x_agenticore_tool_use_id"] == "tu_1"

    def test_tool_use_delta_handles_garbage(self):
        s = format_tool_use_delta("not json", "sonnet", "rid-4b")
        d = _parse_sse_chunk(s)
        content = d["choices"][0]["delta"]["reasoning_content"]
        assert "tool_use: tool" in content

    def test_tool_result_delta(self):
        tr = json.dumps({"tool_use_id": "tu_1", "is_error": False, "output": "rows: 5"})
        s = format_tool_result_delta(tr, "sonnet", "rid-5")
        d = _parse_sse_chunk(s)
        content = d["choices"][0]["delta"]["reasoning_content"]
        assert "rows: 5" in content
        assert "tool_result" in content
        assert d["choices"][0]["x_agenticore_event_type"] == "tool_result"
        assert d["choices"][0]["x_agenticore_tool_use_id"] == "tu_1"
        assert d["choices"][0]["x_agenticore_is_error"] is False

    def test_tool_result_propagates_is_error(self):
        tr = json.dumps({"tool_use_id": "x", "is_error": True, "output": "boom"})
        s = format_tool_result_delta(tr, "sonnet", "rid-5b")
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["x_agenticore_is_error"] is True

    def test_stop_chunk_with_usage(self):
        s = format_stop_chunk({"input_tokens": 50, "output_tokens": 100}, "sonnet", "rid-6")
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["finish_reason"] == "stop"
        assert d["usage"]["prompt_tokens"] == 50
        assert d["usage"]["completion_tokens"] == 100
        assert d["usage"]["total_tokens"] == 150

    def test_stop_chunk_no_usage(self):
        s = format_stop_chunk({}, "sonnet", "rid-6b")
        d = _parse_sse_chunk(s)
        assert d["usage"]["total_tokens"] == 0

    def test_done_marker(self):
        assert format_done() == "data: [DONE]\n\n"

    def test_status_meta(self):
        cfg = {"show_thinking": True, "show_tools": False, "show_text": True}
        s = format_status_meta(cfg, "sonnet", "rid-7")
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["x_agenticore_event_type"] == "stream_config"
        assert json.loads(d["choices"][0]["delta"]["content"]) == cfg


@pytest.mark.unit
class TestFormatEventAsSse:
    def test_assistant_text(self):
        s = format_event_as_sse(
            {"event_type": "assistant_text", "content": "hi"},
            "m",
            "r",
        )
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["delta"]["content"] == "hi"

    def test_thinking(self):
        s = format_event_as_sse(
            {"event_type": "thinking", "content": "think"},
            "m",
            "r",
        )
        d = _parse_sse_chunk(s)
        assert d["choices"][0]["x_agenticore_event_type"] == "thinking"

    def test_tool_use(self):
        s = format_event_as_sse(
            {"event_type": "tool_use", "content": json.dumps({"id": "x", "name": "B"})},
            "m",
            "r",
        )
        d = _parse_sse_chunk(s)
        assert "tool_use: B" in d["choices"][0]["delta"]["reasoning_content"]

    def test_tool_result(self):
        s = format_event_as_sse(
            {"event_type": "tool_result", "content": json.dumps({"output": "ok"})},
            "m",
            "r",
        )
        d = _parse_sse_chunk(s)
        assert "ok" in d["choices"][0]["delta"]["reasoning_content"]

    def test_unknown_returns_none(self):
        assert format_event_as_sse({"event_type": "garbage", "content": ""}, "m", "r") is None

    def test_done_returns_none(self):
        assert format_event_as_sse({"event_type": "done", "content": ""}, "m", "r") is None
