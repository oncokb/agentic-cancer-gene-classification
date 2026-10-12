"""Best-effort DogStatsD metrics for the OpenEvidence sidecar.

Thin wrappers over src.observability's increment/distribution (so the
DATADOG_METRICS_ENABLED gate and the `acgc.` namespace apply exactly as for
every other product metric) that never raise: a metrics failure must never
change sidecar or warmup behavior.

Tag cardinality is bounded: model, outcome/status/result enums, booleans,
an HTTP status class, and `source`. Raw genes, tumor types, fusions, PMIDs,
users, URLs, and cache keys are never tags.
"""

from __future__ import annotations

import contextvars
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Optional

from src import observability
from src.config import settings

logger = logging.getLogger(__name__)

# Metric names (emitted under the DogStatsD namespace, i.e. acgc.<name>).
ENDPOINT_REQUESTS = "openevidence.endpoint.requests"
ENDPOINT_DURATION_MS = "openevidence.endpoint.duration_ms"
UPSTREAM_CALLS = "openevidence.upstream.calls"
UPSTREAM_DURATION_MS = "openevidence.upstream.duration_ms"
UPSTREAM_TIME_TO_FIRST_CHUNK_MS = "openevidence.upstream.time_to_first_chunk_ms"
RETRIES = "openevidence.retries"
CACHE_LOOKUPS = "openevidence.cache.lookups"
LOOKUPS_STARTED = "openevidence.lookups.started"
LOOKUPS_DEDUPED = "openevidence.lookups.deduped"
LOOKUPS_COMPLETED = "openevidence.lookups.completed"
LOOKUP_TIME_TO_READY_MS = "openevidence.lookups.time_to_ready_ms"
LEASE_UNAVAILABLE = "openevidence.lease.unavailable"
SEMAPHORE_WAIT_MS = "openevidence.semaphore_wait_ms"

# Which caller is driving an upstream call: "sidecar" (the endpoint's
# background lookup), "warmup" (src.pipeline.openevidence_warmup), or
# "other" (ad-hoc/benchmark use of OpenEvidenceClient). A ContextVar rather
# than a parameter so _post_streaming_analysis's signature — which tests
# replace with fakes — stays unchanged.
_call_source: contextvars.ContextVar[str] = contextvars.ContextVar("openevidence_call_source", default="other")


@contextmanager
def call_source(source: str) -> Iterator[None]:
    token = _call_source.set(source)
    try:
        yield
    finally:
        _call_source.reset(token)


def current_call_source() -> str:
    return _call_source.get()


def model_tag() -> str:
    return f"model:{settings.openevidence_model or 'unknown'}"


def bool_tag(name: str, value: object) -> str:
    return f"{name}:{'true' if value else 'false'}"


def http_status_class(status_code: Optional[int]) -> Optional[str]:
    if not status_code:
        return None
    return f"{int(status_code) // 100}xx"


def increment(metric: str, tags: Iterable[str] | None = None, value: int = 1) -> None:
    try:
        observability.increment(metric, value=value, tags=list(tags or []))
    except Exception as exc:  # metrics must never break callers
        logger.debug("OpenEvidence metric %s dropped: %s", metric, exc)


def distribution(metric: str, value: float, tags: Iterable[str] | None = None) -> None:
    try:
        observability.distribution(metric, value, tags=list(tags or []))
    except Exception as exc:  # metrics must never break callers
        logger.debug("OpenEvidence metric %s dropped: %s", metric, exc)
