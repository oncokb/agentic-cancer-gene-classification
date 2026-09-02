"""Redis-backed cache-aside helper for slow-changing external reference lookups.

Wraps read-through external calls (HGNC gene normalization, the OncoKB
curated gene list, PubMed literature) with a TTL-based cache, so identical
lookups across pipeline runs don't re-hit rate-limited third-party APIs.

Fails open: if Redis is unreachable, callers fall through to a live fetch
rather than erroring — these lookups sit on the pipeline's critical path,
so cache availability must never block an annotation run. Only successful
compute() results are cached; exceptions from compute() propagate normally
and are never written to the cache.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as redis
from redis.asyncio.sentinel import Sentinel

from src.config import settings
from src.observability import increment

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


def _parse_sentinel_hosts(raw: str) -> list[tuple[str, int]]:
    hosts = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, _, port = entry.partition(":")
        hosts.append((host, int(port) if port else 26379))
    return hosts


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
        if settings.redis_sentinel_enabled:
            password = settings.redis_sentinel_password or None
            sentinel = Sentinel(
                _parse_sentinel_hosts(settings.redis_sentinel_hosts),
                sentinel_kwargs={
                    "socket_connect_timeout": 1,
                    "socket_timeout": 1,
                    "password": password,
                },
                socket_connect_timeout=1,
                socket_timeout=1,
                password=password,
            )
            # All reads and writes go through the master. This is a
            # low-volume cache, not a read-scaling workload — routing
            # everything through one place avoids replica-lag staleness.
            _client = sentinel.master_for(settings.redis_sentinel_master_set)
        else:
            _client = redis.from_url(
                settings.redis_url, socket_connect_timeout=1, socket_timeout=1
            )
    return _client


async def cached_call(
    key: str,
    compute: Callable[[], Awaitable[Any]],
    ttl_seconds: Optional[int] = None,
) -> Any:
    """Return compute()'s JSON-serializable result, caching it in Redis under `key`."""
    client = _get_client()
    # Key prefix (e.g. "hgnc", "oncokb", "pubmed") as a low-cardinality tag —
    # never the full key, which embeds gene symbols/query text.
    cache_tags = [f"cache_name:{key.split(':', 1)[0]}"]

    try:
        cached = await client.get(key)
        if cached is not None:
            increment("redis_cache.lookups", tags=cache_tags + ["result:hit"])
            return json.loads(cached)
        increment("redis_cache.lookups", tags=cache_tags + ["result:miss"])
    except Exception as exc:
        logger.warning("Redis cache read failed for %r, falling through to live call: %s", key, exc)
        increment("redis_cache.lookups", tags=cache_tags + ["result:error"])

    result = await compute()

    try:
        await client.set(key, json.dumps(result), ex=ttl_seconds or settings.redis_cache_ttl_seconds)
    except Exception as exc:
        logger.warning("Redis cache write failed for %r: %s", key, exc)

    return result
