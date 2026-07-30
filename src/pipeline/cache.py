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

from src.config import settings

logger = logging.getLogger(__name__)

_client: Optional[redis.Redis] = None


def _get_client() -> redis.Redis:
    global _client
    if _client is None:
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

    try:
        cached = await client.get(key)
        if cached is not None:
            return json.loads(cached)
    except Exception as exc:
        logger.warning("Redis cache read failed for %r, falling through to live call: %s", key, exc)

    result = await compute()

    try:
        await client.set(key, json.dumps(result), ex=ttl_seconds or settings.redis_cache_ttl_seconds)
    except Exception as exc:
        logger.warning("Redis cache write failed for %r: %s", key, exc)

    return result
