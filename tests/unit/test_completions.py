"""Unit tests for agenticore.agent_mode.completions."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from agenticore.agent_mode.completions import (
    Completion,
    _reset_redis,
    create_completion,
    get_completion,
    list_completions,
    update_completion,
    enqueue_completion,
    dequeue_completion,
)


@pytest.fixture(autouse=True)
def _clean(tmp_path):
    _reset_redis()
    with patch.dict(os.environ, {"REDIS_URL": "", "REDIS_KEY_PREFIX": "test"}):
        with patch("agenticore.agent_mode.completions._completions_dir", return_value=tmp_path):
            yield
    _reset_redis()


@pytest.mark.unit
class TestCompletionDataclass:
    def test_to_dict(self):
        c = Completion(uuid="abc", status="queued", message="hello")
        d = c.to_dict()
        assert d["uuid"] == "abc"
        assert d["status"] == "queued"
        assert d["message"] == "hello"

    def test_from_dict(self):
        data = {"uuid": "x", "status": "completed", "cost_usd": "1.5", "is_error": "true"}
        c = Completion.from_dict(data)
        assert c.uuid == "x"
        assert c.status == "completed"
        assert c.cost_usd == 1.5
        assert c.is_error is True

    def test_from_dict_json_fields(self):
        data = {
            "uuid": "x",
            "usage": '{"input_tokens": 100}',
            "tool_uses": '[{"name": "Read"}]',
            "request_params": '{"model": "sonnet"}',
        }
        c = Completion.from_dict(data)
        assert c.usage == {"input_tokens": 100}
        assert c.tool_uses == [{"name": "Read"}]
        assert c.request_params == {"model": "sonnet"}

    def test_from_dict_defaults(self):
        c = Completion.from_dict({})
        assert c.uuid == ""
        assert c.status == "queued"
        assert c.cost_usd == 0.0
        assert c.is_error is False

    def test_from_dict_invalid_json_fields(self):
        data = {"uuid": "x", "usage": "not-json", "tool_uses": "bad", "request_params": "nope"}
        c = Completion.from_dict(data)
        assert c.usage == {}
        assert c.tool_uses == []
        assert c.request_params == {}


@pytest.mark.unit
class TestCompletionCRUD:
    def test_create_and_get(self, tmp_path):
        c = create_completion(uuid="c1", message="test task")
        assert c.uuid == "c1"
        assert c.status == "queued"
        assert c.created_at != ""

        retrieved = get_completion("c1")
        assert retrieved is not None
        assert retrieved.uuid == "c1"
        assert retrieved.message == "test task"

    def test_get_nonexistent(self):
        assert get_completion("nonexistent") is None

    def test_update(self, tmp_path):
        create_completion(uuid="c2", message="initial")
        updated = update_completion("c2", status="running", started_at="2026-01-01T00:00:00Z")
        assert updated is not None
        assert updated.status == "running"
        assert updated.started_at == "2026-01-01T00:00:00Z"

        retrieved = get_completion("c2")
        assert retrieved.status == "running"

    def test_update_nonexistent(self):
        assert update_completion("nope", status="running") is None

    def test_list_completions(self, tmp_path):
        create_completion(uuid="l1", message="first")
        create_completion(uuid="l2", message="second")
        create_completion(uuid="l3", message="third")

        all_c = list_completions(limit=10)
        assert len(all_c) == 3

    def test_list_with_status_filter(self, tmp_path):
        create_completion(uuid="f1", message="task1")
        create_completion(uuid="f2", message="task2")
        update_completion("f2", status="completed")

        queued = list_completions(status="queued")
        assert len(queued) == 1
        assert queued[0].uuid == "f1"

        completed = list_completions(status="completed")
        assert len(completed) == 1
        assert completed[0].uuid == "f2"

    def test_list_with_limit(self, tmp_path):
        for i in range(5):
            create_completion(uuid=f"lim-{i}", message=f"task {i}")

        limited = list_completions(limit=3)
        assert len(limited) == 3

    def test_create_with_callback_and_params(self, tmp_path):
        c = create_completion(
            uuid="cp1",
            message="task",
            request_params={"model": "opus", "max_turns": 50},
        )
        assert c.request_params["model"] == "opus"

        retrieved = get_completion("cp1")
        assert retrieved.request_params["max_turns"] == 50


@pytest.mark.unit
class TestCompletionQueue:
    def test_enqueue_no_redis_returns_dict(self, tmp_path):
        c = create_completion(uuid="q1", message="task")
        result = enqueue_completion(c)
        assert result is not None
        assert result["uuid"] == "q1"

    def test_dequeue_no_redis_returns_none(self):
        assert dequeue_completion(timeout=1) is None

    def test_enqueue_with_redis(self, tmp_path):
        mock_redis = MagicMock()
        with patch("agenticore.agent_mode.completions._get_redis", return_value=mock_redis):
            c = Completion(uuid="rq1", message="task")
            result = enqueue_completion(c)
            assert result is None  # queued to Redis
            mock_redis.lpush.assert_called_once()

    def test_dequeue_with_redis_empty(self, tmp_path):
        mock_redis = MagicMock()
        mock_redis.brpop.return_value = None
        with patch("agenticore.agent_mode.completions._get_redis", return_value=mock_redis):
            result = dequeue_completion(timeout=1)
            assert result is None

    def test_dequeue_with_redis_data(self, tmp_path):
        payload = json.dumps({"uuid": "rq2", "message": "dequeued"})
        mock_redis = MagicMock()
        mock_redis.brpop.return_value = ("test:cq", payload)
        with patch("agenticore.agent_mode.completions._get_redis", return_value=mock_redis):
            result = dequeue_completion(timeout=1)
            assert result is not None
            assert result["uuid"] == "rq2"
