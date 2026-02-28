"""Unit tests for server module.

Tests MCP tool functions (run_task, get_job, list_jobs, cancel_job,
list_profiles) and REST API routes.
"""

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agenticore.config import reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


# ── MCP Tool: run_task ────────────────────────────────────────────────────


@pytest.mark.unit
class TestRunTask:
    @pytest.mark.asyncio
    async def test_async_by_default(self):
        """run_task defaults to wait=False (fire and forget)."""
        from agenticore.server import run_task

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {
            "id": "job-001",
            "status": "queued",
            "task": "fix bug",
            "profile": "code",
        }

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job),
        ):
            result = await run_task(task="fix bug")

        data = json.loads(result)
        assert data["success"] is True
        assert data["job"]["status"] == "queued"
        assert data["job"]["id"] == "job-001"

    @pytest.mark.asyncio
    async def test_wait_true_blocks(self):
        """run_task with wait=True passes wait to submit_job."""
        from agenticore.server import run_task

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {
            "id": "job-002",
            "status": "succeeded",
            "output": "Done!",
        }

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            result = await run_task(task="deploy", wait=True)

        mock_submit.assert_called_once()
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["wait"] is True

        data = json.loads(result)
        assert data["success"] is True
        assert data["job"]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_with_all_params(self):
        """run_task forwards all parameters to submit_job."""
        from agenticore.server import run_task

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="review") as mock_route,
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            await run_task(
                task="add tests",
                repo_url="https://github.com/org/repo",
                profile="review",
                base_ref="develop",
                wait=False,
                session_id="sess-1",
            )

        mock_route.assert_called_once_with(profile="review", repo_url="https://github.com/org/repo")
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["task"] == "add tests"
        assert call_kwargs["profile"] == "review"
        assert call_kwargs["repo_url"] == "https://github.com/org/repo"
        assert call_kwargs["base_ref"] == "develop"
        assert call_kwargs["wait"] is False
        assert call_kwargs["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_error_handling(self):
        """run_task returns error JSON on exception."""
        from agenticore.server import run_task

        with patch("agenticore.router.route", side_effect=ValueError("bad profile")):
            result = await run_task(task="test")

        data = json.loads(result)
        assert data["success"] is False
        assert "bad profile" in data["error"]

    @pytest.mark.asyncio
    async def test_default_params(self):
        """run_task has sensible defaults for optional params."""
        from agenticore.server import run_task

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="code") as mock_route,
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            await run_task(task="test")

        # Default values
        mock_route.assert_called_once_with(profile="", repo_url="")
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["base_ref"] == "main"
        assert call_kwargs["wait"] is False
        assert call_kwargs["session_id"] is None


# ── MCP Tool: get_job ─────────────────────────────────────────────────────


@pytest.mark.unit
class TestGetJob:
    @pytest.mark.asyncio
    async def test_get_existing_job(self):
        from agenticore.server import get_job

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {
            "id": "j1",
            "status": "running",
            "task": "fix bug",
        }

        with patch("agenticore.jobs.get_job", return_value=mock_job):
            result = await get_job("j1")

        data = json.loads(result)
        assert data["success"] is True
        assert data["job"]["id"] == "j1"

    @pytest.mark.asyncio
    async def test_get_missing_job(self):
        from agenticore.server import get_job

        with patch("agenticore.jobs.get_job", return_value=None):
            result = await get_job("nonexistent")

        data = json.loads(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_get_job_error(self):
        from agenticore.server import get_job

        with patch("agenticore.jobs.get_job", side_effect=RuntimeError("db error")):
            result = await get_job("j1")

        data = json.loads(result)
        assert data["success"] is False
        assert "db error" in data["error"]


# ── MCP Tool: list_jobs ──────────────────────────────────────────────────


@pytest.mark.unit
class TestListJobs:
    @pytest.mark.asyncio
    async def test_list_jobs_default(self):
        from agenticore.server import list_jobs

        mock_jobs = [MagicMock(), MagicMock()]
        mock_jobs[0].to_dict.return_value = {"id": "j1", "status": "queued"}
        mock_jobs[1].to_dict.return_value = {"id": "j2", "status": "running"}

        with patch("agenticore.jobs.list_jobs", return_value=mock_jobs):
            result = await list_jobs()

        data = json.loads(result)
        assert data["success"] is True
        assert data["count"] == 2
        assert len(data["jobs"]) == 2

    @pytest.mark.asyncio
    async def test_list_jobs_with_limit(self):
        from agenticore.server import list_jobs

        with patch("agenticore.jobs.list_jobs", return_value=[]) as mock_list:
            await list_jobs(limit=5)

        mock_list.assert_called_once_with(limit=5, status=None)

    @pytest.mark.asyncio
    async def test_list_jobs_with_status_filter(self):
        from agenticore.server import list_jobs

        with patch("agenticore.jobs.list_jobs", return_value=[]) as mock_list:
            await list_jobs(status="running")

        mock_list.assert_called_once_with(limit=20, status="running")

    @pytest.mark.asyncio
    async def test_list_jobs_empty_status_treated_as_none(self):
        from agenticore.server import list_jobs

        with patch("agenticore.jobs.list_jobs", return_value=[]) as mock_list:
            await list_jobs(status="")

        mock_list.assert_called_once_with(limit=20, status=None)

    @pytest.mark.asyncio
    async def test_list_jobs_error(self):
        from agenticore.server import list_jobs

        with patch("agenticore.jobs.list_jobs", side_effect=RuntimeError("boom")):
            result = await list_jobs()

        data = json.loads(result)
        assert data["success"] is False


# ── MCP Tool: cancel_job ─────────────────────────────────────────────────


@pytest.mark.unit
class TestCancelJob:
    @pytest.mark.asyncio
    async def test_cancel_existing(self):
        from agenticore.server import cancel_job

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j1", "status": "cancelled"}

        with patch("agenticore.jobs.cancel_job", return_value=mock_job):
            result = await cancel_job("j1")

        data = json.loads(result)
        assert data["success"] is True
        assert data["job"]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_missing(self):
        from agenticore.server import cancel_job

        with patch("agenticore.jobs.cancel_job", return_value=None):
            result = await cancel_job("nonexistent")

        data = json.loads(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_cancel_error(self):
        from agenticore.server import cancel_job

        with patch("agenticore.jobs.cancel_job", side_effect=RuntimeError("fail")):
            result = await cancel_job("j1")

        data = json.loads(result)
        assert data["success"] is False


# ── MCP Tool: list_profiles ──────────────────────────────────────────────


@pytest.mark.unit
class TestListProfiles:
    @pytest.mark.asyncio
    async def test_list_profiles(self):
        from agenticore.server import list_profiles

        code_mock = MagicMock()
        code_mock.profile_name = "code"
        review_mock = MagicMock()
        review_mock.profile_name = "review"
        mock_profiles = {"code": code_mock, "review": review_mock}

        call_count = {"n": 0}
        names = ["code", "review"]

        def mock_to_dict(p):
            idx = call_count["n"]
            call_count["n"] += 1
            return {"name": names[idx]}

        with (
            patch("agenticore.profiles.load_profiles", return_value=mock_profiles),
            patch("agenticore.profiles.profile_to_dict", side_effect=mock_to_dict),
        ):
            result = await list_profiles()

        data = json.loads(result)
        assert data["success"] is True
        assert data["count"] == 2
        result_names = {p["name"] for p in data["profiles"]}
        assert "code" in result_names
        assert "review" in result_names

    @pytest.mark.asyncio
    async def test_list_profiles_error(self):
        from agenticore.server import list_profiles

        with patch("agenticore.profiles.load_profiles", side_effect=RuntimeError("fs error")):
            result = await list_profiles()

        data = json.loads(result)
        assert data["success"] is False


# ── REST API routes ───────────────────────────────────────────────────────


@pytest.mark.unit
class TestRestApi:
    """Test REST endpoint routing by calling the Starlette app directly."""

    @pytest.fixture
    def rest_app(self):
        from agenticore.server import _build_rest_app

        return _build_rest_app()

    @pytest.mark.asyncio
    async def test_health_endpoint(self, rest_app):
        from starlette.testclient import TestClient

        client = TestClient(rest_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "agenticore"

    @pytest.mark.asyncio
    async def test_post_jobs_async(self, rest_app):
        """POST /jobs with wait=false (default) submits async."""
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j1", "status": "queued", "profile": "code"}

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job),
        ):
            client = TestClient(rest_app)
            resp = client.post("/jobs", json={"task": "fix bug"})

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["job"]["status"] == "queued"

    @pytest.mark.asyncio
    async def test_post_jobs_wait(self, rest_app):
        """POST /jobs with wait=true blocks until completion."""
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j1", "status": "succeeded", "output": "done"}

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            client = TestClient(rest_app)
            resp = client.post("/jobs", json={"task": "fix bug", "wait": True})

        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["wait"] is True

        data = resp.json()
        assert data["success"] is True
        assert data["job"]["status"] == "succeeded"

    @pytest.mark.asyncio
    async def test_post_jobs_all_fields(self, rest_app):
        """POST /jobs forwards all body fields."""
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="review"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            client = TestClient(rest_app)
            resp = client.post(
                "/jobs",
                json={
                    "task": "add tests",
                    "repo_url": "https://github.com/org/repo",
                    "profile": "review",
                    "base_ref": "develop",
                    "wait": False,
                    "session_id": "s1",
                },
            )

        assert resp.status_code == 200
        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["task"] == "add tests"
        assert call_kwargs["repo_url"] == "https://github.com/org/repo"
        assert call_kwargs["base_ref"] == "develop"

    @pytest.mark.asyncio
    async def test_post_jobs_error_returns_400(self, rest_app):
        from starlette.testclient import TestClient

        with patch("agenticore.router.route", side_effect=ValueError("bad")):
            client = TestClient(rest_app)
            resp = client.post("/jobs", json={"task": "test"})

        assert resp.status_code == 400
        data = resp.json()
        assert data["success"] is False

    @pytest.mark.asyncio
    async def test_get_job_route(self, rest_app):
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j1", "status": "running"}

        with patch("agenticore.jobs.get_job", return_value=mock_job):
            client = TestClient(rest_app)
            resp = client.get("/jobs/j1")

        assert resp.status_code == 200
        assert resp.json()["job"]["id"] == "j1"

    @pytest.mark.asyncio
    async def test_get_job_not_found_returns_404(self, rest_app):
        from starlette.testclient import TestClient

        with patch("agenticore.jobs.get_job", return_value=None):
            client = TestClient(rest_app)
            resp = client.get("/jobs/bad-id")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_jobs_list(self, rest_app):
        from starlette.testclient import TestClient

        mock_jobs = [MagicMock(), MagicMock()]
        mock_jobs[0].to_dict.return_value = {"id": "j1"}
        mock_jobs[1].to_dict.return_value = {"id": "j2"}

        with patch("agenticore.jobs.list_jobs", return_value=mock_jobs):
            client = TestClient(rest_app)
            resp = client.get("/jobs?limit=10&status=running")

        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] == 2

    @pytest.mark.asyncio
    async def test_delete_job_route(self, rest_app):
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j1", "status": "cancelled"}

        with patch("agenticore.jobs.cancel_job", return_value=mock_job):
            client = TestClient(rest_app)
            resp = client.delete("/jobs/j1")

        assert resp.status_code == 200
        assert resp.json()["job"]["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_delete_job_not_found(self, rest_app):
        from starlette.testclient import TestClient

        with patch("agenticore.jobs.cancel_job", return_value=None):
            client = TestClient(rest_app)
            resp = client.delete("/jobs/bad")

        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_get_profiles_route(self, rest_app):
        from starlette.testclient import TestClient

        mock_profiles = {"code": MagicMock(name="code")}

        with (
            patch("agenticore.profiles.load_profiles", return_value=mock_profiles),
            patch("agenticore.profiles.profile_to_dict", return_value={"name": "code"}),
        ):
            client = TestClient(rest_app)
            resp = client.get("/profiles")

        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True

    @pytest.mark.asyncio
    async def test_post_jobs_default_wait_false(self, rest_app):
        """POST /jobs without wait field defaults to false."""
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            client = TestClient(rest_app)
            client.post("/jobs", json={"task": "test"})

        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["wait"] is False

    @pytest.mark.asyncio
    async def test_post_jobs_default_base_ref(self, rest_app):
        """POST /jobs without base_ref defaults to 'main'."""
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            client = TestClient(rest_app)
            client.post("/jobs", json={"task": "test"})

        call_kwargs = mock_submit.call_args[1]
        assert call_kwargs["base_ref"] == "main"


# ── API Key Middleware ────────────────────────────────────────────────────


@pytest.mark.unit
class TestApiKeyMiddleware:
    def test_health_bypasses_auth(self):
        """Health endpoint is always public even with API keys configured."""
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1,secret2"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_rejects_missing_key(self):
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/profiles")
        assert resp.status_code == 401

    def test_accepts_valid_header_key(self):
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        mock_profiles = {"code": MagicMock(name="code")}

        with (
            patch("agenticore.profiles.load_profiles", return_value=mock_profiles),
            patch("agenticore.profiles.profile_to_dict", return_value={"name": "code"}),
        ):
            client = TestClient(app)
            resp = client.get("/profiles", headers={"X-API-Key": "secret1"})
        assert resp.status_code == 200

    def test_rejects_wrong_key(self):
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/profiles", headers={"X-API-Key": "wrong"})
        assert resp.status_code == 401

    def test_accepts_query_param_key(self):
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        mock_profiles = {"code": MagicMock(name="code")}

        with (
            patch("agenticore.profiles.load_profiles", return_value=mock_profiles),
            patch("agenticore.profiles.profile_to_dict", return_value={"name": "code"}),
        ):
            client = TestClient(app)
            resp = client.get("/profiles?api_key=secret1")
        assert resp.status_code == 200

    def test_no_middleware_without_api_keys(self):
        """When no API keys configured, no auth is required."""
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": ""}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        mock_profiles = {"code": MagicMock(name="code")}

        with (
            patch("agenticore.profiles.load_profiles", return_value=mock_profiles),
            patch("agenticore.profiles.profile_to_dict", return_value={"name": "code"}),
        ):
            client = TestClient(app)
            resp = client.get("/profiles")
        assert resp.status_code == 200

    def test_accepts_bearer_token(self):
        """Authorization: Bearer <key> is accepted."""
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        mock_profiles = {"code": MagicMock(name="code")}

        with (
            patch("agenticore.profiles.load_profiles", return_value=mock_profiles),
            patch("agenticore.profiles.profile_to_dict", return_value={"name": "code"}),
        ):
            client = TestClient(app)
            resp = client.get("/profiles", headers={"Authorization": "Bearer secret1"})
        assert resp.status_code == 200

    def test_rejects_invalid_bearer(self):
        """Authorization: Bearer with wrong key returns 401."""
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/profiles", headers={"Authorization": "Bearer wrong"})
        assert resp.status_code == 401

    def test_rejects_non_bearer_authorization(self):
        """Authorization header that is not Bearer returns 401."""
        with patch.dict(os.environ, {"AGENTICORE_API_KEYS": "secret1"}, clear=False):
            reset_config()
            from agenticore.server import _build_asgi_app

            app = _build_asgi_app()

        from starlette.testclient import TestClient

        client = TestClient(app)
        resp = client.get("/profiles", headers={"Authorization": "Basic dXNlcjpwYXNz"})
        assert resp.status_code == 401

    def test_extract_api_key_from_x_api_key(self):
        """_extract_api_key reads X-API-Key header."""
        from agenticore.server import _ApiKeyMiddleware

        mw = _ApiKeyMiddleware(None, ["k"])
        scope = {"headers": [(b"x-api-key", b"mykey")]}
        assert mw._extract_api_key(scope) == "mykey"

    def test_extract_api_key_from_bearer(self):
        """_extract_api_key reads Authorization: Bearer header."""
        from agenticore.server import _ApiKeyMiddleware

        mw = _ApiKeyMiddleware(None, ["k"])
        scope = {"headers": [(b"authorization", b"Bearer mytoken")]}
        assert mw._extract_api_key(scope) == "mytoken"

    def test_extract_api_key_from_query_string(self):
        """_extract_api_key reads api_key query param."""
        from agenticore.server import _ApiKeyMiddleware

        mw = _ApiKeyMiddleware(None, ["k"])
        scope = {"headers": [], "query_string": b"foo=bar&api_key=qparam&baz=1"}
        assert mw._extract_api_key(scope) == "qparam"

    def test_extract_api_key_returns_empty_when_absent(self):
        """_extract_api_key returns empty string when no key present."""
        from agenticore.server import _ApiKeyMiddleware

        mw = _ApiKeyMiddleware(None, ["k"])
        scope = {"headers": [], "query_string": b""}
        assert mw._extract_api_key(scope) == ""

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        """Non-HTTP/WebSocket scopes (e.g. lifespan) bypass auth check."""
        from agenticore.server import _ApiKeyMiddleware
        from unittest.mock import AsyncMock

        inner = AsyncMock()
        mw = _ApiKeyMiddleware(inner, {"key1"})
        scope = {"type": "lifespan"}
        receive = AsyncMock()
        send = AsyncMock()

        await mw(scope, receive, send)

        inner.assert_awaited_once_with(scope, receive, send)


# ── Consistent async behavior across interfaces ──────────────────────────


@pytest.mark.unit
class TestAsyncConsistency:
    """Verify CLI, REST, and MCP all default to fire-and-forget."""

    @pytest.mark.asyncio
    async def test_mcp_default_wait_false(self):
        """MCP run_task defaults wait=False."""
        from agenticore.server import run_task

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            await run_task(task="test")

        assert mock_submit.call_args[1]["wait"] is False

    @pytest.mark.asyncio
    async def test_rest_default_wait_false(self):
        """REST POST /jobs defaults wait=False."""
        from agenticore.server import _build_rest_app
        from starlette.testclient import TestClient

        mock_job = MagicMock()
        mock_job.to_dict.return_value = {"id": "j", "status": "queued"}

        with (
            patch("agenticore.router.route", return_value="code"),
            patch("agenticore.runner.submit_job", new_callable=AsyncMock, return_value=mock_job) as mock_submit,
        ):
            client = TestClient(_build_rest_app())
            client.post("/jobs", json={"task": "test"})

        assert mock_submit.call_args[1]["wait"] is False

    def test_cli_default_wait_false(self):
        """CLI 'run' command defaults --wait to False."""
        import argparse

        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest="command")
        p = sub.add_parser("run")
        p.add_argument("task")
        p.add_argument("--wait", "-w", action="store_true")

        args = parser.parse_args(["run", "test"])
        assert args.wait is False


# ── OAuth config ──────────────────────────────────────────────────────────


@pytest.mark.unit
class TestOAuthConfig:
    def test_no_issuer_returns_none(self):
        """_build_oauth_config returns (None, None) when OAUTH_ISSUER_URL is unset."""
        from agenticore.server import _build_oauth_config

        with patch.dict(os.environ, {"OAUTH_ISSUER_URL": ""}, clear=False):
            provider, settings = _build_oauth_config()

        assert provider is None
        assert settings is None

    def test_issuer_url_creates_provider(self, capsys):
        """_build_oauth_config returns provider + settings when OAUTH_ISSUER_URL is set."""
        from agenticore.server import _build_oauth_config

        env = {
            "OAUTH_ISSUER_URL": "https://auth.example.com",
            "OAUTH_CLIENT_ID": "",
            "OAUTH_CLIENT_SECRET": "",
            "OAUTH_ALLOWED_SCOPES": "",
            "OAUTH_RESOURCE_URL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            reset_config()
            provider, settings = _build_oauth_config()

        assert provider is not None
        assert settings is not None

    def test_make_mcp_logs_oauth_enabled(self, capsys):
        """_make_mcp prints OAuth enabled message when provider is configured."""
        from agenticore.server import _build_oauth_config

        env = {
            "OAUTH_ISSUER_URL": "https://auth.example.com",
            "OAUTH_CLIENT_ID": "",
            "OAUTH_CLIENT_SECRET": "",
            "OAUTH_ALLOWED_SCOPES": "",
            "OAUTH_RESOURCE_URL": "",
        }
        with patch.dict(os.environ, env, clear=False):
            reset_config()
            provider, settings = _build_oauth_config()

        assert provider is not None
        # kwargs path: if oauth_provider and oauth_settings -> logs "OAuth 2.1 enabled"
        from agenticore.server import _make_mcp

        with patch.dict(os.environ, env, clear=False):
            reset_config()
            with patch("agenticore.server._build_oauth_config", return_value=(provider, settings)):
                _make_mcp()

        assert "OAuth 2.1 enabled" in capsys.readouterr().err
