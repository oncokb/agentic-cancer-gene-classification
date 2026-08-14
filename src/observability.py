"""Optional Datadog product metrics and custom spans."""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Iterator
from contextlib import contextmanager

from src.config import settings

logger = logging.getLogger(__name__)

_statsd_client = None
_statsd_warning_logged = False
_tracing_warning_logged = False


class NoopSpan:
    def set_tag(self, key: str, value) -> None:
        return None

    def set_tags(self, tags: dict) -> None:
        return None


def _statsd():
    global _statsd_client, _statsd_warning_logged
    if not settings.datadog_metrics_enabled:
        return None
    if _statsd_client is not None:
        return _statsd_client
    try:
        from datadog import DogStatsd
    except ImportError as exc:  # pragma: no cover - depends on optional package install
        if not _statsd_warning_logged:
            logger.warning("Datadog metrics enabled but DogStatsD client is unavailable: %s", exc)
            _statsd_warning_logged = True
        return None

    _statsd_client = DogStatsd(
        host=settings.datadog_statsd_host,
        port=settings.datadog_statsd_port,
        namespace=settings.datadog_metrics_namespace,
    )
    return _statsd_client


def _merge_tags(tags: Iterable[str] | None) -> list[str]:
    return list(tags or [])


def increment(metric: str, value: int = 1, tags: Iterable[str] | None = None) -> None:
    client = _statsd()
    if client is None:
        return
    client.increment(metric, value=value, tags=_merge_tags(tags))


def distribution(metric: str, value: float, tags: Iterable[str] | None = None) -> None:
    client = _statsd()
    if client is None:
        return
    client.distribution(metric, value, tags=_merge_tags(tags))


def set_metric(metric: str, value: str, tags: Iterable[str] | None = None) -> None:
    client = _statsd()
    if client is None:
        return
    client.set(metric, value, tags=_merge_tags(tags))


def stable_user_key(user_id: str) -> str:
    """Keep raw user identifiers out of Datadog metric payloads."""
    return hashlib.sha256(user_id.strip().lower().encode("utf-8")).hexdigest()


def record_user_seen(user_id: str | None, tags: Iterable[str] | None = None) -> None:
    if user_id:
        set_metric("users.active", stable_user_key(user_id), tags=tags)
    else:
        increment("users.anonymous_requests", tags=tags)


@contextmanager
def trace(name: str, resource: str | None = None, tags: dict | None = None) -> Iterator[NoopSpan]:
    global _tracing_warning_logged
    try:
        from ddtrace import tracer
    except ImportError as exc:  # pragma: no cover - depends on optional package install
        if not _tracing_warning_logged:
            logger.warning("Datadog tracing enabled but ddtrace is unavailable: %s", exc)
            _tracing_warning_logged = True
        yield NoopSpan()
        return

    with tracer.trace(name, resource=resource) as span:
        if tags:
            span.set_tags(tags)
        yield span


def tag_current_span(tags: dict) -> None:
    try:
        from ddtrace import tracer
    except ImportError:  # pragma: no cover - depends on optional package install
        return
    span = tracer.current_span()
    if span is not None:
        span.set_tags(tags)
