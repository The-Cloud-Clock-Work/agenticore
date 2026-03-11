"""Unit tests for agenticore.agent_mode.openai_compat."""

import pytest

from agenticore.agent_mode.openai_compat import (
    build_openai_error,
    build_openai_response,
    extract_request_id,
    flatten_messages,
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
