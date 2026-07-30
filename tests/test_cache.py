"""Tests for the Redis-backed cache-aside helper.

Runs against a real Redis instance (REDIS_URL env var / docker-compose's
`redis` service). Skipped cleanly if Redis isn't reachable, and includes
a dedicated fail-open test pointed at a deliberately unreachable Redis.
"""

from __future__ import annotations

import pytest

from src.pipeline import cache as cache_module
from src.pipeline.cache import cached_call


@pytest.fixture
async def _require_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")


async def test_cache_miss_calls_compute_and_populates(_require_redis):
    calls = []

    async def compute():
        calls.append(1)
        return {"value": 42}

    result = await cached_call("test:miss-then-hit", compute)
    assert result == {"value": 42}
    assert len(calls) == 1

    result_again = await cached_call("test:miss-then-hit", compute)
    assert result_again == {"value": 42}
    assert len(calls) == 1  # second call served from cache, compute() not re-invoked


async def test_cache_hit_skips_compute(_require_redis):
    client = cache_module._get_client()
    await client.set("test:pre-seeded", '{"cached": true}')

    async def compute():
        raise AssertionError("compute() should not run on a cache hit")

    result = await cached_call("test:pre-seeded", compute)

    assert result == {"cached": True}


async def test_cached_call_fails_open_when_redis_unreachable(monkeypatch):
    monkeypatch.setattr(
        cache_module.settings, "redis_url", "redis://127.0.0.1:1/0"
    )  # port 1: nothing listens here
    cache_module._client = None

    calls = []

    async def compute():
        calls.append(1)
        return {"value": "computed anyway"}

    result = await cached_call("test:unreachable", compute)

    assert result == {"value": "computed anyway"}
    assert len(calls) == 1
