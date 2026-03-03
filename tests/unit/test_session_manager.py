"""Unit tests for session_manager module."""

import pytest

from agenticore.agent_mode.session_manager import (
    RetryableError,
    compose_retry_prompt,
    detect_retryable_error,
    handle_session_error,
)


@pytest.mark.unit
class TestDetectRetryableError:
    def test_token_limit(self):
        err = detect_retryable_error("Error: context window exceeded", "")
        assert err is not None
        assert err.category == "token_limit"

    def test_token_limit_in_stderr(self):
        err = detect_retryable_error("", "token limit reached")
        assert err is not None
        assert err.category == "token_limit"

    def test_context_too_long(self):
        err = detect_retryable_error("Error: context is too long for this model", "")
        assert err is not None
        assert err.category == "token_limit"

    def test_rate_limit(self):
        err = detect_retryable_error("429 Too Many Requests", "")
        assert err is not None
        assert err.category == "rate_limit"

    def test_rate_limit_overloaded(self):
        err = detect_retryable_error("API overloaded", "")
        assert err is not None
        assert err.category == "rate_limit"

    def test_too_many_requests(self):
        err = detect_retryable_error("too many requests, slow down", "")
        assert err is not None
        assert err.category == "rate_limit"

    def test_network_error(self):
        err = detect_retryable_error("", "connection refused")
        assert err is not None
        assert err.category == "network"

    def test_connection_reset(self):
        err = detect_retryable_error("", "connection reset by peer")
        assert err is not None
        assert err.category == "network"

    def test_connection_timeout(self):
        err = detect_retryable_error("", "connection timeout")
        assert err is not None
        assert err.category == "network"

    def test_econnrefused(self):
        err = detect_retryable_error("ECONNREFUSED 127.0.0.1:8080", "")
        assert err is not None
        assert err.category == "network"

    def test_network_error_generic(self):
        err = detect_retryable_error("", "network error occurred")
        assert err is not None
        assert err.category == "network"

    def test_no_retryable_error(self):
        err = detect_retryable_error("Normal output", "")
        assert err is None

    def test_empty_input(self):
        err = detect_retryable_error("", "")
        assert err is None

    def test_case_insensitive(self):
        err = detect_retryable_error("TOKEN LIMIT EXCEEDED", "")
        assert err is not None
        assert err.category == "token_limit"

    def test_error_in_stderr_only(self):
        err = detect_retryable_error("clean output", "RATE LIMIT hit")
        assert err is not None
        assert err.category == "rate_limit"

    def test_first_match_wins(self):
        """When multiple patterns match, first pattern found wins."""
        err = detect_retryable_error("token limit and 429 rate limit", "")
        assert err is not None
        assert err.category == "token_limit"

    def test_retryable_error_has_suggestion(self):
        err = detect_retryable_error("token limit reached", "")
        assert err.suggestion != ""

    def test_retryable_error_has_message(self):
        err = detect_retryable_error("context window exceeded here", "")
        assert "context" in err.message.lower()


@pytest.mark.unit
class TestComposeRetryPrompt:
    def test_token_limit_retry(self):
        err = RetryableError(category="token_limit", message="token limit", suggestion="summarize")
        prompt = compose_retry_prompt("do the thing", err)
        assert "token limit" in prompt
        assert "do the thing" in prompt

    def test_rate_limit_passthrough(self):
        err = RetryableError(category="rate_limit", message="429", suggestion="wait")
        prompt = compose_retry_prompt("do the thing", err)
        assert prompt == "do the thing"

    def test_network_error_passthrough(self):
        err = RetryableError(category="network", message="connection refused", suggestion="retry")
        prompt = compose_retry_prompt("my task", err)
        assert prompt == "my task"

    def test_token_limit_includes_summarize_instruction(self):
        err = RetryableError(category="token_limit", message="limit", suggestion="")
        prompt = compose_retry_prompt("original", err)
        assert "Summarize" in prompt


@pytest.mark.unit
class TestHandleSessionError:
    def test_session_conflict(self):
        result = handle_session_error("Error: session already in use")
        assert result == "session_conflict"

    def test_session_conflict_case_insensitive(self):
        result = handle_session_error("Session Already In Use by another process")
        assert result == "session_conflict"

    def test_no_conversation(self):
        result = handle_session_error("Error: no conversation found for id")
        assert result == "no_conversation"

    def test_no_conversation_case_insensitive(self):
        result = handle_session_error("No Conversation Found")
        assert result == "no_conversation"

    def test_no_session_error(self):
        result = handle_session_error("Normal stderr output")
        assert result is None

    def test_empty_stderr(self):
        result = handle_session_error("")
        assert result is None


@pytest.mark.unit
class TestRetryableErrorDataclass:
    def test_fields(self):
        err = RetryableError(category="test", message="msg", suggestion="hint")
        assert err.category == "test"
        assert err.message == "msg"
        assert err.suggestion == "hint"
