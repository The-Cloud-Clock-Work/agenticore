"""Redis-backed request queue and state management."""

import json
import time
import uuid
from typing import List, Optional

import redis.asyncio as aioredis

from auth_broker.models import AuthStatus

_redis: Optional[aioredis.Redis] = None

EVENT_MAX = 1000
HISTORY_MAX = 200


async def connect(redis_url: str) -> None:
    global _redis
    _redis = await aioredis.from_url(redis_url, decode_responses=True)


async def close() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        _redis = None


def get_redis() -> aioredis.Redis:
    return _redis


async def create_request(
    service: str,
    consumer_id: str,
    callback_url: str,
    scopes: List[str],
) -> str:
    request_id = str(uuid.uuid4())
    now = int(time.time())
    data = {
        "id": request_id,
        "service": service,
        "consumer_id": consumer_id,
        "callback_url": callback_url,
        "scopes": json.dumps(scopes),
        "status": AuthStatus.pending.value,
        "created_at": str(now),
        "oauth_state": str(uuid.uuid4()),
    }
    pipe = _redis.pipeline()
    pipe.hset(f"auth:request:{request_id}", mapping=data)
    pipe.expire(f"auth:request:{request_id}", 86400)  # 24h TTL
    pipe.zadd("auth:pending", {request_id: now})
    await pipe.execute()
    return request_id


async def get_request(request_id: str) -> Optional[dict]:
    data = await _redis.hgetall(f"auth:request:{request_id}")
    return data if data else None


async def update_status(request_id: str, status: AuthStatus, **extra) -> None:
    updates = {"status": status.value}
    for k, v in extra.items():
        updates[k] = str(v)
    await _redis.hset(f"auth:request:{request_id}", mapping=updates)
    if status in (AuthStatus.completed, AuthStatus.denied):
        await _redis.zrem("auth:pending", request_id)
        data = await get_request(request_id)
        if data:
            await push_history(data)


async def get_pending() -> List[dict]:
    ids = await _redis.zrange("auth:pending", 0, -1)
    results = []
    for rid in ids:
        data = await get_request(rid)
        if data:
            results.append(data)
    return results


async def store_pkce_verifier(request_id: str, verifier: str) -> None:
    await _redis.hset(f"auth:request:{request_id}", "pkce_verifier", verifier)


async def store_oauth_state(state: str, request_id: str) -> None:
    await _redis.setex(f"auth:state:{state}", 600, request_id)


async def get_request_by_state(state: str) -> Optional[str]:
    return await _redis.get(f"auth:state:{state}")


async def delete_request(request_id: str) -> None:
    await _redis.delete(f"auth:request:{request_id}")
    await _redis.zrem("auth:pending", request_id)


async def append_event(event: str, data: dict) -> None:
    """Append an event to the persistent event log (Redis list, newest-first)."""
    entry = json.dumps({"ts": int(time.time()), "event": event, "data": data})
    await _redis.lpush("auth:events", entry)
    await _redis.ltrim("auth:events", 0, EVENT_MAX - 1)


async def get_events(limit: int = 200) -> list:
    """Return up to `limit` events, newest first."""
    raw = await _redis.lrange("auth:events", 0, limit - 1)
    return [json.loads(r) for r in raw]


async def push_history(request_data: dict) -> None:
    """Add completed/denied request to history ZSET (scored by timestamp)."""
    ts = int(time.time())
    await _redis.zadd("auth:history", {json.dumps(request_data): ts})
    count = await _redis.zcard("auth:history")
    if count > HISTORY_MAX:
        await _redis.zremrangebyrank("auth:history", 0, count - HISTORY_MAX - 1)


async def get_history(limit: int = 50) -> list:
    """Return up to `limit` historical requests, newest first."""
    raw = await _redis.zrevrange("auth:history", 0, limit - 1)
    return [json.loads(r) for r in raw]


async def deny_all_pending() -> list[str]:
    """Deny all pending requests. Returns list of denied request IDs."""
    ids = await _redis.zrange("auth:pending", 0, -1)
    denied_ids = []
    for rid in ids:
        data = await _redis.hgetall(f"auth:request:{rid}")
        if data:
            await update_status(rid, AuthStatus.denied)
            denied_ids.append(rid)
    return denied_ids


async def find_pending_by_service(service: str, consumer_id: str) -> List[str]:
    """Return request IDs from auth:pending that match service+consumer_id."""
    ids = await _redis.zrange("auth:pending", 0, -1)
    matches = []
    for rid in ids:
        data = await _redis.hgetall(f"auth:request:{rid}")
        if data and data.get("service") == service and data.get("consumer_id") == consumer_id:
            matches.append(rid)
    return matches
