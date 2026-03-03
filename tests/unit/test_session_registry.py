"""Unit tests for session_registry module."""

import os
from unittest.mock import patch

import pytest

from agenticore.config import reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


@pytest.fixture()
def _no_redis():
    """Ensure no Redis connection for file-fallback tests."""
    from agenticore.jobs import _reset_redis

    _reset_redis()
    with patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
        _reset_redis()
        yield
    _reset_redis()


@pytest.fixture()
def _clean_sessions(tmp_path, _no_redis):
    """Use a temp sessions file."""
    sessions_file = tmp_path / "agent_sessions.json"
    with patch("agenticore.agent_mode.session_registry._sessions_file", return_value=sessions_file):
        yield sessions_file


@pytest.mark.unit
class TestSessionRegistry:
    def test_register_persistent(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        mapping = reg.register("ext-123", stateless=False)
        assert mapping.external_uuid == "ext-123"
        assert mapping.claude_session_id == "ext-123"  # same as external for persistent
        assert mapping.session_type == "persistent"
        assert mapping.status == "active"

    def test_register_persistent_reuses_existing(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        m1 = reg.register("ext-123", stateless=False)
        m2 = reg.register("ext-123", stateless=False)
        assert m1.claude_session_id == m2.claude_session_id

    def test_register_stateless_always_fresh(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        m1 = reg.register("ext-456", stateless=True)
        m2 = reg.register("ext-456", stateless=True)
        assert m1.claude_session_id != m2.claude_session_id
        assert m1.session_type == "stateless"

    def test_get_missing(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        assert reg.get("nonexistent") is None

    def test_mark_complete(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-789", stateless=False)
        reg.mark_complete("ext-789")
        mapping = reg.get("ext-789")
        assert mapping.status == "completed"

    def test_mark_failed(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-fail", stateless=False)
        reg.mark_failed("ext-fail")
        mapping = reg.get("ext-fail")
        assert mapping.status == "failed"

    def test_file_persistence(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg1 = SessionRegistry()
        reg1.register("ext-persist", stateless=False)

        reg2 = SessionRegistry()
        mapping = reg2.get("ext-persist")
        assert mapping is not None
        assert mapping.claude_session_id == "ext-persist"
