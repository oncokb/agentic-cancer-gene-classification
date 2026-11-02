"""Shared store for async job status snapshots (annotation, enrichment,
gene-query and fusion-evidence jobs).

A job's asyncio task still runs in the process that accepted it; this store
only makes its state/progress/result readable from any pod. Every write keeps
the live job object in a process-local map (so the submitting process sees
its own writes immediately and behaves exactly as before when Redis is down)
and mirrors a JSON snapshot to Redis under ``job:<kind>:<id>``. Gene-query
jobs are annotation jobs with ``kind="gene_query"``, so their snapshots live
under ``job:annotation:<id>``. Reads check the local map first, then Redis.

Snapshots expire ``job_ttl_seconds()`` after the job's last write, so finished
jobs of every kind are evicted. Jobs that haven't finished are never evicted
from the submitting process (its worker task keeps reading them), and their
Redis TTL is refreshed by the periodic sweep even if progress stalls.

Fails open like the reference-data cache: a Redis error is logged, Redis is
skipped for a short back-off, and the local map keeps serving this process.
Writes that couldn't reach Redis stay dirty and the latest snapshot of each
is republished once Redis is reachable again: on the next store read or
write in this process, or by a retry timer, whichever comes first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import time
import weakref
from typing import Any, Callable, Dict, Generic, Optional, Set, Tuple, Type, TypeVar

from pydantic import BaseModel

from src.config import settings

logger = logging.getLogger(__name__)

JobT = TypeVar("JobT", bound=BaseModel)

DEFAULT_JOB_TTL_SECONDS = 86400
TERMINAL_STATUSES = frozenset({"complete", "failed"})
# After a Redis error, don't retry the same client for this long — a down
# Redis must not add a connect timeout to every progress update.
REDIS_RETRY_BACKOFF_SECONDS = 30.0
# Expired local entries are swept (and stalled running jobs' TTLs refreshed)
# at most this often, on any store read or write.
_LOCAL_SWEEP_INTERVAL_SECONDS = 60.0

_warned_legacy_ttl = False


def job_ttl_seconds() -> int:
    """JOB_TTL_SECONDS, else the deprecated ANNOTATION_JOB_TTL_SECONDS (now
    applied to every job kind), else 24h."""
    global _warned_legacy_ttl
    if settings.job_ttl_seconds is not None:
        return settings.job_ttl_seconds
    if settings.annotation_job_ttl_seconds is not None:
        if not _warned_legacy_ttl:
            _warned_legacy_ttl = True
            logger.warning(
                "ANNOTATION_JOB_TTL_SECONDS is deprecated and now applies to every job kind; "
                "set JOB_TTL_SECONDS instead."
            )
        return settings.annotation_job_ttl_seconds
    return DEFAULT_JOB_TTL_SECONDS


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
        ttl_seconds: Callable[[], int] = job_ttl_seconds,
        clock: Callable[[], float] = time.monotonic,
        call_later: Optional[Callable[[float, Callable[[], None]], Any]] = None,
    ) -> None:
        self.kind = kind
        self.model = model
        self._redis_client_factory = redis_client_factory
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        # Schedules the retry timer; defaults to the running loop's call_later.
        self._call_later = call_later
        # job_id -> (live job object, local expiry on `clock`)
        self.local: Dict[str, Tuple[JobT, float]] = {}
        # job_id -> write sequence number of the newest snapshot not yet in Redis
        self._dirty: Dict[str, int] = {}
        self._seq = 0
        self._last_sweep = clock()
        # (client, retry_at) after a failure; a new client resets it.
        self._redis_down: Optional[Tuple[Any, float]] = None
        # asyncio.Lock binds to one loop on Python 3.9; keep one per loop.
        self._locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
            weakref.WeakKeyDictionary()
        )
        self._retry_timer: Optional[Tuple[asyncio.AbstractEventLoop, Any]] = None
        self._retry_tasks: Set[asyncio.Task] = set()

    def key(self, job_id: str) -> str:
        return f"job:{self.kind}:{job_id}"

    def _lock(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = self._locks[loop] = asyncio.Lock()
        return lock

    def _redis(self):
        """The Redis client to use now, or None (unconfigured or backing off)."""
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

    def _mark_dirty(self, job_id: str) -> None:
        self._seq += 1
        self._dirty[job_id] = self._seq

    def _forget(self, job_id: str) -> None:
        self.local.pop(job_id, None)
        self._dirty.pop(job_id, None)

    def _sweep(self, now: float) -> None:
        """Drop expired finished jobs; give unfinished ones a fresh TTL (and
        republish them so their Redis copy doesn't lapse either)."""
        if now - self._last_sweep < _LOCAL_SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        ttl = max(1, int(self._ttl_seconds()))
        for job_id, (job, expires_at) in list(self.local.items()):
            if getattr(job, "status", None) in TERMINAL_STATUSES:
                if expires_at <= now:
                    self._forget(job_id)
            elif expires_at - now <= ttl / 2:
                self.local[job_id] = (job, now + ttl)
                self._mark_dirty(job_id)

    async def _flush(self) -> None:
        """Write every dirty snapshot (its latest state) to Redis."""
        if not self._dirty:
            return
        if self._redis() is None:
            self._schedule_retry()
            return
        async with self._lock():
            # Re-check under the lock: a writer queued behind one that just
            # hit a dead Redis must back off rather than retry it, and the
            # first writer through flushes everyone's snapshots.
            client = self._redis()
            if client is None:
                self._schedule_retry()
                return
            for job_id, seq in list(self._dirty.items()):
                entry = self.local.get(job_id)
                if entry is None:
                    self._dirty.pop(job_id, None)
                    continue
                job, expires_at = entry
                now = self._clock()
                if expires_at <= now:
                    if getattr(job, "status", None) in TERMINAL_STATUSES:
                        # Expired while waiting to be republished: let it go
                        # rather than resurrect it in Redis.
                        self._forget(job_id)
                        continue
                    expires_at = now + max(1, int(self._ttl_seconds()))
                    self.local[job_id] = (job, expires_at)
                ex = max(1, math.ceil(expires_at - now))
                try:
                    await client.set(self.key(job_id), self._dump(job), ex=ex)
                except Exception as exc:
                    self._mark_redis_down(client, exc)
                    self._schedule_retry()
                    return
                # A newer write that landed while we awaited stays dirty.
                if self._dirty.get(job_id) == seq:
                    del self._dirty[job_id]

    def _schedule_retry(self) -> None:
        """Arm a one-shot timer that republishes dirty snapshots after the
        back-off, so a job that finished while Redis was down still reaches
        other pods even if this process sees no further job traffic. A timer
        (not a sleeping task) so nothing is left pending on a closed loop."""
        if self._redis_down is None:
            return  # Redis unconfigured, not failing: nothing to retry
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._retry_timer is not None:
            timer_loop, handle = self._retry_timer
            if timer_loop is loop and not loop.is_closed() and not handle.cancelled():
                return
        call_later = self._call_later or loop.call_later
        self._retry_timer = (loop, call_later(REDIS_RETRY_BACKOFF_SECONDS + 1, self._on_retry_timer))

    def _on_retry_timer(self) -> None:
        self._retry_timer = None
        if not self._dirty:
            return
        task = asyncio.get_running_loop().create_task(self._flush())
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def put(self, job: JobT) -> None:
        now = self._clock()
        self._sweep(now)
        self.local[job.job_id] = (job, now + max(1, int(self._ttl_seconds())))
        self._mark_dirty(job.job_id)
        await self._flush()

    async def get(self, job_id: str) -> Optional[JobT]:
        now = self._clock()
        self._sweep(now)
        await self._flush()
        entry = self.local.get(job_id)
        if entry is not None:
            job, expires_at = entry
            if expires_at > now or getattr(job, "status", None) not in TERMINAL_STATUSES:
                return job
            self._forget(job_id)
        client = self._redis()
        if client is None:
            return None
        try:
            raw = await client.get(self.key(job_id))
        except Exception as exc:
            self._mark_redis_down(client, exc)
            self._schedule_retry()
            return None
        if raw is None:
            return None
        try:
            return self._load(raw)
        except Exception:
            logger.warning("Discarding unreadable job snapshot %s", self.key(job_id), exc_info=True)
            return None

    def clear_local(self) -> None:
        """Forget all process-local state (jobs, pending republishes, retry
        timer/tasks, back-off, locks); Redis is untouched."""
        self.local.clear()
        self._dirty.clear()
        if self._retry_timer is not None:
            self._retry_timer[1].cancel()
            self._retry_timer = None
        for task in list(self._retry_tasks):
            try:
                task.cancel()
            except RuntimeError:
                pass  # its loop is already closed
        self._retry_tasks.clear()
        self._redis_down = None
        self._locks = weakref.WeakKeyDictionary()


def job_owner(user: Any) -> Optional[str]:
    """Canonical owner id for a job: the caller's lowercased email (for an
    API key, the key owner's email)."""
    email = getattr(user, "email", None)
    return email.strip().lower() if email else None


def can_read_job(owner: Optional[str], user: Any) -> bool:
    """Owners and real admins may read a job. AGCG_DEV_MODE deliberately does
    not count as admin here (unlike require_admin), so dev mode with auth on
    doesn't expose every user's jobs. Jobs with no recorded owner are
    admin-only."""
    if user is not None and getattr(user, "role", None) == "admin":
        return True
    return owner is not None and owner == job_owner(user)
