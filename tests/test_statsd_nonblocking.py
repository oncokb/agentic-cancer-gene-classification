"""The shared DogStatsd client must never block the event loop: emitting a
metric only enqueues it (DogStatsd's background sender, bounded queue, drop
on full), and building the client (file/DNS I/O) never happens on the loop
thread. Covers every metric that shares the client, not just OpenEvidence's.
"""

from __future__ import annotations

import asyncio
import sys
import threading

import pytest
from fastapi import Response

from src import main, observability


class SlowSocket:
    """A transport held in flight until the test releases it."""

    def __init__(self) -> None:
        self.sent: list = []
        self.entered = threading.Event()
        self.release = threading.Event()
        self.loop_thread = threading.current_thread()
        self.expired = threading.Event()

    def send(self, data: bytes) -> int:
        assert threading.current_thread() is not self.loop_thread
        self.entered.set()
        if not self.release.wait(30):
            self.expired.set()
            raise AssertionError("transport was never released")
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


async def test_blocked_transport_does_not_block_endpoint_or_event_loop(metrics_on):
    # Built the way the app builds it (off the loop), then given a slow transport.
    client = await asyncio.to_thread(observability._statsd)
    slow = SlowSocket()
    client.socket = slow

    try:
        await _disabled_endpoint()
        assert await asyncio.to_thread(slow.entered.wait, 30)
        for _ in range(3):
            await _disabled_endpoint()
        # The loop and endpoint progressed while the sender was blocked.
        assert not slow.release.is_set()
        assert not slow.expired.is_set()
    finally:
        slow.release.set()
    assert await asyncio.to_thread(client.wait_for_pending, 30.0)
    assert any(b"openevidence.endpoint.requests" in packet for packet in slow.sent)


async def test_client_construction_never_runs_on_the_event_loop(metrics_on, monkeypatch):
    built_on: list = []
    building = threading.Event()
    release = threading.Event()

    class SlowDogStatsd:
        def __init__(self, **kwargs):
            built_on.append(threading.current_thread())
            assert threading.current_thread() is not threading.main_thread()
            building.set()
            assert release.wait(30), "construction was never released"
            self.calls: list = []

        def increment(self, metric, value=1, tags=None):
            self.calls.append(metric)

        def distribution(self, metric, value, tags=None):
            self.calls.append(metric)

    monkeypatch.setitem(sys.modules, "datadog", type("_module", (), {"DogStatsd": SlowDogStatsd}))

    try:
        await _disabled_endpoint()
        assert await asyncio.to_thread(building.wait, 30)
        assert observability._statsd_client is None
        # Construction is in progress and a second endpoint still returns.
        await _disabled_endpoint()
    finally:
        release.set()
    await _wait_for_state(observability._READY)
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

    try:
        assert await asyncio.to_thread(client.socket.entered.wait, 30)
        await asyncio.to_thread(observability.shutdown_statsd, 0.2)
        assert not client.socket.release.is_set()
        assert not client.socket.expired.is_set()
    finally:
        client.socket.release.set()
    assert await asyncio.to_thread(client.wait_for_pending, 30.0)
    assert observability._statsd_client is None


# --- Client lifecycle: startup failure, bounded shutdown, no re-init --------


class CountingDogStatsd:
    """Records client constructions and stops."""

    constructed: list = []

    def __init__(self, **kwargs):
        type(self).constructed.append(self)
        self.calls: list = []
        self.stopped = False

    def increment(self, metric, value=1, tags=None):
        self.calls.append(metric)

    def distribution(self, metric, value, tags=None):
        self.calls.append(metric)

    def stop(self, timeout=None):
        self.stopped = True
        return True


@pytest.fixture
def counting_dogstatsd(monkeypatch, metrics_on):
    class Fresh(CountingDogStatsd):
        constructed: list = []

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
    for _ in range(3000):
        if observability._statsd_state == observability._UNAVAILABLE:
            break
        await asyncio.sleep(0.01)
    for _ in range(20):
        observability.increment("llm.requests")
    await asyncio.to_thread(observability.increment, "llm.requests")  # sync caller too

    assert len(attempts) == 1
    assert observability._statsd_state == observability._UNAVAILABLE


class _LockHolder:
    """Holds the lifecycle lock on another thread until release()."""

    def __init__(self) -> None:
        self._held = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        with observability._statsd_lock:
            self._held.set()
            assert self._release.wait(30)

    def __enter__(self) -> "_LockHolder":
        self._thread.start()
        assert self._held.wait(30), "the lifecycle lock was never taken"
        return self

    def release(self) -> None:
        self._release.set()
        self._thread.join(30)

    def __exit__(self, *exc) -> None:
        self.release()


async def _wait_for_state(state: str) -> None:
    for _ in range(3000):
        if observability._statsd_state == state:
            return
        await asyncio.sleep(0.01)


async def test_shutdown_is_bounded_and_final_even_when_the_lifecycle_lock_is_held(counting_dogstatsd):
    with _LockHolder() as holder:
        await asyncio.to_thread(observability.shutdown_statsd, 0.05)
        assert not holder._release.is_set()
        assert holder._thread.is_alive()
        holder.release()

        # The deferred cleanup finishes the transition once the lock is free.
        await _wait_for_state(observability._CLOSED)
        assert observability._statsd_state == observability._CLOSED

        # Shutdown stays final: no construction, nothing emitted, from the
        # loop or from a synchronous caller.
        observability.increment("llm.requests")
        await asyncio.to_thread(observability.increment, "llm.requests")
        assert await asyncio.to_thread(observability.init_statsd) is None

    assert counting_dogstatsd.constructed == []
    assert observability._statsd_client is None


async def test_timed_out_shutdown_still_stops_and_unpublishes_a_ready_client(counting_dogstatsd):
    client = await asyncio.to_thread(observability.init_statsd)
    assert observability._statsd_state == observability._READY

    with _LockHolder() as holder:
        await asyncio.to_thread(observability.shutdown_statsd, 0.05)
        observability.increment("llm.requests")  # after shutdown returned
        assert client.calls == []  # latched: not delivered even while still published
        holder.release()
        await _wait_for_state(observability._CLOSED)
        for _ in range(3000):
            if client.stopped:
                break
            await asyncio.sleep(0.01)

    assert observability._statsd_state == observability._CLOSED
    assert observability._statsd_client is None
    assert client.stopped
    assert len(counting_dogstatsd.constructed) == 1
    observability.increment("llm.requests")
    assert client.calls == []


async def test_initializer_finishing_after_shutdown_never_publishes(counting_dogstatsd, monkeypatch):
    building = threading.Event()
    finish = threading.Event()
    original_init = counting_dogstatsd.__init__

    def slow_init(self, **kwargs):
        building.set()
        assert finish.wait(30)  # e.g. slow hostname resolution
        original_init(self, **kwargs)

    monkeypatch.setattr(counting_dogstatsd, "__init__", slow_init)
    initializer = threading.Thread(target=observability.init_statsd, daemon=True)
    initializer.start()
    assert await asyncio.to_thread(building.wait, 30)

    try:
        await asyncio.to_thread(observability.shutdown_statsd, 0.05)
        assert not finish.is_set()
    finally:
        finish.set()
        await asyncio.to_thread(initializer.join, 30)
    assert not initializer.is_alive()
    assert observability._statsd_client is None
    assert observability._statsd_state == observability._CLOSED
    (late,) = counting_dogstatsd.constructed
    assert late.stopped  # stopped and discarded, not published


async def test_emission_during_stop_does_not_start_a_replacement_client(counting_dogstatsd, monkeypatch):
    stopping = threading.Event()
    release = threading.Event()

    def controlled_stop(self, timeout=None):
        stopping.set()
        assert release.wait(30)
        self.stopped = True

    monkeypatch.setattr(counting_dogstatsd, "stop", controlled_stop)
    client = await asyncio.to_thread(observability.init_statsd)
    assert client is not None

    shutdown = asyncio.create_task(asyncio.to_thread(observability.shutdown_statsd, 1.0))
    try:
        assert await asyncio.to_thread(stopping.wait, 30)
        observability.increment("llm.requests")  # loop caller
        await asyncio.to_thread(observability.increment, "llm.requests")  # sync caller
    finally:
        release.set()
        await shutdown

    assert len(counting_dogstatsd.constructed) == 1
    assert observability._statsd_client is None
    assert observability._statsd() is None
    assert client.stopped


async def test_stop_without_timeout_support_stays_bounded(metrics_on, caplog):
    stop_called = threading.Event()
    release = threading.Event()
    stopped = threading.Event()

    class OldDogStatsd:
        def stop(self):  # datadog < 0.55: no timeout parameter
            stop_called.set()
            assert release.wait(30)
            stopped.set()

    observability._statsd_client = OldDogStatsd()
    observability._statsd_state = observability._READY

    try:
        await asyncio.to_thread(observability.shutdown_statsd, 0.1)
        assert await asyncio.to_thread(stop_called.wait, 30)
        assert not release.is_set()
    finally:
        release.set()
        assert await asyncio.to_thread(stopped.wait, 30)
    assert "takes no timeout" in caplog.text
