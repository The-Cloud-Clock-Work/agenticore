"""Unit tests for agenticore.agent_mode.hook_notifier."""

import json
from unittest.mock import patch

import pytest

from agenticore.agent_mode.hook_notifier import (
    _is_enabled,
    _load_config_file,
    _map_hook_event,
    _send_notification,
)


@pytest.mark.unit
class TestMapHookEvent:
    def test_post_tool_use(self):
        event = {"tool_name": "Write", "file_path": "/src/app.py", "description": "wrote file"}
        event_type, payload = _map_hook_event("PostToolUse", event)
        assert event_type == "tool_call"
        assert payload["tool_name"] == "Write"
        assert payload["file_path"] == "/src/app.py"

    def test_post_tool_use_system_tool_skipped(self):
        event = {"tool_name": "system/init"}
        event_type, _ = _map_hook_event("PostToolUse", event)
        assert event_type is None

    def test_subprocess_output_thinking(self):
        event = {"type": "thinking", "content": "Let me analyze..."}
        event_type, payload = _map_hook_event("SubprocessOutputLine", event)
        assert event_type == "thinking"
        assert payload["content"] == "Let me analyze..."

    def test_subprocess_output_non_thinking(self):
        event = {"type": "output", "content": "some output"}
        event_type, _ = _map_hook_event("SubprocessOutputLine", event)
        assert event_type is None

    def test_notification(self):
        event = {"message": "Task completed"}
        event_type, payload = _map_hook_event("Notification", event)
        assert event_type == "status"
        assert payload["message"] == "Task completed"

    def test_unknown_hook(self):
        event_type, _ = _map_hook_event("UnknownHook", {})
        assert event_type is None


@pytest.mark.unit
class TestIsEnabled:
    def test_enabled_true(self):
        config = {"enabled": "true", "status": "true"}
        assert _is_enabled(config, "status") is True

    def test_enabled_false(self):
        config = {"enabled": "true", "tool_call": "false"}
        assert _is_enabled(config, "tool_call") is False

    def test_globally_disabled(self):
        config = {"enabled": "false", "status": "true"}
        assert _is_enabled(config, "status") is False

    def test_missing_field_defaults_false(self):
        config = {"enabled": "true"}
        assert _is_enabled(config, "thinking") is False

    def test_bool_values(self):
        config = {"enabled": True, "status": True}
        assert _is_enabled(config, "status") is True


@pytest.mark.unit
class TestLoadConfigFile:
    def test_load_existing_config(self, tmp_path):
        config_dir = tmp_path / ".agenticore"
        config_dir.mkdir()
        config_file = config_dir / "notification_configs.json"
        config_file.write_text(
            json.dumps(
                {
                    "uuid-1": {"callback_url": "https://example.com", "status": "true"},
                }
            )
        )

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _load_config_file("uuid-1")

        assert result is not None
        assert result["callback_url"] == "https://example.com"

    def test_load_missing_correlation_id(self, tmp_path):
        config_dir = tmp_path / ".agenticore"
        config_dir.mkdir()
        config_file = config_dir / "notification_configs.json"
        config_file.write_text(json.dumps({"other-id": {}}))

        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _load_config_file("nonexistent")

        assert result is None

    def test_load_missing_file(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            result = _load_config_file("any")

        assert result is None


@pytest.mark.unit
class TestSendNotification:
    def test_sends_post_request(self):
        with patch("urllib.request.urlopen") as mock_urlopen:
            _send_notification("https://example.com/hook", "id-1", "status", {"status": "started"})

        mock_urlopen.assert_called_once()
        req = mock_urlopen.call_args[0][0]
        assert req.full_url == "https://example.com/hook"
        assert req.method == "POST"

        body = json.loads(req.data)
        assert body["correlation_id"] == "id-1"
        assert body["event_type"] == "status"
        assert body["data"]["status"] == "started"

    def test_handles_exception_gracefully(self):
        with patch(
            "urllib.request.urlopen",
            side_effect=Exception("connection refused"),
        ):
            # Should not raise
            _send_notification("https://bad.url", "id-1", "status", {})
