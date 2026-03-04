"""Unit tests for agenticore.agent_mode.worker."""

import os
from unittest.mock import AsyncMock, patch

import pytest

from agenticore.agent_mode.completions import _reset_redis


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    _reset_redis()
    with patch.dict(os.environ, {"REDIS_URL": "", "REDIS_KEY_PREFIX": "test"}):
        with patch("agenticore.agent_mode.completions._completions_dir", return_value=tmp_path):
            yield
    _reset_redis()


@pytest.mark.unit
class TestProcessCompletion:
    @pytest.mark.asyncio
    async def test_successful_completion(self, tmp_path):
        """Worker processes a completion and updates status."""
        from agenticore.agent_mode.completions import create_completion, get_completion
        from agenticore.agent_mode.worker import _process_completion

        create_completion(uuid="w1", message="test task")

        mock_result = {
            "result": "Done!",
            "session_id": "s1",
            "cost_usd": 0.05,
            "duration_ms": 2000,
            "num_turns": 3,
            "is_error": False,
            "error": "",
            "usage": {"input_tokens": 100},
            "tool_uses": [],
        }

        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=mock_result)

        with (
            patch("agenticore.agent_mode.agent.AgentExecutor", return_value=mock_executor),
            patch("agenticore.agent_mode.notifications.send_notification", new_callable=AsyncMock),
        ):
            await _process_completion({"uuid": "w1", "message": "test task", "request_params": {}})

        c = get_completion("w1")
        assert c is not None
        assert c.status == "completed"
        assert c.result == "Done!"
        assert c.cost_usd == 0.05
        assert c.num_turns == 3

    @pytest.mark.asyncio
    async def test_failed_completion(self, tmp_path):
        """Worker marks completion as failed on error result."""
        from agenticore.agent_mode.completions import create_completion, get_completion
        from agenticore.agent_mode.worker import _process_completion

        create_completion(uuid="w2", message="fail task")

        mock_result = {
            "result": "oops",
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 500,
            "num_turns": 1,
            "is_error": True,
            "error": "something broke",
            "usage": {},
            "tool_uses": [],
        }

        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=mock_result)

        with (
            patch("agenticore.agent_mode.agent.AgentExecutor", return_value=mock_executor),
            patch("agenticore.agent_mode.notifications.send_notification", new_callable=AsyncMock),
        ):
            await _process_completion({"uuid": "w2", "message": "fail task", "request_params": {}})

        c = get_completion("w2")
        assert c is not None
        assert c.status == "failed"
        assert c.is_error is True

    @pytest.mark.asyncio
    async def test_exception_during_execution(self, tmp_path):
        """Worker handles executor exceptions gracefully."""
        from agenticore.agent_mode.completions import create_completion, get_completion
        from agenticore.agent_mode.worker import _process_completion

        create_completion(uuid="w3", message="crash task")

        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(side_effect=RuntimeError("boom"))

        with (
            patch("agenticore.agent_mode.agent.AgentExecutor", return_value=mock_executor),
            patch("agenticore.agent_mode.notifications.send_notification", new_callable=AsyncMock),
        ):
            await _process_completion({"uuid": "w3", "message": "crash task", "request_params": {}})

        c = get_completion("w3")
        assert c is not None
        assert c.status == "failed"
        assert "boom" in c.error

    @pytest.mark.asyncio
    async def test_sends_notifications(self, tmp_path):
        """Worker sends status notifications during processing."""
        from agenticore.agent_mode.completions import create_completion
        from agenticore.agent_mode.worker import _process_completion

        create_completion(uuid="w4", message="notify task")

        mock_result = {
            "result": "ok",
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 100,
            "num_turns": 1,
            "is_error": False,
            "error": "",
            "usage": {},
            "tool_uses": [],
        }

        mock_notify = AsyncMock()
        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=mock_result)

        with (
            patch("agenticore.agent_mode.agent.AgentExecutor", return_value=mock_executor),
            patch("agenticore.agent_mode.notifications.send_notification", mock_notify),
        ):
            await _process_completion(
                {
                    "uuid": "w4",
                    "message": "notify task",
                    "callback_url": "https://example.com/hook",
                    "request_params": {},
                }
            )

        # Should have at least 2 notifications: started + completed
        assert mock_notify.call_count >= 2
        calls = [c.args for c in mock_notify.call_args_list]
        event_types = [c[1] for c in calls]
        assert "status" in event_types


@pytest.mark.unit
class TestRunInlineCompletion:
    @pytest.mark.asyncio
    async def test_inline_execution(self, tmp_path):
        """run_inline_completion processes without queue."""
        from agenticore.agent_mode.completions import create_completion, get_completion
        from agenticore.agent_mode.worker import run_inline_completion

        create_completion(uuid="il1", message="inline task")

        mock_result = {
            "result": "inline done",
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 100,
            "num_turns": 1,
            "is_error": False,
            "error": "",
            "usage": {},
            "tool_uses": [],
        }

        mock_executor = AsyncMock()
        mock_executor.execute = AsyncMock(return_value=mock_result)

        with (
            patch("agenticore.agent_mode.agent.AgentExecutor", return_value=mock_executor),
            patch("agenticore.agent_mode.notifications.send_notification", new_callable=AsyncMock),
        ):
            await run_inline_completion({"uuid": "il1", "message": "inline task", "request_params": {}})

        c = get_completion("il1")
        assert c.status == "completed"
        assert c.result == "inline done"
