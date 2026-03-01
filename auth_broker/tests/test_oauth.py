"""Unit tests for auth_broker.oauth."""

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth_broker import oauth


PROVIDER = {
    "auth_type": "oauth2",
    "client_id_env": "GITHUB_CLIENT_ID",
    "client_secret_env": "GITHUB_CLIENT_SECRET",
    "authorize_url": "https://github.com/login/oauth/authorize",
    "token_url": "https://github.com/login/oauth/access_token",
    "scopes": ["repo", "read:user"],
}


@pytest.mark.asyncio
class TestBuildAuthUrl:
    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "my-client-id"})
    async def test_contains_client_id(self):
        url = await oauth.build_auth_url(PROVIDER, "state-123")
        assert "client_id=my-client-id" in url

    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"})
    async def test_contains_state(self):
        url = await oauth.build_auth_url(PROVIDER, "unique-state")
        assert "state=unique-state" in url

    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"})
    async def test_contains_redirect_uri(self):
        url = await oauth.build_auth_url(PROVIDER, "s")
        assert "redirect_uri=" in url

    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid"})
    async def test_scopes_encoded(self):
        url = await oauth.build_auth_url(PROVIDER, "s")
        assert "scope=" in url
        assert "repo" in url


@pytest.mark.asyncio
class TestExchangeCode:
    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid", "GITHUB_CLIENT_SECRET": "sec"})
    async def test_posts_correct_fields(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"access_token": "tok", "token_type": "bearer"})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("auth_broker.oauth.httpx.AsyncClient", return_value=mock_client):
            result = await oauth.exchange_code(PROVIDER, "auth-code-xyz")

        call_kwargs = mock_client.post.call_args[1]
        assert call_kwargs["data"]["grant_type"] == "authorization_code"
        assert call_kwargs["data"]["code"] == "auth-code-xyz"
        assert call_kwargs["data"]["client_id"] == "cid"
        assert call_kwargs["data"]["client_secret"] == "sec"

    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid", "GITHUB_CLIENT_SECRET": "sec"})
    async def test_200_returns_dict(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"access_token": "tok"})
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("auth_broker.oauth.httpx.AsyncClient", return_value=mock_client):
            result = await oauth.exchange_code(PROVIDER, "code")

        assert result["access_token"] == "tok"

    @patch.dict(os.environ, {"GITHUB_CLIENT_ID": "cid", "GITHUB_CLIENT_SECRET": "sec"})
    async def test_4xx_raises(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("400 Bad Request")
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with patch("auth_broker.oauth.httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(Exception, match="400"):
                await oauth.exchange_code(PROVIDER, "bad-code")
