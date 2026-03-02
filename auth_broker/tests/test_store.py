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


@pytest.mark.asyncio
class TestAppendEvent:
    async def test_appends_to_list(self, redis_store):
        await store.append_event("new_request", {"service": "github"})
        length = await redis_store.llen("auth:events")
        assert length == 1

    async def test_newest_first(self, redis_store):
        await store.append_event("event_a", {"seq": 1})
        await store.append_event("event_b", {"seq": 2})
        events = await store.get_events(limit=10)
        assert events[0]["event"] == "event_b"
        assert events[1]["event"] == "event_a"

    async def test_entry_has_required_fields(self, redis_store):
        await store.append_event("token_ready", {"id": "req-1", "service": "anthropic"})
        events = await store.get_events(limit=1)
        assert len(events) == 1
        entry = events[0]
        assert entry["event"] == "token_ready"
        assert entry["data"] == {"id": "req-1", "service": "anthropic"}
        assert "ts" in entry

    async def test_get_events_empty(self, redis_store):
        events = await store.get_events(limit=10)
        assert events == []

    async def test_get_events_respects_limit(self, redis_store):
        for i in range(10):
            await store.append_event("e", {"i": i})
        events = await store.get_events(limit=3)
        assert len(events) == 3

    async def test_trim_to_event_max(self, redis_store):
        original_max = store.EVENT_MAX
        store.EVENT_MAX = 3
        try:
            for i in range(5):
                await store.append_event("e", {"i": i})
            length = await redis_store.llen("auth:events")
            assert length == 3
        finally:
            store.EVENT_MAX = original_max


@pytest.mark.asyncio
class TestPushHistory:
    async def test_adds_to_zset(self, redis_store):
        req_data = {"id": "req-1", "service": "github", "status": "completed", "consumer_id": "default"}
        await store.push_history(req_data)
        count = await redis_store.zcard("auth:history")
        assert count == 1

    async def test_get_history_empty(self, redis_store):
        result = await store.get_history(limit=10)
        assert result == []

    async def test_get_history_returns_dict(self, redis_store):
        req_data = {"id": "req-1", "service": "anthropic", "status": "completed", "consumer_id": "default"}
        await store.push_history(req_data)
        history = await store.get_history(limit=10)
        assert len(history) == 1
        assert history[0]["id"] == "req-1"
        assert history[0]["service"] == "anthropic"

    async def test_get_history_newest_first(self, redis_store):
        import asyncio
        await store.push_history({"id": "req-1", "service": "a", "status": "completed"})
        await asyncio.sleep(0.01)
        await store.push_history({"id": "req-2", "service": "b", "status": "denied"})
        history = await store.get_history(limit=10)
        assert history[0]["id"] == "req-2"
        assert history[1]["id"] == "req-1"

    async def test_get_history_respects_limit(self, redis_store):
        for i in range(10):
            await store.push_history({"id": f"req-{i}", "service": "s", "status": "completed"})
        history = await store.get_history(limit=3)
        assert len(history) == 3

    async def test_trim_to_history_max(self, redis_store):
        original_max = store.HISTORY_MAX
        store.HISTORY_MAX = 3
        try:
            for i in range(5):
                await store.push_history({"id": f"req-{i}", "service": "s", "status": "completed"})
            count = await redis_store.zcard("auth:history")
            assert count == 3
        finally:
            store.HISTORY_MAX = original_max


@pytest.mark.asyncio
class TestUpdateStatusHistory:
    async def test_completed_pushed_to_history(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.update_status(rid, AuthStatus.completed)
        history = await store.get_history(limit=10)
        ids = [h["id"] for h in history]
        assert rid in ids

    async def test_denied_pushed_to_history(self, redis_store):
        rid = await store.create_request("anthropic", "default", "", [])
        await store.update_status(rid, AuthStatus.denied)
        history = await store.get_history(limit=10)
        ids = [h["id"] for h in history]
        assert rid in ids

    async def test_url_ready_not_in_history(self, redis_store):
        rid = await store.create_request("github", "default", "", [])
        await store.update_status(rid, AuthStatus.url_ready)
        history = await store.get_history(limit=10)
        assert history == []
