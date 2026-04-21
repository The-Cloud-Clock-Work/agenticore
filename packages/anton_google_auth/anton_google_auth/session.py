"""Redis-backed session management for Anton Google Auth."""

import secrets
from typing import Optional

import redis.asyncio as aioredis


class SessionStore:
    """Thin Redis wrapper for 24-hour dashboard sessions."""

    SESSION_PREFIX = "anton:session:"
    SESSION_TTL = 86400  # 24 hours
    STATE_PREFIX = "anton:oauth_state:"
    STATE_TTL = 600  # 10 minutes

    def __init__(self, redis_url: str) -> None:
        self._url = redis_url
        self._redis: Optional[aioredis.Redis] = None

    async def connect(self) -> None:
        self._redis = await aioredis.from_url(self._url, decode_responses=True)

    async def close(self) -> None:
        if self._redis:
            await self._redis.aclose()
            self._redis = None

    # ── Sessions ──────────────────────────────────────────────────────────────

    async def create(self, email: str) -> str:
        """Create a session and return the opaque token."""
        token = secrets.token_hex(32)
        await self._redis.setex(f"{self.SESSION_PREFIX}{token}", self.SESSION_TTL, email)
        return token

    async def get(self, token: str) -> Optional[str]:
        """Return the email for a valid token, or None."""
        if not token:
            return None
        return await self._redis.get(f"{self.SESSION_PREFIX}{token}")

    async def delete(self, token: str) -> None:
        if token:
            await self._redis.delete(f"{self.SESSION_PREFIX}{token}")

    # ── OAuth state ───────────────────────────────────────────────────────────

    async def save_state(self, state: str) -> None:
        await self._redis.setex(f"{self.STATE_PREFIX}{state}", self.STATE_TTL, "1")

    async def consume_state(self, state: str) -> bool:
        """Return True and delete if state is valid, False otherwise."""
        key = f"{self.STATE_PREFIX}{state}"
        val = await self._redis.get(key)
        if val:
            await self._redis.delete(key)
            return True
        return False
