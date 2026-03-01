"""Shared fixtures for auth_broker tests."""

import fakeredis
import pytest

from auth_broker import store
from auth_broker.config import reset_config


@pytest.fixture(autouse=True)
def reset_cfg():
    """Reset auth_broker config singleton before/after every test."""
    reset_config()
    yield
    reset_config()


@pytest.fixture
async def redis_store():
    """Inject a FakeAsyncRedis instance into store._redis."""
    r = fakeredis.FakeAsyncRedis(decode_responses=True)
    store._redis = r
    yield r
    await r.aclose()
    store._redis = None
