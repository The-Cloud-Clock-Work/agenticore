"""Redis-backed request queue and state management."""

import json
import secrets
import time
import uuid
from typing import List, Optional

import redis.asyncio as aioredis

from auth_broker.models import AuthStatus

_redis: Optional[aioredis.Redis] = None


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


# ── Dashboard sessions ────────────────────────────────────────────────────────


async def create_session(email: str) -> str:
    """Create a 24-hour dashboard session and return the opaque token."""
    token = secrets.token_hex(32)
    await _redis.setex(f"auth:session:{token}", 86400, email)
    return token


async def get_session(token: str) -> Optional[str]:
    """Return email for a valid session token, or None if expired/missing."""
    if not token:
        return None
    return await _redis.get(f"auth:session:{token}")


async def delete_session(token: str) -> None:
    if token:
        await _redis.delete(f"auth:session:{token}")
