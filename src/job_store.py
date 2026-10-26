"""Shared store for async job status snapshots (annotation, enrichment,
gene-query and fusion-evidence jobs).

A job's asyncio task still runs in the process that accepted it; this store
only makes its state/progress/result readable from any pod. Every write keeps
the live job object in a process-local map (so the submitting process sees
its own writes immediately and behaves exactly as before when Redis is down)
and mirrors a JSON snapshot to Redis under ``job:<kind>:<id>`` with a TTL.
Reads check the local map first, then Redis.

Both copies expire ``settings.job_ttl_seconds`` after the job's last write, so
finished jobs of every kind are evicted while running jobs (which write on
every progress step) stay alive.

Fails open like the reference-data cache: a Redis error is logged, Redis is
skipped for a short back-off, and the local map keeps serving this process.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Generic, Optional, Tuple, Type, TypeVar

from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

JobT = TypeVar("JobT", bound=BaseModel)

# After a Redis error, don't retry the same client for this long — a down
# Redis must not add a connect timeout to every progress update.
REDIS_RETRY_BACKOFF_SECONDS = 30.0
# Local expired entries are swept at most this often (on write).
_LOCAL_SWEEP_INTERVAL_SECONDS = 60.0


def _default_redis_client():
    """The shared Redis client (honours Sentinel), or None when unconfigured."""
    if not settings.redis_sentinel_enabled and not settings.redis_url.strip():
        return None
    from src.pipeline import cache

    return cache._get_client()


class JobStore(Generic[JobT]):
    def __init__(
        self,
        kind: str,
        model: Type[JobT],
        *,
        redis_client_factory: Optional[Callable[[], Any]] = _default_redis_client,
        ttl_seconds: Optional[Callable[[], int]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.kind = kind
        self.model = model
        self._redis_client_factory = redis_client_factory
        self._ttl_seconds = ttl_seconds or (lambda: settings.job_ttl_seconds)
        self._clock = clock
        self.local: Dict[str, Tuple[JobT, float]] = {}
        self._lock = asyncio.Lock()
        self._last_sweep = clock()
        # (client, retry_at) after a failure; a new client resets it.
        self._redis_down: Optional[Tuple[Any, float]] = None

    def key(self, job_id: str) -> str:
        return f"job:{self.kind}:{job_id}"

    def _redis(self):
        if self._redis_client_factory is None:
            return None
        try:
            client = self._redis_client_factory()
        except Exception as exc:
            logger.warning("Job store Redis client unavailable: %s", exc)
            return None
        if client is None:
            return None
        if self._redis_down is not None:
            down_client, retry_at = self._redis_down
            if down_client is client and self._clock() < retry_at:
                return None
            self._redis_down = None
        return client

    def _mark_redis_down(self, client, exc: Exception) -> None:
        logger.warning("Job store Redis %s unavailable, serving from process memory: %s", self.kind, exc)
        self._redis_down = (client, self._clock() + REDIS_RETRY_BACKOFF_SECONDS)

    def _dump(self, job: JobT) -> str:
        data = job.model_dump(mode="json")
        # Fields excluded from the public response (owner, kind, ...) are
        # still part of the stored snapshot.
        for name, field in type(job).model_fields.items():
            if field.exclude:
                data[name] = getattr(job, name)
        return json.dumps(data)

    def _load(self, raw: Any) -> JobT:
        if isinstance(raw, bytes):
            raw = raw.decode()
        return self.model.model_validate(json.loads(raw))

    def _sweep_local(self, now: float) -> None:
        if now - self._last_sweep < _LOCAL_SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        for job_id in [job_id for job_id, (_, expires_at) in self.local.items() if expires_at <= now]:
            del self.local[job_id]

    async def put(self, job: JobT) -> None:
        ttl = max(1, int(self._ttl_seconds()))
        now = self._clock()
        self._sweep_local(now)
        self.local[job.job_id] = (job, now + ttl)
        client = self._redis()
        if client is None:
            return
        # Serialise under the lock so concurrent progress writes for the same
        # job can't land in Redis out of order: whoever writes last writes the
        # job's latest state.
        async with self._lock:
            try:
                await client.set(self.key(job.job_id), self._dump(job), ex=ttl)
            except Exception as exc:
                self._mark_redis_down(client, exc)

    async def get(self, job_id: str) -> Optional[JobT]:
        entry = self.local.get(job_id)
        if entry is not None:
            job, expires_at = entry
            if expires_at > self._clock():
                return job
            del self.local[job_id]
        client = self._redis()
        if client is None:
            return None
        try:
            raw = await client.get(self.key(job_id))
        except Exception as exc:
            self._mark_redis_down(client, exc)
            return None
        if raw is None:
            return None
        try:
            return self._load(raw)
        except Exception:
            logger.warning("Discarding unreadable job snapshot %s", self.key(job_id), exc_info=True)
            return None

    def clear_local(self) -> None:
        self.local.clear()


def job_owner(user: Any) -> Optional[str]:
    """Canonical owner id for a job: the caller's lowercased email (for an
    API key, the key owner's email)."""
    email = getattr(user, "email", None)
    return email.strip().lower() if email else None


def can_read_job(owner: Optional[str], user: Any) -> bool:
    """Owners and admins may read a job. Jobs with no recorded owner are
    admin-only."""
    from src.auth import is_admin

    if user is not None and is_admin(user):
        return True
    return owner is not None and owner == job_owner(user)
