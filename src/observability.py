"""Optional Datadog product metrics and custom spans."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
import threading
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Dict, Optional

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


# DogStatsd's own background sender (datadog >= 0.43): emitting a metric only
# enqueues the packet (put with sender_queue_timeout=0, i.e. drop instead of
# block when the bounded queue is full) and a daemon thread does the socket
# I/O, so a slow or unreachable Agent can never stall the event loop.
_STATSD_SENDER_KWARGS: dict = {
    "disable_background_sender": False,
    "sender_queue_size": 4096,
    "sender_queue_timeout": 0,
}
_STATSD_STOP_TIMEOUT_SECONDS = 2.0

_statsd_init_lock = threading.Lock()
_statsd_init_thread_lock = threading.Lock()
_statsd_init_thread: Optional[threading.Thread] = None
_statsd_unavailable = False


def _build_statsd():
    global _statsd_warning_logged, _statsd_unavailable
    try:
        from datadog import DogStatsd
    except ImportError as exc:  # pragma: no cover - depends on optional package install
        if not _statsd_warning_logged:
            logger.warning("Datadog metrics enabled but DogStatsD client is unavailable: %s", exc)
            _statsd_warning_logged = True
        _statsd_unavailable = True
        return None

    # Only pin an explicit host/port when configured; otherwise leave them
    # unset so DogStatsd falls through to its own DD_DOGSTATSD_URL /
    # DD_AGENT_HOST / DD_DOGSTATSD_PORT env var detection, which is what
    # actually resolves to the Datadog Agent's Unix domain socket in this
    # cluster (mirrors how ddtrace-run auto-detects DD_TRACE_AGENT_URL).
    kwargs: dict = {"namespace": settings.datadog_metrics_namespace, **_STATSD_SENDER_KWARGS}
    if settings.datadog_statsd_host:
        kwargs["host"] = settings.datadog_statsd_host
        kwargs["port"] = settings.datadog_statsd_port or 8125

    return DogStatsd(**kwargs)


def init_statsd():
    """Construct the shared DogStatsd client if metrics are enabled.

    Blocking (client setup can do file and DNS I/O), so async code must run
    it off the event loop — the app does so once at startup (see main.py's
    lifespan). Idempotent."""
    global _statsd_client
    if not settings.datadog_metrics_enabled:
        return None
    with _statsd_init_lock:
        if _statsd_client is None and not _statsd_unavailable:
            _statsd_client = _build_statsd()
        return _statsd_client


def shutdown_statsd(timeout: float = _STATSD_STOP_TIMEOUT_SECONDS) -> None:
    """Flush and stop the background sender, waiting at most `timeout`
    seconds (never hangs shutdown on an unreachable Agent). Blocking; async
    code must run it off the event loop."""
    global _statsd_client
    with _statsd_init_lock:
        client, _statsd_client = _statsd_client, None
    stop = getattr(client, "stop", None)
    if stop is None:
        return
    try:
        stop(timeout=timeout)
    except Exception as exc:  # pragma: no cover - defensive; shutdown must not fail
        logger.warning("Datadog metrics client shutdown failed: %s", exc)


def _start_background_init() -> None:
    global _statsd_init_thread
    with _statsd_init_thread_lock:
        if _statsd_init_thread is not None and _statsd_init_thread.is_alive():
            return
        _statsd_init_thread = threading.Thread(target=init_statsd, name="statsd-init", daemon=True)
        _statsd_init_thread.start()


def _statsd():
    if not settings.datadog_metrics_enabled:
        return None
    client = _statsd_client
    if client is not None or _statsd_unavailable:
        return client
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Synchronous caller (CLI, scripts): build inline, as before.
        return init_statsd()
    # Inside the event loop and not initialized at startup (e.g. a script's
    # asyncio.run): never build the client on the loop thread. Build it in
    # the background and drop this metric.
    _start_background_init()
    return None


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


def gene_latency_tag(gene: str) -> str:
    """Bucket a gene into the fixed watchlist (or "other") for latency tags.

    Tagging by raw gene symbol would make gene.total_duration_ms a
    high-cardinality custom metric; bucketing keeps cardinality bounded to
    the watchlist size + 1 while still surfacing per-gene latency for the
    recurrent fusion partners operators care about most.
    """
    watchlist = {
        symbol.strip().upper()
        for symbol in settings.datadog_gene_latency_watchlist.split(",")
        if symbol.strip()
    }
    symbol = gene.strip().upper()
    return f"gene:{symbol}" if symbol in watchlist else "gene:other"


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


_current_user_context: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "_current_user_context", default=None
)


def set_user_context(
    user_id: Optional[str],
    email: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    domain: Optional[str] = None,
    auth_method: Optional[str] = None,
    api_key_id: Optional[str] = None,
) -> contextvars.Token:
    """Sets request-scoped user context for logging and observability."""
    ctx = {
        "user_id": user_id or "",
        "email": email or user_id or "",
        "name": name or "",
        "role": role or "",
        "domain": domain or "",
        "auth_method": auth_method or "",
        "api_key_id": api_key_id or "",
    }
    return _current_user_context.set(ctx)


def get_user_context() -> Dict[str, Any]:
    """Returns the current request-scoped user context, or an empty dict if none set."""
    ctx = _current_user_context.get()
    return ctx if ctx is not None else {}


def reset_user_context(token: contextvars.Token) -> None:
    """Resets the user context to its previous state."""
    _current_user_context.reset(token)


def tag_user(
    user_id: Optional[str],
    email: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
) -> None:
    """Tags active Datadog APM span with standard user metadata (usr.id, usr.email, usr.name, usr.role)."""
    if not user_id:
        return
    try:
        from ddtrace import tracer

        if hasattr(tracer, "set_user"):
            tracer.set_user(user_id=user_id, email=email or user_id, name=name or "", role=role or "")
        else:
            span = tracer.current_span()
            if span is not None:
                tags = {"usr.id": user_id, "usr.email": email or user_id}
                if name:
                    tags["usr.name"] = name
                if role:
                    tags["usr.role"] = role
                span.set_tags(tags)
    except Exception:  # pragma: no cover
        pass


def record_user_action(
    user_id: Optional[str],
    action: str,
    details: Optional[Dict[str, Any]] = None,
    tags: Optional[Iterable[str]] = None,
) -> None:
    """Emits a structured audit log and DogStatsD metrics for a user action.

    Structured log format:
        USER_ACTION: action=<action> user=<email> <details>
    This allows instant grouping, filtering, and leaderboard analytics in Datadog Log Explorer.
    """
    clean_user = user_id.strip() if user_id and user_id.strip() else "anonymous"
    extra_details = dict(details or {})

    detail_str = " ".join(f"{k}={v}" for k, v in extra_details.items())
    msg_suffix = f" {detail_str}" if detail_str else ""

    token = None
    if user_id and not get_user_context().get("user_id"):
        token = set_user_context(user_id=clean_user, email=clean_user)

    try:
        logger.info(
            "USER_ACTION: action=%s user=%s%s",
            action,
            clean_user,
            msg_suffix,
            extra={
                "user_action": action,
                "user_identity": clean_user,
                **{k: v for k, v in extra_details.items() if not k.startswith("usr.") and not k.startswith("dd.")},
            },
        )
    finally:
        if token is not None:
            reset_user_context(token)

    if user_id and user_id.strip():
        set_metric("users.active", stable_user_key(user_id), tags=tags)
        metric_tags = list(tags or []) + [f"action:{action}"]
        if getattr(settings, "datadog_tag_user_metrics", True):
            metric_tags.append(f"user:{user_id.strip().lower()}")
        increment("users.activity", tags=metric_tags)
    else:
        anon_tags = list(tags or []) + [f"action:{action}"]
        increment("users.anonymous_requests", tags=anon_tags)
