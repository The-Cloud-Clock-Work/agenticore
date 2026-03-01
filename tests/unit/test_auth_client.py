"""Unit tests for agenticore.auth_client."""

import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from agenticore.auth_client import AuthClient
from agenticore.config import reset_config


@pytest.fixture(autouse=True)
def _reset():
    reset_config()
    yield
    reset_config()


@pytest.mark.unit
class TestEnabled:
    def test_enabled_when_both_set(self):
        client = AuthClient(url="http://broker", api_key="key123")
        assert client.enabled is True

    def test_disabled_when_no_url(self):
        client = AuthClient(url="", api_key="key123")
        assert client.enabled is False

    def test_disabled_when_no_key(self):
        client = AuthClient(url="http://broker", api_key="")
        assert client.enabled is False

    @patch.dict(os.environ, {"AUTH_BROKER_URL": "http://env-broker", "AUTH_BROKER_API_KEY": "env-key"})
    def test_reads_from_env(self):
        client = AuthClient()
        assert client.enabled is True
        assert client._url == "http://env-broker"
        assert client._api_key == "env-key"

    @patch.dict(os.environ, {"AUTH_BROKER_URL": "", "AUTH_BROKER_API_KEY": ""})
    def test_disabled_from_env_when_unset(self):
        client = AuthClient()
        assert client.enabled is False


@pytest.mark.unit
class TestGetToken:
    def test_disabled_returns_none(self):
        client = AuthClient(url="", api_key="")
        result = client.get_token("anthropic")
        assert result is None

    def test_direct_hit_returns_token(self):
        token = {"token": "sk-ant-123", "expires_at": 0}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json = MagicMock(return_value={"token": token, "valid": True})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=mock_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            client = AuthClient(url="http://broker", api_key="key")
            result = client.get_token("anthropic")

        assert result == token

    def test_direct_miss_falls_to_request_and_poll(self):
        # Fast path: 404
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        # Slow path: request created
        request_resp = MagicMock()
        request_resp.status_code = 200
        request_resp.json = MagicMock(return_value={"request_id": "req-abc", "status": "pending"})

        # Poll: completed
        poll_resp = MagicMock()
        poll_resp.status_code = 200
        poll_resp.json = MagicMock(return_value={"status": "completed", "token": {"token": "tok", "expires_at": 0}})

        def mock_get(url, **kwargs):
            if "token/direct" in url:
                return miss_resp
            return poll_resp

        def mock_post(url, **kwargs):
            return request_resp

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=mock_get)
        mock_client.post = MagicMock(return_value=request_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            with patch("agenticore.auth_client.time.sleep"):
                client = AuthClient(url="http://broker", api_key="key", poll_interval=0)
                result = client.get_token("anthropic", timeout=30)

        # Result comes from poll (token dict or None based on get_token returning data["token"])
        assert result is not None

    def test_cached_immediate_return(self):
        """POST /auth/request returns cached=True — no polling needed."""
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        cached_token = {"token": "cached-tok", "expires_at": 0}
        cached_resp = MagicMock()
        cached_resp.status_code = 200
        cached_resp.json = MagicMock(return_value={"cached": True, "status": "completed", "token": cached_token})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=miss_resp)
        mock_client.post = MagicMock(return_value=cached_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            client = AuthClient(url="http://broker", api_key="key")
            result = client.get_token("anthropic")

        assert result == cached_token

    def test_denied_returns_none(self):
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        req_resp = MagicMock()
        req_resp.status_code = 200
        req_resp.json = MagicMock(return_value={"request_id": "req-1", "status": "pending"})

        denied_resp = MagicMock()
        denied_resp.status_code = 200
        denied_resp.json = MagicMock(return_value={"status": "denied"})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        def mock_get(url, **kwargs):
            if "token/direct" in url:
                return miss_resp
            return denied_resp

        mock_client.get = MagicMock(side_effect=mock_get)
        mock_client.post = MagicMock(return_value=req_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            with patch("agenticore.auth_client.time.sleep"):
                client = AuthClient(url="http://broker", api_key="key", poll_interval=0)
                result = client.get_token("anthropic", timeout=30)

        assert result is None

    def test_timeout_returns_none(self):
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        req_resp = MagicMock()
        req_resp.status_code = 200
        req_resp.json = MagicMock(return_value={"request_id": "req-1", "status": "pending"})

        pending_resp = MagicMock()
        pending_resp.status_code = 200
        pending_resp.json = MagicMock(return_value={"status": "pending"})

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)

        def mock_get(url, **kwargs):
            if "token/direct" in url:
                return miss_resp
            return pending_resp

        mock_client.get = MagicMock(side_effect=mock_get)
        mock_client.post = MagicMock(return_value=req_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            with patch("agenticore.auth_client.time.sleep"):
                with patch("agenticore.auth_client.time.monotonic", side_effect=[0, 0, 999]):
                    client = AuthClient(url="http://broker", api_key="key", poll_interval=0)
                    result = client.get_token("anthropic", timeout=1)

        assert result is None

    def test_connect_error_returns_none(self):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=httpx.ConnectError("refused"))

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            client = AuthClient(url="http://broker", api_key="key")
            result = client.get_token("anthropic")

        assert result is None

    def test_slow_path_post_failure_returns_none(self):
        """POST /auth/request returning non-200 → None."""
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        fail_resp = MagicMock()
        fail_resp.status_code = 503

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=miss_resp)
        mock_client.post = MagicMock(return_value=fail_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            client = AuthClient(url="http://broker", api_key="key")
            result = client.get_token("anthropic")

        assert result is None

    def test_slow_path_connect_error_returns_none(self):
        """ConnectError on POST /auth/request → None."""
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=miss_resp)
        mock_client.post = MagicMock(side_effect=httpx.ConnectError("refused"))

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            client = AuthClient(url="http://broker", api_key="key")
            result = client.get_token("anthropic")

        assert result is None

    def test_no_request_id_returns_none(self):
        """POST /auth/request returns 200 but no request_id → None."""
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        bad_resp = MagicMock()
        bad_resp.status_code = 200
        bad_resp.json = MagicMock(return_value={"status": "pending"})  # no request_id

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(return_value=miss_resp)
        mock_client.post = MagicMock(return_value=bad_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            client = AuthClient(url="http://broker", api_key="key")
            result = client.get_token("anthropic")

        assert result is None

    def test_poll_non_200_continues_then_completes(self):
        """Poll returning non-200 is skipped; next poll with 200 completed returns token."""
        miss_resp = MagicMock()
        miss_resp.status_code = 404

        req_resp = MagicMock()
        req_resp.status_code = 200
        req_resp.json = MagicMock(return_value={"request_id": "req-1", "status": "pending"})

        error_poll = MagicMock()
        error_poll.status_code = 500

        ok_poll = MagicMock()
        ok_poll.status_code = 200
        ok_poll.json = MagicMock(return_value={"status": "completed", "token": {"token": "tok"}})

        poll_calls = [0]

        def mock_get(url, **kwargs):
            if "token/direct" in url:
                return miss_resp
            poll_calls[0] += 1
            return error_poll if poll_calls[0] == 1 else ok_poll

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get = MagicMock(side_effect=mock_get)
        mock_client.post = MagicMock(return_value=req_resp)

        with patch("agenticore.auth_client.httpx.Client", return_value=mock_client):
            with patch("agenticore.auth_client.time.sleep"):
                client = AuthClient(url="http://broker", api_key="key", poll_interval=0)
                result = client.get_token("anthropic", timeout=60)

        assert result == {"token": "tok"}


@pytest.mark.unit
class TestGetCredential:
    def test_returns_token_string(self):
        token_dict = {"token": "sk-ant-xyz", "expires_at": 0}
        client = AuthClient(url="http://broker", api_key="key")
        with patch.object(client, "get_token", return_value=token_dict):
            result = client.get_credential("anthropic")
        assert result == "sk-ant-xyz"

    def test_disabled_returns_none(self):
        client = AuthClient(url="", api_key="")
        result = client.get_credential("anthropic")
        assert result is None
