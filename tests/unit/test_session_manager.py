"""Unit tests for session_manager module."""

import pytest

from agenticore.agent_mode.session_manager import (
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

    def test_rate_limit(self):
        err = detect_retryable_error("429 Too Many Requests", "")
        assert err is not None
        assert err.category == "rate_limit"

    def test_rate_limit_overloaded(self):
        err = detect_retryable_error("API overloaded", "")
        assert err is not None
        assert err.category == "rate_limit"

    def test_network_error(self):
        err = detect_retryable_error("", "connection refused")
        assert err is not None
        assert err.category == "network"

    def test_no_retryable_error(self):
        err = detect_retryable_error("Normal output", "")
        assert err is None


@pytest.mark.unit
class TestComposeRetryPrompt:
    def test_token_limit_retry(self):
        from agenticore.agent_mode.session_manager import RetryableError

        err = RetryableError(category="token_limit", message="token limit", suggestion="summarize")
        prompt = compose_retry_prompt("do the thing", err)
        assert "token limit" in prompt
        assert "do the thing" in prompt

    def test_other_error_passthrough(self):
        from agenticore.agent_mode.session_manager import RetryableError

        err = RetryableError(category="rate_limit", message="429", suggestion="wait")
        prompt = compose_retry_prompt("do the thing", err)
        assert prompt == "do the thing"


@pytest.mark.unit
class TestHandleSessionError:
    def test_session_conflict(self):
        result = handle_session_error("Error: session already in use")
        assert result == "session_conflict"

    def test_no_conversation(self):
        result = handle_session_error("Error: no conversation found for id")
        assert result == "no_conversation"

    def test_no_session_error(self):
        result = handle_session_error("Normal stderr output")
        assert result is None
