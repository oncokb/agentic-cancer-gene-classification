"""Shared test fixtures."""

import pytest

from src.pipeline import cache as cache_module


@pytest.fixture(autouse=True)
async def _reset_cache_client():
    """Reset the cache module's Redis client singleton before each test.

    redis.asyncio.Redis binds its connection pool to the event loop active
    at creation time. pytest-asyncio creates a fresh event loop per test
    function by default, so a client cached from an earlier test becomes
    unusable in a later test's loop — reset it so each test gets its own.
    """
    cache_module._client = None
    client = cache_module._get_client()
    try:
        await client.flushdb()
    except Exception:
        pass  # Redis not reachable — tests relying on it will skip themselves
    yield
    cache_module._client = None
