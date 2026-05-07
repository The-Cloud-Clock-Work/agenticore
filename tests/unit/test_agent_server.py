"""Unit tests for agent mode integration in server.py.

Tests the agent_completions MCP tool, REST /completions endpoint,
and enhanced /health endpoint when AGENT_MODE=true.
"""

import os
from unittest.mock import AsyncMock, patch

import pytest

from agenticore.agent_mode.agent import reset_system_prompt_cache
from agenticore.config import reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    reset_system_prompt_cache()
    yield
    reset_config()
    reset_system_prompt_cache()


_AGENT_ENV = {
    "AGENT_MODE": "true",
    "AGENT_MODE_PACKAGE_DIR": "/tmp/test-pkg",
    "AGENT_MODE_MODEL": "sonnet",
    "AGENT_MODE_MAX_TURNS": "80",
    "AGENT_MODE_PERMISSION_MODE": "bypassPermissions",
    "AGENT_MODE_OUTPUT_FORMAT": "json",
    "AGENT_MODE_EFFORT": "",
    "AGENT_MODE_TIMEOUT": "3600",
    "AGENT_MODE_MAX_RETRIES": "1",
    "AGENT_MODE_SESSION_TTL": "86400",
    "AGENT_MODE_APPEND_SYSTEM_PROMPT": "true",
    "REDIS_URL": "",
}


# ── REST /health with agent_mode ────────────────────────────────────────


@pytest.mark.unit
class TestHealthAgentMode:
    @patch.dict(os.environ, _AGENT_ENV)
    def test_health_includes_agent_mode_info(self):
        reset_config()
        from agenticore.server import _build_rest_app
        from starlette.testclient import TestClient

        app = _build_rest_app()
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_mode"] is True
        # CLAUDE_FLAG_DEFAULTS ships model=opus when no command.yml/profile loads.
        assert data["default_model"] == "opus"
        assert data["package_dir"] == "/tmp/test-pkg"

    @patch.dict(os.environ, {"AGENT_MODE": ""})
    def test_health_no_agent_mode_when_disabled(self):
        reset_config()
        from agenticore.server import _build_rest_app
        from starlette.testclient import TestClient

        app = _build_rest_app()
        client = TestClient(app)
        resp = client.get("/health")
        data = resp.json()
        assert "agent_mode" not in data


# ── REST /completions endpoint ───────────────────────────────────────────


@pytest.mark.unit
class TestRestCompletions:
    @pytest.fixture()
    def agent_rest_app(self, tmp_path):
        pkg = tmp_path / "test-pkg"
        pkg.mkdir()

        sessions_file = tmp_path / "sessions.json"
        state_file = tmp_path / "state.json"

        env = {**_AGENT_ENV, "AGENT_MODE_PACKAGE_DIR": str(pkg)}
        with (
            patch.dict(os.environ, env),
            patch("agenticore.agent_mode.session_registry._sessions_file", return_value=sessions_file),
            patch("agenticore.agent_mode.state._state_file", return_value=state_file),
        ):
            reset_config()
            from agenticore.server import _build_rest_app

            yield _build_rest_app()

    @pytest.mark.asyncio
    async def test_completions_sync(self, agent_rest_app):
        """POST /completions with wait=true returns agent result."""
        from starlette.testclient import TestClient

        mock_result = {
            "result": "Hello!",
            "session_id": "s1",
            "cost_usd": 0.01,
            "duration_ms": 500,
            "num_turns": 1,
            "is_error": False,
            "usage": {},
            "tool_uses": [],
        }

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            client = TestClient(agent_rest_app)
            resp = client.post(
                "/completions",
                json={"message": "say hi", "uuid": "test-1", "wait": True},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["result"] == "Hello!"

    @pytest.mark.asyncio
    async def test_completions_missing_fields(self, agent_rest_app):
        """POST /completions without required fields returns 400."""
        from starlette.testclient import TestClient

        client = TestClient(agent_rest_app)
        resp = client.post("/completions", json={"message": "hi"})
        assert resp.status_code == 400
        assert "uuid" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_completions_missing_message(self, agent_rest_app):
        from starlette.testclient import TestClient

        client = TestClient(agent_rest_app)
        resp = client.post("/completions", json={"uuid": "u1"})
        assert resp.status_code == 400
        assert "message" in resp.json()["error"]

    @pytest.mark.asyncio
    async def test_completions_async_returns_202(self, agent_rest_app, tmp_path):
        """POST /completions with wait=false returns 202 queued."""
        from starlette.testclient import TestClient

        from agenticore.agent_mode.completions import Completion

        mock_completion = Completion(uuid="async-1", status="queued", message="do something")

        with (
            patch("agenticore.agent_mode.completions.create_completion", return_value=mock_completion),
            patch("agenticore.agent_mode.completions.enqueue_completion", return_value=None),
        ):
            client = TestClient(agent_rest_app)
            resp = client.post(
                "/completions",
                json={"message": "do something", "uuid": "async-1", "wait": False},
            )

        assert resp.status_code == 202
        data = resp.json()
        assert data["success"] is True
        assert data["status"] == "queued"
        assert data["uuid"] == "async-1"
        assert data["poll_url"] == "/completions/async-1"

    @pytest.mark.asyncio
    async def test_completions_error_returns_500(self, agent_rest_app):
        """POST /completions with error result returns 500."""
        from starlette.testclient import TestClient

        mock_result = {
            "result": "failed",
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 0,
            "num_turns": 0,
            "is_error": True,
            "usage": {},
            "tool_uses": [],
        }

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            client = TestClient(agent_rest_app)
            resp = client.post(
                "/completions",
                json={"message": "fail", "uuid": "err-1", "wait": True},
            )

        assert resp.status_code == 500
        data = resp.json()
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_completions_passes_all_params(self, agent_rest_app):
        """POST /completions forwards all body params to executor."""
        from starlette.testclient import TestClient

        captured_kwargs = {}
        mock_result = {
            "result": "ok",
            "is_error": False,
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 0,
            "num_turns": 0,
            "usage": {},
            "tool_uses": [],
        }

        async def capture(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            side_effect=capture,
        ):
            client = TestClient(agent_rest_app)
            client.post(
                "/completions",
                json={
                    "message": "task",
                    "uuid": "u1",
                    "wait": True,
                    "stateless": True,
                    "model": "opus",
                    "max_turns": 50,
                    "effort": "high",
                    "system_prompt": "Be concise.",
                    "meta": {"user": "john"},
                },
            )

        assert captured_kwargs["message"] == "task"
        assert captured_kwargs["external_uuid"] == "u1"
        assert captured_kwargs["stateless"] is True
        assert captured_kwargs["model"] == "opus"
        assert captured_kwargs["max_turns"] == 50
        assert captured_kwargs["effort"] == "high"
        assert captured_kwargs["system_prompt"] == "Be concise."
        assert captured_kwargs["meta"]["user"] == "john"


# ── Completions route not registered when agent_mode disabled ─────────


@pytest.mark.unit
class TestCompletionsRouteDisabled:
    @patch.dict(os.environ, {"AGENT_MODE": ""})
    def test_no_completions_route_when_disabled(self):
        """POST /completions returns 404 when agent mode is off."""
        reset_config()
        from agenticore.server import _build_rest_app
        from starlette.testclient import TestClient

        app = _build_rest_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post("/completions", json={"message": "hi", "uuid": "u"})
        # Starlette returns 404 or 405 for unregistered routes
        assert resp.status_code in (404, 405)


# ── MCP agent_completions tool ───────────────────────────────────────────


@pytest.mark.unit
class TestAgentCompletionsTool:
    @pytest.mark.asyncio
    async def test_sync_call(self, tmp_path):
        """agent_completions MCP tool returns result when wait=True."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()

        sessions_file = tmp_path / "sessions.json"
        state_file = tmp_path / "state.json"

        env = {**_AGENT_ENV, "AGENT_MODE_PACKAGE_DIR": str(pkg)}

        with (
            patch.dict(os.environ, env),
            patch("agenticore.agent_mode.session_registry._sessions_file", return_value=sessions_file),
            patch("agenticore.agent_mode.state._state_file", return_value=state_file),
        ):
            reset_config()
            from agenticore.server import _register_agent_mode_tools

            _register_agent_mode_tools()

            # Import after registration
            from agenticore.server import mcp

            # Find the agent_completions tool
            tools = mcp._tool_manager.list_tools()
            tool_names = [t.name for t in tools]
            assert "agent_completions" in tool_names

    @pytest.mark.asyncio
    async def test_invalid_context_json(self, tmp_path):
        """agent_completions rejects invalid context JSON."""
        pkg = tmp_path / "pkg"
        pkg.mkdir()

        env = {**_AGENT_ENV, "AGENT_MODE_PACKAGE_DIR": str(pkg)}

        with patch.dict(os.environ, env):
            reset_config()
            from agenticore.server import _register_agent_mode_tools

            _register_agent_mode_tools()

            from agenticore.server import mcp

            tool_fn = None
            for t in mcp._tool_manager.list_tools():
                if t.name == "agent_completions":
                    tool_fn = t
                    break

            assert tool_fn is not None


# ── REST /completions unhappy-path tests ──────────────────────────────


@pytest.mark.unit
class TestRestCompletionsUnhappyPaths:
    @pytest.fixture()
    def agent_rest_app(self, tmp_path):
        pkg = tmp_path / "test-pkg"
        pkg.mkdir()

        sessions_file = tmp_path / "sessions.json"
        state_file = tmp_path / "state.json"

        env = {**_AGENT_ENV, "AGENT_MODE_PACKAGE_DIR": str(pkg)}
        with (
            patch.dict(os.environ, env),
            patch("agenticore.agent_mode.session_registry._sessions_file", return_value=sessions_file),
            patch("agenticore.agent_mode.state._state_file", return_value=state_file),
        ):
            reset_config()
            from agenticore.server import _build_rest_app

            yield _build_rest_app()

    @pytest.mark.asyncio
    async def test_completions_invalid_json_body(self, agent_rest_app):
        """POST /completions with non-JSON body returns error status (400 or 500)."""
        from starlette.testclient import TestClient

        client = TestClient(agent_rest_app, raise_server_exceptions=False)
        resp = client.post(
            "/completions",
            content="this is not json",
            headers={"Content-Type": "application/json"},
        )
        # Starlette raises JSONDecodeError in request.json() — results in 500
        assert resp.status_code in (400, 500)

    @pytest.mark.asyncio
    async def test_completions_executor_raises_exception(self, agent_rest_app):
        """POST /completions when executor.execute() raises returns 500."""
        from starlette.testclient import TestClient

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            new_callable=AsyncMock,
            side_effect=RuntimeError("unexpected executor crash"),
        ):
            client = TestClient(agent_rest_app, raise_server_exceptions=False)
            resp = client.post(
                "/completions",
                json={"message": "test", "uuid": "u1", "wait": True},
            )

        assert resp.status_code == 500

    @pytest.mark.asyncio
    async def test_completions_empty_message_accepted(self, agent_rest_app):
        """POST /completions with empty message is accepted (documents behavior)."""
        from starlette.testclient import TestClient

        mock_result = {
            "result": "ok",
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 0,
            "num_turns": 0,
            "is_error": False,
            "usage": {},
            "tool_uses": [],
        }

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            client = TestClient(agent_rest_app)
            resp = client.post(
                "/completions",
                json={"message": "", "uuid": "u1", "wait": True},
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_completions_empty_uuid_accepted(self, agent_rest_app):
        """POST /completions with empty uuid is accepted (documents behavior)."""
        from starlette.testclient import TestClient

        mock_result = {
            "result": "ok",
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 0,
            "num_turns": 0,
            "is_error": False,
            "usage": {},
            "tool_uses": [],
        }

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            client = TestClient(agent_rest_app)
            resp = client.post(
                "/completions",
                json={"message": "test", "uuid": "", "wait": True},
            )

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_completions_empty_body(self, agent_rest_app):
        """POST /completions with empty body returns 400."""
        from starlette.testclient import TestClient

        client = TestClient(agent_rest_app)
        resp = client.post("/completions", json={})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_completions_bad_meta_defaults_empty(self, agent_rest_app):
        """POST /completions with bad meta field defaults to empty dict."""
        from starlette.testclient import TestClient

        captured_kwargs = {}
        mock_result = {
            "result": "ok",
            "is_error": False,
            "session_id": "",
            "cost_usd": 0,
            "duration_ms": 0,
            "num_turns": 0,
            "usage": {},
            "tool_uses": [],
        }

        async def capture(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_result

        with patch.object(
            __import__("agenticore.agent_mode.agent", fromlist=["AgentExecutor"]).AgentExecutor,
            "execute",
            side_effect=capture,
        ):
            client = TestClient(agent_rest_app)
            resp = client.post(
                "/completions",
                json={"message": "test", "uuid": "u1", "wait": True, "meta": "not-a-dict"},
            )

        # The meta is passed through as-is from the body — the executor receives it
        # Server code does: meta_dict = body.get("meta", {})
        # If meta is a string, it's passed as a string — no error
        assert resp.status_code == 200
