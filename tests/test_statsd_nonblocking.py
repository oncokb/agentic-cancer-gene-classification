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
