"""The shared DogStatsd client must never block the event loop: emitting a
metric only enqueues it (DogStatsd's background sender, bounded queue, drop
on full), and building the client (file/DNS I/O) never happens on the loop
thread. Covers every metric that shares the client, not just OpenEvidence's.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest
from fastapi import Response

from src import main, observability

_SLOW_SECONDS = 0.15


class SlowSocket:
    """A transport whose every send takes _SLOW_SECONDS (a congested or
    unreachable Agent)."""

    def __init__(self) -> None:
        self.sent: list = []

    def send(self, data: bytes) -> int:
        time.sleep(_SLOW_SECONDS)
        self.sent.append(data)
        return len(data)

    sendall = send

    def close(self) -> None:
        return None


@pytest.fixture
def metrics_on(monkeypatch):
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability.settings, "datadog_statsd_host", "127.0.0.1")
    monkeypatch.setattr(observability.settings, "datadog_statsd_port", 8125)
    monkeypatch.setattr(observability, "_statsd_client", None)
    monkeypatch.setattr(main.settings, "openevidence_enabled", False)
    yield
    client = observability._statsd_client
    if client is not None and hasattr(client, "stop"):
        client.stop(timeout=0)
    observability._statsd_client = None


async def _disabled_endpoint() -> None:
    response = Response()
    await main.get_gene_openevidence("ALK", response=response, core_pmids=[], core_titles=[])


async def test_slow_transport_does_not_delay_the_endpoint_or_other_loop_timers(metrics_on):
    # Built the way the app builds it (off the loop), then given a slow transport.
    client = await asyncio.to_thread(observability._statsd)
    slow = SlowSocket()
    client.socket = slow

    timer_lateness: list = []

    async def independent_timer() -> None:
        for _ in range(5):
            due = time.monotonic() + 0.01
            await asyncio.sleep(0.01)
            timer_lateness.append(time.monotonic() - due)

    timer = asyncio.create_task(independent_timer())
    start = time.monotonic()
    for _ in range(3):  # 2 metrics each: 6 slow sends if they were synchronous
        await _disabled_endpoint()
    elapsed = time.monotonic() - start
    await timer

    assert elapsed < _SLOW_SECONDS / 2
    assert max(timer_lateness) < _SLOW_SECONDS / 2
    # Still delivered, just off the loop thread.
    assert await asyncio.to_thread(client.wait_for_pending, 5.0)
    assert any(b"openevidence.endpoint.requests" in packet for packet in slow.sent)


async def test_client_construction_never_runs_on_the_event_loop(metrics_on, monkeypatch):
    built_on: list = []

    class SlowDogStatsd:
        def __init__(self, **kwargs):
            built_on.append(threading.current_thread())
            time.sleep(_SLOW_SECONDS * 2)  # e.g. hostname resolution
            self.calls: list = []

        def increment(self, metric, value=1, tags=None):
            self.calls.append(metric)

        def distribution(self, metric, value, tags=None):
            self.calls.append(metric)

    monkeypatch.setitem(sys.modules, "datadog", type("_module", (), {"DogStatsd": SlowDogStatsd}))

    start = time.monotonic()
    await _disabled_endpoint()
    assert time.monotonic() - start < _SLOW_SECONDS

    for _ in range(100):  # built in the background meanwhile
        if observability._statsd_client is not None:
            break
        await asyncio.sleep(0.01)
    assert built_on and built_on[0] is not threading.main_thread()
    await _disabled_endpoint()
    assert "openevidence.endpoint.requests" in observability._statsd_client.calls


async def test_shared_client_uses_a_bounded_drop_on_full_background_sender(metrics_on):
    client = await asyncio.to_thread(observability._statsd)

    assert client._sender_enabled
    assert client._sender_queue_size > 0
    assert client._sender_queue_timeout == 0  # drop, never block, when full


async def test_shutdown_stops_the_sender_without_hanging(metrics_on):
    client = await asyncio.to_thread(observability.init_statsd)
    client.socket = SlowSocket()
    for _ in range(50):
        observability.increment("llm.requests")

    start = time.monotonic()
    await asyncio.to_thread(observability.shutdown_statsd, 0.2)

    assert time.monotonic() - start < 1.0
    assert observability._statsd_client is None


# --- Client lifecycle: startup failure, bounded shutdown, no re-init --------


class CountingDogStatsd:
    """Records constructions; stop() optionally slow."""

    constructed: list = []
    stop_seconds = 0.0

    def __init__(self, **kwargs):
        type(self).constructed.append(self)
        self.calls: list = []
        self.stopped = False

    def increment(self, metric, value=1, tags=None):
        self.calls.append(metric)

    def distribution(self, metric, value, tags=None):
        self.calls.append(metric)

    def stop(self, timeout=None):
        time.sleep(min(self.stop_seconds, timeout if timeout is not None else self.stop_seconds))
        self.stopped = True
        return True


@pytest.fixture
def counting_dogstatsd(monkeypatch, metrics_on):
    class Fresh(CountingDogStatsd):
        constructed: list = []
        stop_seconds = 0.0

    monkeypatch.setitem(sys.modules, "datadog", type("_module", (), {"DogStatsd": Fresh}))
    return Fresh


class _FakeRunStore:
    @classmethod
    async def create(cls):
        return cls()

    async def close(self):
        return None


def _purge_datadog_modules(monkeypatch) -> None:
    for name in [name for name in sys.modules if name == "datadog" or name.startswith("datadog.")]:
        monkeypatch.delitem(sys.modules, name)


async def test_bad_dd_config_does_not_break_startup_and_disables_metrics(metrics_on, monkeypatch):
    # The real library raises ValueError while importing/constructing.
    monkeypatch.setenv("DD_TELEMETRY_HOST", "localhost")
    monkeypatch.setenv("DD_TELEMETRY_PORT", "not-a-port")
    _purge_datadog_modules(monkeypatch)
    monkeypatch.setattr(main, "RunStore", _FakeRunStore)

    async with main.lifespan(main.app):
        assert observability._statsd_state == observability._UNAVAILABLE
        response = Response()
        result = await main.get_gene_openevidence("ALK", response=response, core_pmids=[], core_titles=[])
        assert result.body == b'{"available":false,"distilled":null,"error":null}'
        assert observability._statsd() is None


async def test_failed_lazy_construction_is_not_retried_on_every_emission(metrics_on, monkeypatch):
    attempts: list = []

    class BrokenDogStatsd:
        def __init__(self, **kwargs):
            attempts.append(threading.current_thread())
            raise ValueError("invalid literal for int() with base 10: 'not-a-port'")

    monkeypatch.setitem(sys.modules, "datadog", type("_module", (), {"DogStatsd": BrokenDogStatsd}))

    observability.increment("llm.requests")  # inside the loop: background init
    for _ in range(100):
        if observability._statsd_state == observability._UNAVAILABLE:
            break
        await asyncio.sleep(0.01)
    for _ in range(20):
        observability.increment("llm.requests")
    await asyncio.to_thread(observability.increment, "llm.requests")  # sync caller too
    await asyncio.sleep(0.05)

    assert len(attempts) == 1
    assert observability._statsd_state == observability._UNAVAILABLE


async def test_shutdown_is_bounded_even_when_the_lifecycle_lock_is_held(metrics_on):
    held = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with observability._statsd_lock:
            held.set()
            release.wait(5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert held.wait(1), "the lifecycle lock was never taken"
    try:
        start = time.monotonic()
        await asyncio.to_thread(observability.shutdown_statsd, 0.05)
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        holder.join(1)


async def test_initializer_finishing_after_shutdown_never_publishes(counting_dogstatsd, monkeypatch):
    building = threading.Event()
    finish = threading.Event()
    original_init = counting_dogstatsd.__init__

    def slow_init(self, **kwargs):
        building.set()
        finish.wait(5)  # e.g. slow hostname resolution
        original_init(self, **kwargs)

    monkeypatch.setattr(counting_dogstatsd, "__init__", slow_init)
    initializer = threading.Thread(target=observability.init_statsd, daemon=True)
    initializer.start()
    building.wait(1)

    start = time.monotonic()
    await asyncio.to_thread(observability.shutdown_statsd, 0.05)
    assert time.monotonic() - start < 0.5  # never waits on the in-flight construction

    finish.set()
    initializer.join(2)
    assert observability._statsd_client is None
    assert observability._statsd_state == observability._CLOSED
    (late,) = counting_dogstatsd.constructed
    assert late.stopped  # stopped and discarded, not published


async def test_emission_during_stop_does_not_start_a_replacement_client(counting_dogstatsd):
    counting_dogstatsd.stop_seconds = 0.2
    client = await asyncio.to_thread(observability.init_statsd)
    assert client is not None

    shutdown = asyncio.create_task(asyncio.to_thread(observability.shutdown_statsd, 1.0))
    await asyncio.sleep(0.05)  # stop() is now in progress
    observability.increment("llm.requests")  # loop caller
    await asyncio.to_thread(observability.increment, "llm.requests")  # sync caller
    await shutdown
    await asyncio.sleep(0.05)

    assert len(counting_dogstatsd.constructed) == 1
    assert observability._statsd_client is None
    assert observability._statsd() is None
    assert client.stopped


async def test_stop_without_timeout_support_stays_bounded(metrics_on, caplog):
    stop_called = threading.Event()

    class OldDogStatsd:
        def stop(self):  # datadog < 0.55: no timeout parameter
            stop_called.set()
            time.sleep(1.0)

    observability._statsd_client = OldDogStatsd()
    observability._statsd_state = observability._READY

    start = time.monotonic()
    await asyncio.to_thread(observability.shutdown_statsd, 0.1)

    assert time.monotonic() - start < 0.5
    assert stop_called.wait(1)
    assert "takes no timeout" in caplog.text
