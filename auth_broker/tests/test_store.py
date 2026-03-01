"""Unit tests for auth_broker.store."""

import pytest

from auth_broker import store
from auth_broker.models import AuthStatus


@pytest.mark.asyncio
class TestCreateRequest:
    async def test_returns_uuid_string(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        assert isinstance(rid, str)
        assert len(rid) == 36  # UUID4

    async def test_stores_redis_hash_fields(self, redis_store):
        rid = await store.create_request("github", "agent1", "http://cb", ["repo"])
        data = await redis_store.hgetall(f"auth:request:{rid}")
        assert data["service"] == "github"
        assert data["consumer_id"] == "agent1"
        assert data["callback_url"] == "http://cb"
        assert data["status"] == AuthStatus.pending.value

    async def test_added_to_pending_zset(self, redis_store):
        rid = await store.create_request("anthropic", "default", "", [])
        members = await redis_store.zrange("auth:pending", 0, -1)
        assert rid in members

    async def test_ttl_is_24h(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        ttl = await redis_store.ttl(f"auth:request:{rid}")
        assert 86390 <= ttl <= 86400


@pytest.mark.asyncio
class TestGetRequest:
    async def test_returns_dict_when_found(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        data = await store.get_request(rid)
        assert isinstance(data, dict)
        assert data["id"] == rid

    async def test_returns_none_when_missing(self, redis_store):
        result = await store.get_request("nonexistent-id")
        assert result is None


@pytest.mark.asyncio
class TestUpdateStatus:
    async def test_pending_stays_in_zset(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.update_status(rid, AuthStatus.url_ready)
        members = await redis_store.zrange("auth:pending", 0, -1)
        assert rid in members

    async def test_completed_removed_from_zset(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.update_status(rid, AuthStatus.completed)
        members = await redis_store.zrange("auth:pending", 0, -1)
        assert rid not in members

    async def test_denied_removed_from_zset(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.update_status(rid, AuthStatus.denied)
        members = await redis_store.zrange("auth:pending", 0, -1)
        assert rid not in members

    async def test_extra_kwargs_stored(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.update_status(rid, AuthStatus.url_ready, auth_url="https://github.com/login")
        data = await redis_store.hgetall(f"auth:request:{rid}")
        assert data["auth_url"] == "https://github.com/login"


@pytest.mark.asyncio
class TestGetPending:
    async def test_empty_list(self, redis_store):
        result = await store.get_pending()
        assert result == []

    async def test_returns_all_pending(self, redis_store):
        rid1 = await store.create_request("github", "a", "", [])
        rid2 = await store.create_request("anthropic", "b", "", [])
        result = await store.get_pending()
        ids = [r["id"] for r in result]
        assert rid1 in ids
        assert rid2 in ids


@pytest.mark.asyncio
class TestOAuthState:
    async def test_store_and_retrieve(self, redis_store):
        await store.store_oauth_state("abc123", "req-456")
        result = await store.get_request_by_state("abc123")
        assert result == "req-456"

    async def test_missing_state_returns_none(self, redis_store):
        result = await store.get_request_by_state("no-such-state")
        assert result is None


@pytest.mark.asyncio
class TestDeleteRequest:
    async def test_hash_removed(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.delete_request(rid)
        data = await redis_store.hgetall(f"auth:request:{rid}")
        assert data == {}

    async def test_removed_from_zset(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.delete_request(rid)
        members = await redis_store.zrange("auth:pending", 0, -1)
        assert rid not in members
