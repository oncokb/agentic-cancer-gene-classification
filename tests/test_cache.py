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


async def test_cache_miss_calls_compute_and_populates(_require_redis, monkeypatch):
    metric_calls = []
    monkeypatch.setattr(
        cache_module, "increment", lambda metric, tags=None: metric_calls.append((metric, tags))
    )
    calls = []

    async def compute():
        calls.append(1)
        return {"value": 42}

    result = await cached_call("test:miss-then-hit", compute)
    assert result == {"value": 42}
    assert len(calls) == 1
    assert ("redis_cache.lookups", ["cache_name:test", "result:miss"]) in metric_calls

    metric_calls.clear()
    result_again = await cached_call("test:miss-then-hit", compute)
    assert result_again == {"value": 42}
    assert len(calls) == 1  # second call served from cache, compute() not re-invoked
    assert ("redis_cache.lookups", ["cache_name:test", "result:hit"]) in metric_calls


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
    metric_calls = []
    monkeypatch.setattr(
        cache_module, "increment", lambda metric, tags=None: metric_calls.append((metric, tags))
    )

    calls = []

    async def compute():
        calls.append(1)
        return {"value": "computed anyway"}

    result = await cached_call("test:unreachable", compute)

    assert result == {"value": "computed anyway"}
    assert len(calls) == 1
    assert ("redis_cache.lookups", ["cache_name:test", "result:error"]) in metric_calls


def test_parse_sentinel_hosts_defaults_port_and_strips_whitespace():
    parsed = cache_module._parse_sentinel_hosts(
        " sentinel-0:26379, sentinel-1:26380 ,sentinel-2"
    )
    assert parsed == [("sentinel-0", 26379), ("sentinel-1", 26380), ("sentinel-2", 26379)]


def test_parse_sentinel_hosts_skips_empty_entries():
    assert cache_module._parse_sentinel_hosts("sentinel-0:26379,,") == [
        ("sentinel-0", 26379)
    ]


def test_get_client_sentinel_mode_connects_via_master_for(monkeypatch):
    seen_sentinel_args = {}
    seen_master_for_args = {}

    class FakeMasterClient:
        pass

    class FakeSentinel:
        def __init__(self, sentinels, **kwargs):
            seen_sentinel_args["sentinels"] = sentinels
            seen_sentinel_args.update(kwargs)

        def master_for(self, service_name, **kwargs):
            seen_master_for_args["service_name"] = service_name
            seen_master_for_args.update(kwargs)
            return FakeMasterClient()

    monkeypatch.setattr(cache_module, "Sentinel", FakeSentinel)
    monkeypatch.setattr(cache_module.settings, "redis_sentinel_enabled", True)
    monkeypatch.setattr(
        cache_module.settings, "redis_sentinel_hosts", "sentinel-0:26379,sentinel-1:26379"
    )
    monkeypatch.setattr(cache_module.settings, "redis_sentinel_master_set", "mymaster")
    monkeypatch.setattr(cache_module.settings, "redis_sentinel_password", "hunter2")
    cache_module._client = None

    client = cache_module._get_client()

    assert isinstance(client, FakeMasterClient)
    assert seen_sentinel_args["sentinels"] == [("sentinel-0", 26379), ("sentinel-1", 26379)]
    assert seen_sentinel_args["password"] == "hunter2"
    assert seen_sentinel_args["sentinel_kwargs"]["password"] == "hunter2"
    assert seen_master_for_args["service_name"] == "mymaster"

    cache_module._client = None
