"""Unit tests for auth_broker.vault."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from auth_broker import vault


@pytest.mark.asyncio
class TestStoreToken:
    async def test_posts_to_correct_kv_path(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        vault._client = mock_client

        await vault.store_token("github", "agent1", {"token": "abc"})

        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/v1/secret/data/auth-broker/github/agent1"
        assert call_args[1]["json"] == {"data": {"token": "abc"}}

    async def test_raises_on_4xx(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        vault._client = mock_client

        with pytest.raises(Exception, match="403"):
            await vault.store_token("github", "agent1", {"token": "abc"})


@pytest.mark.asyncio
class TestGetToken:
    async def test_200_unwraps_data(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = MagicMock(return_value={"data": {"data": {"token": "xyz", "expires_at": 0}}})
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        vault._client = mock_client

        result = await vault.get_token("anthropic", "default")
        assert result == {"token": "xyz", "expires_at": 0}

    async def test_404_returns_none(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        vault._client = mock_client

        result = await vault.get_token("anthropic", "default")
        assert result is None

    async def test_500_raises(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("500 Server Error")
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        vault._client = mock_client

        with pytest.raises(Exception, match="500"):
            await vault.get_token("anthropic", "default")


@pytest.mark.asyncio
class TestDeleteToken:
    async def test_204_ok(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_resp)
        vault._client = mock_client
        await vault.delete_token("github", "agent1")  # no exception

    async def test_404_idempotent(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_resp)
        vault._client = mock_client
        await vault.delete_token("github", "agent1")  # no exception

    async def test_500_raises(self):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.raise_for_status.side_effect = Exception("500")
        mock_client = AsyncMock()
        mock_client.delete = AsyncMock(return_value=mock_resp)
        vault._client = mock_client
        with pytest.raises(Exception):
            await vault.delete_token("github", "agent1")


class TestIsValid:
    def test_manual_token_always_valid(self):
        assert vault.is_valid({"token": "mykey", "expires_at": 0}) is True

    def test_manual_token_empty_string_invalid(self):
        assert vault.is_valid({"token": "", "expires_at": 0}) is False

    def test_expired_token_invalid(self):
        past = int(time.time()) - 3600
        assert vault.is_valid({"token": "t", "expires_at": past}) is False

    def test_expiring_within_30s_invalid(self):
        soon = int(time.time()) + 15
        assert vault.is_valid({"token": "t", "expires_at": soon}) is False

    def test_future_token_valid(self):
        future = int(time.time()) + 3600
        assert vault.is_valid({"token": "t", "expires_at": future}) is True


class TestNeedsRefresh:
    def test_manual_token_never_needs_refresh(self):
        assert vault.needs_refresh({"token": "t", "expires_at": 0, "refresh_token": "r"}) is False

    def test_no_refresh_token(self):
        future = int(time.time()) + 100
        assert vault.needs_refresh({"token": "t", "expires_at": future, "refresh_token": ""}) is False

    def test_expiring_within_5min_with_refresh(self):
        soon = int(time.time()) + 200  # < 300s
        assert vault.needs_refresh({"token": "t", "expires_at": soon, "refresh_token": "r"}) is True

    def test_not_expiring_soon(self):
        future = int(time.time()) + 3600
        assert vault.needs_refresh({"token": "t", "expires_at": future, "refresh_token": "r"}) is False
