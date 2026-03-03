"""Unit tests for agenticore.agent_mode.notifications."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenticore.agent_mode.completions import _reset_redis
from agenticore.agent_mode.notifications import (
    NotificationConfig,
    get_notification_config,
    parse_notifications_param,
    save_notification_config,
    send_notification,
    update_notification_config,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    _reset_redis()
    config_file = tmp_path / "notification_configs.json"
    with patch.dict(os.environ, {"REDIS_URL": "", "REDIS_KEY_PREFIX": "test"}):
        with patch("agenticore.agent_mode.notifications._config_file", return_value=config_file):
            yield
    _reset_redis()


@pytest.mark.unit
class TestNotificationConfig:
    def test_defaults(self):
        c = NotificationConfig()
        assert c.status is True
        assert c.tool_call is False
        assert c.thinking is False
        assert c.enabled is True
        assert c.callback_url == ""

    def test_save_and_get(self, tmp_path):
        config = NotificationConfig(
            callback_url="https://example.com/hook",
            status=True,
            tool_call=True,
        )
        save_notification_config("n1", config)

        retrieved = get_notification_config("n1")
        assert retrieved is not None
        assert retrieved.callback_url == "https://example.com/hook"
        assert retrieved.status is True
        assert retrieved.tool_call is True
        assert retrieved.thinking is False

    def test_get_nonexistent(self):
        assert get_notification_config("nonexistent") is None

    def test_update(self, tmp_path):
        config = NotificationConfig(callback_url="https://example.com/hook")
        save_notification_config("n2", config)

        updated = update_notification_config("n2", tool_call=True, thinking=True)
        assert updated is not None
        assert updated.tool_call is True
        assert updated.thinking is True

        retrieved = get_notification_config("n2")
        assert retrieved.tool_call is True

    def test_update_nonexistent(self):
        assert update_notification_config("nope", tool_call=True) is None

    def test_disable_globally(self, tmp_path):
        config = NotificationConfig(
            callback_url="https://example.com/hook",
            enabled=False,
        )
        save_notification_config("n3", config)

        retrieved = get_notification_config("n3")
        assert retrieved.enabled is False


@pytest.mark.unit
class TestParseNotificationsParam:
    def test_parse_dict(self):
        result = parse_notifications_param({"status": True, "tool_call": True, "thinking": False})
        assert result == {"status": True, "tool_call": True, "thinking": False}

    def test_parse_string(self):
        result = parse_notifications_param("status,tool_call")
        assert result == {"status": True, "tool_call": True}

    def test_parse_empty_string(self):
        assert parse_notifications_param("") == {}

    def test_parse_none(self):
        assert parse_notifications_param(None) == {}

    def test_parse_dict_filters_unknown_keys(self):
        result = parse_notifications_param({"status": True, "bogus": True})
        assert "bogus" not in result
        assert result["status"] is True

    def test_parse_string_filters_unknown(self):
        result = parse_notifications_param("status,bogus,tool_call")
        assert "bogus" not in result
        assert result == {"status": True, "tool_call": True}


@pytest.mark.unit
class TestSendNotification:
    @pytest.mark.asyncio
    async def test_no_config_returns_false(self):
        result = await send_notification("nonexistent", "status", {"status": "started"})
        assert result is False

    @pytest.mark.asyncio
    async def test_disabled_returns_false(self, tmp_path):
        config = NotificationConfig(callback_url="https://example.com", enabled=False)
        save_notification_config("disabled-1", config)
        result = await send_notification("disabled-1", "status", {"status": "started"})
        assert result is False

    @pytest.mark.asyncio
    async def test_no_callback_url_returns_false(self, tmp_path):
        config = NotificationConfig(callback_url="", status=True)
        save_notification_config("no-url", config)
        result = await send_notification("no-url", "status", {"status": "started"})
        assert result is False

    @pytest.mark.asyncio
    async def test_type_not_enabled_returns_false(self, tmp_path):
        config = NotificationConfig(callback_url="https://example.com", tool_call=False)
        save_notification_config("tc-off", config)
        result = await send_notification("tc-off", "tool_call", {"tool_name": "Read"})
        assert result is False

    @pytest.mark.asyncio
    async def test_successful_delivery(self, tmp_path):
        config = NotificationConfig(callback_url="https://example.com/hook", status=True)
        save_notification_config("ok-1", config)

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_cfg = MagicMock()
        mock_cfg.agent_mode.notification_timeout = 5

        with (
            patch("agenticore.config.get_config", return_value=mock_cfg),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            result = await send_notification("ok-1", "status", {"status": "started"})

        assert result is True
        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        envelope = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert envelope["event_type"] == "status"
        assert envelope["correlation_id"] == "ok-1"

    @pytest.mark.asyncio
    async def test_delivery_failure_returns_false(self, tmp_path):
        config = NotificationConfig(callback_url="https://example.com/hook", status=True)
        save_notification_config("fail-1", config)

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        mock_cfg = MagicMock()
        mock_cfg.agent_mode.notification_timeout = 5

        with (
            patch("agenticore.config.get_config", return_value=mock_cfg),
            patch("httpx.AsyncClient", return_value=mock_client),
        ):
            result = await send_notification("fail-1", "status", {"status": "started"})

        assert result is False

    @pytest.mark.asyncio
    async def test_delivery_exception_returns_false(self, tmp_path):
        config = NotificationConfig(callback_url="https://example.com/hook", status=True)
        save_notification_config("exc-1", config)

        mock_cfg = MagicMock()
        mock_cfg.agent_mode.notification_timeout = 5

        with (
            patch("agenticore.config.get_config", return_value=mock_cfg),
            patch("httpx.AsyncClient", side_effect=Exception("connection refused")),
        ):
            result = await send_notification("exc-1", "status", {"status": "started"})

        assert result is False
