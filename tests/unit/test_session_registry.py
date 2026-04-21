"""Unit tests for session_registry module."""

import json
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
        assert mapping.claude_session_id == ""  # empty until first Claude call
        assert mapping.session_type == "persistent"
        assert mapping.status == "active"

    def test_register_persistent_reuses_existing(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-123", stateless=False)
        # Simulate first call populating the real session ID
        reg.update_claude_session_id("ext-123", "real-claude-id")
        m2 = reg.register("ext-123", stateless=False)
        assert m2.claude_session_id == "real-claude-id"

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
        assert mapping.claude_session_id == ""

    def test_completed_session_not_reused_by_persistent(self, _clean_sessions):
        """A completed persistent session should create a NEW mapping when re-registered."""
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-done", stateless=False)
        reg.mark_complete("ext-done")

        # Re-register — the completed one should not be reused
        mapping = reg.register("ext-done", stateless=False)
        assert mapping.status == "active"
        # New persistent session starts with empty claude_session_id
        assert mapping.claude_session_id == ""

    def test_failed_session_not_reused_by_persistent(self, _clean_sessions):
        """A failed persistent session should create a NEW mapping."""
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-err", stateless=False)
        reg.mark_failed("ext-err")

        mapping = reg.register("ext-err", stateless=False)
        assert mapping.status == "active"

    def test_stateless_session_type(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        mapping = reg.register("ext-sl", stateless=True)
        assert mapping.session_type == "stateless"
        # Stateless UUID is NOT the external UUID
        assert mapping.claude_session_id != "ext-sl"

    def test_timestamps_populated(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        mapping = reg.register("ext-ts", stateless=False)
        assert mapping.created_at != ""
        assert mapping.updated_at != ""

    def test_update_status_on_missing_is_noop(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.mark_complete("nonexistent")  # should not raise

    def test_corrupt_file_handled(self, _clean_sessions):
        """Corrupt sessions file doesn't crash."""
        from agenticore.agent_mode.session_registry import SessionRegistry

        _clean_sessions.write_text("{{not json}}")
        reg = SessionRegistry()
        assert reg.get("any") is None

    def test_update_claude_session_id(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-upd", stateless=False)
        reg.update_claude_session_id("ext-upd", "real-claude-abc")
        mapping = reg.get("ext-upd")
        assert mapping.claude_session_id == "real-claude-abc"

    def test_update_only_active_sessions(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-done2", stateless=False)
        reg.mark_complete("ext-done2")
        reg.update_claude_session_id("ext-done2", "should-not-stick")
        mapping = reg.get("ext-done2")
        assert mapping.claude_session_id == ""

    def test_reuse_established_session(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-est", stateless=False)
        reg.update_claude_session_id("ext-est", "real-id-xyz")
        # Re-register should reuse the established session
        m = reg.register("ext-est", stateless=False)
        assert m.claude_session_id == "real-id-xyz"

    def test_multiple_registrations_tracked(self, _clean_sessions):
        """Multiple distinct sessions coexist in the file."""
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("a", stateless=False)
        reg.register("b", stateless=False)
        reg.register("c", stateless=True)

        data = json.loads(_clean_sessions.read_text())
        assert "a" in data
        assert "b" in data
        assert "c" in data


@pytest.mark.unit
class TestSessionMappingDataclass:
    def test_to_dict_round_trip(self):
        from agenticore.agent_mode.session_registry import SessionMapping

        m = SessionMapping(
            external_uuid="ext",
            claude_session_id="claude",
            session_type="persistent",
            status="active",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z",
        )
        d = m.to_dict()
        m2 = SessionMapping.from_dict(d)
        assert m2.external_uuid == "ext"
        assert m2.claude_session_id == "claude"
        assert m2.session_type == "persistent"

    def test_from_dict_with_defaults(self):
        from agenticore.agent_mode.session_registry import SessionMapping

        m = SessionMapping.from_dict({})
        assert m.external_uuid == ""
        assert m.status == "active"
        assert m.session_type == "persistent"


@pytest.mark.unit
class TestSessionRegistryUnhappyPaths:
    def test_save_raises_on_permission_error(self, _clean_sessions):
        """PermissionError on file write propagates (documents current behavior)."""
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        with patch("agenticore.agent_mode.session_registry._save_file_sessions", side_effect=PermissionError("denied")):
            with pytest.raises(PermissionError):
                reg.register("ext-perm", stateless=False)

    def test_from_dict_ignores_extra_keys(self):
        """Extra keys in the dict are silently ignored."""
        from agenticore.agent_mode.session_registry import SessionMapping

        m = SessionMapping.from_dict(
            {
                "external_uuid": "ext",
                "claude_session_id": "claude",
                "session_type": "persistent",
                "status": "active",
                "created_at": "2024-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "unknown_field": "should be ignored",
                "another_extra": 42,
            }
        )
        assert m.external_uuid == "ext"
        assert m.claude_session_id == "claude"

    def test_register_after_unknown_status_creates_new(self, _clean_sessions):
        """Session with a non-active status is not reused by persistent register."""
        from agenticore.agent_mode.session_registry import SessionRegistry

        reg = SessionRegistry()
        reg.register("ext-unknown", stateless=False)
        # Manually set status to something unusual
        reg._update_status("ext-unknown", "unknown")

        mapping = reg.register("ext-unknown", stateless=False)
        assert mapping.status == "active"


@pytest.mark.unit
class TestModuleLevelConvenience:
    """Test the module-level convenience functions that delegate to _registry."""

    def test_register_session(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import register_session

        mapping = register_session("conv-1", stateless=False)
        assert mapping.external_uuid == "conv-1"

    def test_mark_session_complete(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import (
            mark_session_complete,
            register_session,
            resolve_external_uuid,
        )

        register_session("conv-2", stateless=False)
        mark_session_complete("conv-2")
        m = resolve_external_uuid("conv-2")
        assert m.status == "completed"

    def test_mark_session_failed(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import (
            mark_session_failed,
            register_session,
            resolve_external_uuid,
        )

        register_session("conv-3", stateless=False)
        mark_session_failed("conv-3")
        m = resolve_external_uuid("conv-3")
        assert m.status == "failed"

    def test_resolve_missing_returns_none(self, _clean_sessions):
        from agenticore.agent_mode.session_registry import resolve_external_uuid

        assert resolve_external_uuid("missing") is None
