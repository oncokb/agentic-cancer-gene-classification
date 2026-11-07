"""The module's own small Redis client (single-flight and single-loader leases).

Built lazily from public settings only: ``EXPRESSION_CONTEXT_REDIS_URL`` if
set, else ACGC's public ``REDIS_URL`` / ``REDIS_SENTINEL_*`` settings (same
master-only Sentinel routing as the core cache). Never reaches into
``src.pipeline.cache`` internals. Closed by the service on shutdown.

Degrades gracefully: callers treat any error as "Redis unavailable", and after
a failure the client is skipped for a short back-off so a down Redis doesn't
add a connect timeout to every operation.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, List, Optional, Tuple

logger = logging.getLogger(__name__)

RETRY_BACKOFF_SECONDS = 30.0


class RedisUnavailable(RuntimeError):
    pass


def parse_sentinel_hosts(raw: str) -> List[Tuple[str, int]]:
    hosts = []
    for entry in (raw or "").split(","):
        entry = entry.strip()
        if not entry:
            continue
        host, _, port = entry.partition(":")
        hosts.append((host, int(port) if port else 26379))
    return hosts


def build_client(redis_url: str = "") -> Any:
    """A redis.asyncio client from EXPRESSION_CONTEXT_REDIS_URL or ACGC's public settings."""
    import redis.asyncio as redis

    from src.config import settings as acgc_settings

    if redis_url.strip():
        return redis.from_url(redis_url.strip(), socket_connect_timeout=1, socket_timeout=1)
    if acgc_settings.redis_sentinel_enabled:
        from redis.asyncio.sentinel import Sentinel

        password = acgc_settings.redis_sentinel_password or None
        sentinel = Sentinel(
            parse_sentinel_hosts(acgc_settings.redis_sentinel_hosts),
            sentinel_kwargs={"socket_connect_timeout": 1, "socket_timeout": 1, "password": password},
            socket_connect_timeout=1,
            socket_timeout=1,
            password=password,
        )
        return sentinel.master_for(acgc_settings.redis_sentinel_master_set)
    return redis.from_url(acgc_settings.redis_url, socket_connect_timeout=1, socket_timeout=1)


class ModuleRedis:
    """Lazily-built client with a failure back-off; ``client()`` raises
    RedisUnavailable while backing off."""

    def __init__(
        self,
        factory: Optional[Callable[[], Any]] = None,
        *,
        redis_url: str = "",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._factory = factory or (lambda: build_client(redis_url))
        self._client: Optional[Any] = None
        self._down_until = 0.0
        self._clock = clock

    def client(self) -> Any:
        if self._clock() < self._down_until:
            raise RedisUnavailable("backing off after a Redis error")
        if self._client is None:
            self._client = self._factory()
        return self._client

    def mark_down(self, exc: BaseException) -> None:
        logger.warning(
            "Expression-context Redis error (%s); skipping Redis for %.0fs",
            exc, RETRY_BACKOFF_SECONDS,
        )
        self._down_until = self._clock() + RETRY_BACKOFF_SECONDS

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        close = getattr(client, "aclose", None) or getattr(client, "close", None)
        if close is None:
            return
        try:
            await close()
        except Exception as exc:
            logger.debug("Expression-context Redis close failed: %s", exc)
