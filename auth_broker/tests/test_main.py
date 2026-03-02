"""Unit tests for auth_broker.main FastAPI app."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from auth_broker.main import app
from auth_broker.models import AuthStatus

API_KEY = "test-secret-key"


@pytest.fixture(autouse=True)
def mock_env(monkeypatch):
    monkeypatch.setenv("AUTH_BROKER_API_KEY", API_KEY)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/9")
    monkeypatch.setenv("OPENBAO_ADDR", "http://localhost:8200")
    monkeypatch.setenv("OPENBAO_TOKEN", "test-token")
    monkeypatch.setenv("CALLBACK_BASE_URL", "http://localhost:8300")
    monkeypatch.setenv("PROVIDERS_PATH", "/nonexistent/providers.yaml")


@pytest.fixture
def client():
    with (
        patch("auth_broker.store.connect", new_callable=AsyncMock),
        patch("auth_broker.store.close", new_callable=AsyncMock),
        patch("auth_broker.vault.connect", new_callable=AsyncMock),
        patch("auth_broker.vault.close", new_callable=AsyncMock),
        patch("auth_broker.providers.load_providers"),
        patch("auth_broker.store.append_event", new_callable=AsyncMock),
    ):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


def auth_headers():
    return {"Authorization": f"Bearer {API_KEY}"}


class TestHealth:
    def test_health_returns_200(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestCreateAuthRequest:
    def test_unknown_service_returns_404(self, client):
        with patch("auth_broker.providers.get_provider", return_value=None):
            resp = client.post("/auth/request", json={"service": "unknown"}, headers=auth_headers())
        assert resp.status_code == 404

    def test_cached_token_returns_immediately(self, client):
        cached = {"token": "tok123", "expires_at": 0}
        with (
            patch("auth_broker.providers.get_provider", return_value={"auth_type": "manual"}),
            patch("auth_broker.vault.get_token", new_callable=AsyncMock, return_value=cached),
            patch("auth_broker.vault.needs_refresh", return_value=False),
            patch("auth_broker.vault.is_valid", return_value=True),
        ):
            resp = client.post(
                "/auth/request",
                json={"service": "anthropic", "consumer_id": "agent"},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is True
        assert body["status"] == AuthStatus.completed.value
        assert body["request_id"] is None

    def test_oauth_provider_creates_request(self, client):
        provider = {
            "auth_type": "oauth2",
            "authorize_url": "https://github.com/login/oauth/authorize",
            "token_url": "https://github.com/login/oauth/access_token",
            "client_id_env": "GITHUB_CLIENT_ID",
            "scopes": ["repo"],
        }
        with (
            patch("auth_broker.providers.get_provider", return_value=provider),
            patch("auth_broker.vault.get_token", new_callable=AsyncMock, return_value=None),
            patch("auth_broker.store.create_request", new_callable=AsyncMock, return_value="req-001"),
            patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
                "id": "req-001", "service": "github", "consumer_id": "default",
                "oauth_state": "state-xyz",
            }),
            patch("auth_broker.oauth.build_auth_url", new_callable=AsyncMock,
                  return_value="https://github.com/login/oauth/authorize?client_id=x"),
            patch("auth_broker.store.store_oauth_state", new_callable=AsyncMock),
            patch("auth_broker.store.update_status", new_callable=AsyncMock),
            patch("auth_broker.ws.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            resp = client.post("/auth/request", json={"service": "github"}, headers=auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["request_id"] == "req-001"

    def test_manual_provider_no_auth_url(self, client):
        provider = {"auth_type": "manual", "display_name": "Anthropic"}
        with (
            patch("auth_broker.providers.get_provider", return_value=provider),
            patch("auth_broker.vault.get_token", new_callable=AsyncMock, return_value=None),
            patch("auth_broker.store.create_request", new_callable=AsyncMock, return_value="req-002"),
            patch("auth_broker.ws.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            resp = client.post("/auth/request", json={"service": "anthropic"}, headers=auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["auth_url"] is None

    def test_missing_auth_returns_401(self, client):
        resp = client.post("/auth/request", json={"service": "github"})
        assert resp.status_code in (401, 403)


class TestGetTokenDirect:
    def test_found_returns_200(self, client):
        token = {"token": "secret", "expires_at": 0}
        with (
            patch("auth_broker.vault.get_token", new_callable=AsyncMock, return_value=token),
            patch("auth_broker.vault.needs_refresh", return_value=False),
            patch("auth_broker.vault.is_valid", return_value=True),
        ):
            resp = client.get(
                "/auth/token/direct",
                params={"service": "anthropic", "consumer_id": "default"},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"] == token
        assert body["valid"] is True

    def test_not_found_returns_404(self, client):
        with patch("auth_broker.vault.get_token", new_callable=AsyncMock, return_value=None):
            resp = client.get(
                "/auth/token/direct",
                params={"service": "anthropic"},
                headers=auth_headers(),
            )
        assert resp.status_code == 404

    def test_no_auth_returns_401(self, client):
        resp = client.get("/auth/token/direct", params={"service": "anthropic"})
        assert resp.status_code in (401, 403)


class TestGetTokenById:
    def test_pending_returns_null_token(self, client):
        with patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
            "id": "req-1", "status": AuthStatus.pending.value,
            "service": "github", "consumer_id": "default",
        }):
            resp = client.get("/auth/token/req-1", headers=auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["token"] is None
        assert body["status"] == AuthStatus.pending.value

    def test_completed_returns_token(self, client):
        token_data = {"token": "gh-token", "expires_at": 0}
        with (
            patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
                "id": "req-2", "status": AuthStatus.completed.value,
                "service": "github", "consumer_id": "default",
            }),
            patch("auth_broker.vault.get_token", new_callable=AsyncMock, return_value=token_data),
        ):
            resp = client.get("/auth/token/req-2", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json()["token"] == token_data

    def test_unknown_id_returns_404(self, client):
        with patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value=None):
            resp = client.get("/auth/token/no-such-id", headers=auth_headers())
        assert resp.status_code == 404


class TestDenyRequest:
    def test_sets_status_denied(self, client):
        with (
            patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
                "id": "req-3", "service": "github",
            }),
            patch("auth_broker.store.update_status", new_callable=AsyncMock) as mock_update,
            patch("auth_broker.ws.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            resp = client.delete("/auth/request/req-3", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json()["status"] == "denied"
        mock_update.assert_called_once()


class TestManualToken:
    def test_stores_token_and_completes(self, client):
        with (
            patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
                "id": "req-4", "service": "anthropic", "consumer_id": "default",
            }),
            patch("auth_broker.vault.store_token", new_callable=AsyncMock) as mock_store,
            patch("auth_broker.store.update_status", new_callable=AsyncMock),
            patch("auth_broker.ws.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            resp = client.post(
                "/auth/manual/req-4",
                json={"token": "sk-ant-123", "expires_at": 0, "scope": ""},
                headers=auth_headers(),
            )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"
        mock_store.assert_called_once()


class TestOAuthCallback:
    def test_valid_state_returns_html(self, client):
        with (
            patch("auth_broker.store.get_request_by_state", new_callable=AsyncMock, return_value="req-5"),
            patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
                "id": "req-5", "service": "github", "consumer_id": "default",
            }),
            patch("auth_broker.providers.get_provider", return_value={
                "token_url": "https://github.com/login/oauth/access_token",
                "client_id_env": "X", "client_secret_env": "Y",
            }),
            patch("auth_broker.store.update_status", new_callable=AsyncMock),
            patch("auth_broker.oauth.exchange_code", new_callable=AsyncMock,
                  return_value={"access_token": "tok", "expires_in": 3600}),
            patch("auth_broker.vault.store_token", new_callable=AsyncMock),
            patch("auth_broker.ws.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            resp = client.get("/auth/callback?code=mycode&state=validstate")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_invalid_state_returns_400(self, client):
        with patch("auth_broker.store.get_request_by_state", new_callable=AsyncMock, return_value=None):
            resp = client.get("/auth/callback?code=x&state=badstate")
        assert resp.status_code == 400

    def test_exchange_failure_returns_500(self, client):
        with (
            patch("auth_broker.store.get_request_by_state", new_callable=AsyncMock, return_value="req-6"),
            patch("auth_broker.store.get_request", new_callable=AsyncMock, return_value={
                "id": "req-6", "service": "github", "consumer_id": "default",
            }),
            patch("auth_broker.providers.get_provider", return_value={
                "token_url": "https://github.com/login/oauth/access_token",
                "client_id_env": "X", "client_secret_env": "Y",
            }),
            patch("auth_broker.store.update_status", new_callable=AsyncMock),
            patch("auth_broker.oauth.exchange_code", new_callable=AsyncMock,
                  side_effect=Exception("Token exchange failed")),
            patch("auth_broker.ws.ws_manager.broadcast", new_callable=AsyncMock),
        ):
            resp = client.get("/auth/callback?code=bad&state=validstate")
        assert resp.status_code == 500


class TestGetPending:
    def test_empty_pending(self, client):
        with patch("auth_broker.store.get_pending", new_callable=AsyncMock, return_value=[]):
            resp = client.get("/auth/pending", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_two_pending_items(self, client):
        items = [{"id": "a", "service": "github"}, {"id": "b", "service": "anthropic"}]
        with patch("auth_broker.store.get_pending", new_callable=AsyncMock, return_value=items):
            resp = client.get("/auth/pending", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json()["count"] == 2


class TestWebSocket:
    def test_valid_token_connects(self, client):
        with client.websocket_connect(f"/auth/ws?token={API_KEY}") as ws:
            # Connection accepted — no close code
            ws.send_text("ping")

    def test_invalid_token_closes_4001(self, client):
        with pytest.raises(Exception):
            with client.websocket_connect("/auth/ws?token=wrong-key") as ws:
                ws.receive_text()


class TestListEvents:
    def test_returns_events_list(self, client):
        events = [
            {"ts": 1700000001, "event": "new_request", "data": {"service": "github"}},
            {"ts": 1700000000, "event": "token_ready", "data": {"service": "github"}},
        ]
        with patch("auth_broker.store.get_events", new_callable=AsyncMock, return_value=events):
            resp = client.get("/auth/events", headers=auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        assert body["events"][0]["event"] == "new_request"

    def test_empty_events(self, client):
        with patch("auth_broker.store.get_events", new_callable=AsyncMock, return_value=[]):
            resp = client.get("/auth/events", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "events": []}

    def test_limit_param_forwarded(self, client):
        with patch("auth_broker.store.get_events", new_callable=AsyncMock, return_value=[]) as mock_get:
            client.get("/auth/events?limit=10", headers=auth_headers())
        mock_get.assert_called_once_with(limit=10)

    def test_no_auth_returns_401(self, client):
        resp = client.get("/auth/events")
        assert resp.status_code in (401, 403)


class TestListHistory:
    def test_returns_history_list(self, client):
        reqs = [
            {"id": "req-1", "service": "anthropic", "status": "completed", "consumer_id": "default", "created_at": "1700000000"},
        ]
        with patch("auth_broker.store.get_history", new_callable=AsyncMock, return_value=reqs):
            resp = client.get("/auth/history", headers=auth_headers())
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["requests"][0]["service"] == "anthropic"

    def test_empty_history(self, client):
        with patch("auth_broker.store.get_history", new_callable=AsyncMock, return_value=[]):
            resp = client.get("/auth/history", headers=auth_headers())
        assert resp.status_code == 200
        assert resp.json() == {"count": 0, "requests": []}

    def test_limit_param_forwarded(self, client):
        with patch("auth_broker.store.get_history", new_callable=AsyncMock, return_value=[]) as mock_get:
            client.get("/auth/history?limit=25", headers=auth_headers())
        mock_get.assert_called_once_with(limit=25)

    def test_no_auth_returns_401(self, client):
        resp = client.get("/auth/history")
        assert resp.status_code in (401, 403)
