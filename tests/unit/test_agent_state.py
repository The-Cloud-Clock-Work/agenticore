"""Unit tests for agent_mode/state.py."""

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
    from agenticore.jobs import _reset_redis

    _reset_redis()
    with patch.dict(os.environ, {"REDIS_URL": ""}, clear=False):
        _reset_redis()
        yield
    _reset_redis()


@pytest.fixture()
def _clean_state(tmp_path, _no_redis):
    """Redirect state file to tmp_path."""
    state_file = tmp_path / "conversation_map.json"
    with patch("agenticore.agent_mode.state._state_file", return_value=state_file):
        yield state_file


@pytest.mark.unit
class TestSaveState:
    def test_saves_basic_state(self, _clean_state):
        from agenticore.agent_mode.state import save_state

        state = save_state("uuid-1", wait=True, meta={"platform": "teams"})
        assert state["uuid"] == "uuid-1"
        assert state["wait"] is True
        assert state["meta"] == {"platform": "teams"}
        assert "updated_at" in state

    def test_saves_to_file(self, _clean_state):
        from agenticore.agent_mode.state import save_state

        save_state("uuid-2", wait=False)

        data = json.loads(_clean_state.read_text())
        assert "uuid-2" in data
        assert data["uuid-2"]["wait"] is False

    def test_meta_defaults_to_empty(self, _clean_state):
        from agenticore.agent_mode.state import save_state

        state = save_state("uuid-3")
        assert state["meta"] == {}

    def test_overwrites_previous_state(self, _clean_state):
        from agenticore.agent_mode.state import save_state

        save_state("uuid-4", wait=True, meta={"v": 1})
        save_state("uuid-4", wait=False, meta={"v": 2})

        data = json.loads(_clean_state.read_text())
        assert data["uuid-4"]["wait"] is False
        assert data["uuid-4"]["meta"]["v"] == 2

    def test_multiple_uuids_coexist(self, _clean_state):
        from agenticore.agent_mode.state import save_state

        save_state("a", wait=True)
        save_state("b", wait=False)

        data = json.loads(_clean_state.read_text())
        assert "a" in data
        assert "b" in data


@pytest.mark.unit
class TestGetSessionState:
    def test_returns_saved_state(self, _clean_state):
        from agenticore.agent_mode.state import get_session_state, save_state

        save_state("uuid-get", wait=True, meta={"user": "john"})
        result = get_session_state("uuid-get")
        assert result is not None
        assert result["uuid"] == "uuid-get"
        assert result["meta"]["user"] == "john"

    def test_returns_none_for_missing(self, _clean_state):
        from agenticore.agent_mode.state import get_session_state

        assert get_session_state("missing") is None

    def test_handles_corrupt_file(self, _clean_state):
        from agenticore.agent_mode.state import get_session_state

        _clean_state.write_text("not valid json {{{")
        assert get_session_state("any") is None

    def test_handles_empty_file(self, _clean_state):
        from agenticore.agent_mode.state import get_session_state

        _clean_state.write_text("")
        assert get_session_state("any") is None
