"""Single-flight of literature verdicts across concurrent jobs and pods, and
the process-wide literature concurrency bound."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from src.expression_context.artifact_client import ArtifactClient
from src.expression_context.lease import Claim, VerdictLease, lease_key
from src.expression_context.models import JobRecord
from src.expression_context.service import ExpressionContextService
from src.expression_context.settings import ExpressionContextSettings
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.conftest import FakeRedis
from tests.expression_context_helpers import BASE_URL, RELEASE_ID, FakeLiteratureGate, add_artifacts
from tests.test_expression_context_api import PREFIX, Harness, wait_for_job

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

QUERIES = ["breast cancer", "esophageal cancer", "gastric cancer"]


class SlowGate(FakeLiteratureGate):
    """Fake gate that takes a while and tracks how many calls overlap."""

    def __init__(self, delay: float = 0.05, **kwargs):
        super().__init__(**kwargs)
        self.delay = delay
        self.active = 0
        self.max_active = 0

    async def evaluate(self, hugo_symbol, tumor_type_query):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await super().evaluate(hugo_symbol, tumor_type_query)
        finally:
            self.active -= 1


def _settings():
    return ExpressionContextSettings(
        enabled=True, artifact_base_url=BASE_URL, http_retry_backoff_seconds=0
    )


def _pod(gate, store, redis, *, concurrency=4, wait=5.0, lease_ttl=5.0):
    """One 'pod': its own service (in-flight map, semaphore) on shared store + Redis."""
    return ExpressionContextService(
        artifacts=ArtifactClient(_settings()),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        literature_concurrency=concurrency,
        lease=VerdictLease(ttl_seconds=lease_ttl, client_factory=lambda: redis),
        lease_wait_seconds=wait,
        lease_poll_seconds=0.01,
    )


# ------------------------------------------------------------------ lease unit


async def test_lease_claim_hold_release_renew():
    redis = FakeRedis()
    lease = VerdictLease(ttl_seconds=10, client_factory=lambda: redis)
    key = lease_key("ERBB2", "BRCA", RELEASE_ID, "acgc_pipeline_v1")
    first = await lease.claim(key)
    assert first.outcome == "claimed" and first.token
    assert (await lease.claim(key)).outcome == "held"
    assert await lease.is_held(key)
    # Only the owner can renew or release.
    assert await lease.renew(key, "someone-else") is False
    await lease.release(key, "someone-else")
    assert await lease.is_held(key)
    assert await lease.renew(key, first.token) is True
    await lease.release(key, first.token)
    assert not await lease.is_held(key)
    # TTL bounds a dead holder.
    again = await lease.claim(key)
    redis.advance(11)
    assert (await lease.claim(key)).outcome == "claimed" and again.token


async def test_lease_fails_open_without_redis():
    class Down:
        async def set(self, *a, **k):
            raise ConnectionError("down")

        async def get(self, *a, **k):
            raise ConnectionError("down")

    lease = VerdictLease(ttl_seconds=1, client_factory=lambda: Down())
    assert await lease.claim("k") == Claim("unavailable")
    assert await lease.is_held("k") is False
    await lease.release("k", "t")  # no raise


# -------------------------------------------------------- in-process (one pod)


def test_concurrent_duplicate_jobs_call_gate_once_per_key(httpx_mock):
    add_artifacts(httpx_mock)
    harness = Harness(gate=SlowGate(delay=0.2))
    with TestClient(harness.app) as client:
        urls = [
            client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).json()["status_url"]
            for _ in range(3)
        ]
        jobs = [wait_for_job(client, status_url) for status_url in urls]

    assert all(job["status"] == "succeeded" for job in jobs)
    assert sorted(q for _, q in harness.gate.calls) == QUERIES  # once per verdict key
    assert sum(job["literature_evaluated"] for job in jobs) == 3
    assert sum(job["literature_reused"] for job in jobs) == 6
    surfaced = {tuple(p["oncotree_code"] for p in job["result"]["surfaced"]) for job in jobs}
    assert surfaced == {("BRCA",)}


async def test_concurrency_bound_is_shared_across_jobs(httpx_mock):
    add_artifacts(httpx_mock)
    gate = SlowGate(delay=0.02)
    pod = _pod(gate, InMemoryExpressionContextStore(), FakeRedis(), concurrency=1)
    resolved = await pod.resolve("ERBB2")
    other = resolved.__class__(
        query="x", hugo_symbol="OTHER1", manifest=resolved.manifest, artifact=resolved.artifact
    )
    await asyncio.gather(pod.evaluate(resolved, None), pod.evaluate(other, None))
    assert len(gate.calls) == 6  # distinct keys: no dedupe
    assert gate.max_active == 1  # but never more than one at a time process-wide
    await pod.artifacts.aclose()


# ----------------------------------------------------------- cross-pod (Redis)


async def test_two_pods_share_one_evaluation_per_key(httpx_mock):
    add_artifacts(httpx_mock)
    store, redis = InMemoryExpressionContextStore(), FakeRedis()
    gate_a, gate_b = SlowGate(delay=0.1), SlowGate(delay=0.1)
    pod_a, pod_b = _pod(gate_a, store, redis), _pod(gate_b, store, redis)
    resolved_a, resolved_b = await pod_a.resolve("ERBB2"), await pod_b.resolve("ERBB2")

    (res_a, ev_a, re_a, err_a), (res_b, ev_b, re_b, err_b) = await asyncio.gather(
        pod_a.evaluate(resolved_a, "a@x"), pod_b.evaluate(resolved_b, "b@x")
    )
    calls = sorted(q for _, q in gate_a.calls + gate_b.calls)
    assert calls == QUERIES
    assert ev_a + ev_b == 3 and re_a + re_b == 3
    assert err_a == err_b == []
    assert [p.oncotree_code for p in res_a.surfaced] == [p.oncotree_code for p in res_b.surfaced]
    assert redis.keys_with_prefix("expression_context:lease:") == []  # all released
    for pod in (pod_a, pod_b):
        await pod.artifacts.aclose()


async def test_waiter_takes_over_when_holder_fails(httpx_mock):
    add_artifacts(httpx_mock)
    store, redis = InMemoryExpressionContextStore(), FakeRedis()
    failing = SlowGate(delay=0.05)
    failing.fail_for = set(QUERIES)
    healthy = SlowGate(delay=0.0)
    pod_a, pod_b = _pod(failing, store, redis), _pod(healthy, store, redis)
    resolved = await pod_a.resolve("ERBB2")

    async def late_b():
        await asyncio.sleep(0.01)  # A claims every lease first
        return await pod_b.evaluate(resolved, None)

    (_, _, _, errors_a), (res_b, ev_b, _, errors_b) = await asyncio.gather(
        pod_a.evaluate(resolved, None), late_b()
    )
    assert len(errors_a) == 3
    assert errors_b == [] and ev_b == 3
    assert sorted(q for _, q in healthy.calls) == QUERIES
    assert [p.oncotree_code for p in res_b.surfaced] == ["BRCA"]
    for pod in (pod_a, pod_b):
        await pod.artifacts.aclose()


async def test_waiter_falls_back_after_wait_timeout(httpx_mock):
    add_artifacts(httpx_mock)
    redis = FakeRedis()
    gate = SlowGate(delay=0.0)
    pod = _pod(gate, InMemoryExpressionContextStore(), redis, wait=0.05)
    resolved = await pod.resolve("ERBB2")
    # A wedged holder elsewhere owns every lease (and never stores a verdict).
    for code in ("BRCA", "STAD", "ESCA"):
        await redis.set(lease_key("ERBB2", code, RELEASE_ID, gate.name), "other-pod", px=60_000)

    started = time.monotonic()
    _, evaluated, _, errors = await pod.evaluate(resolved, None)
    assert evaluated == 3 and errors == []
    assert time.monotonic() - started >= 0.05
    # The foreign leases were not touched.
    assert len(redis.keys_with_prefix("expression_context:lease:")) == 3
    await pod.artifacts.aclose()


async def test_redis_down_still_dedupes_in_process(httpx_mock):
    add_artifacts(httpx_mock)

    class Down:
        async def set(self, *a, **k):
            raise ConnectionError("down")

        async def get(self, *a, **k):
            raise ConnectionError("down")

    gate = SlowGate(delay=0.05)
    pod = _pod(gate, InMemoryExpressionContextStore(), Down())
    resolved = await pod.resolve("ERBB2")
    results = await asyncio.gather(*(pod.evaluate(resolved, None) for _ in range(3)))
    assert sorted(q for _, q in gate.calls) == QUERIES
    assert sum(evaluated for _, evaluated, _, _ in results) == 3
    await pod.artifacts.aclose()


async def test_lease_renewed_while_gate_runs(httpx_mock):
    add_artifacts(httpx_mock)
    redis = FakeRedis()
    renewals = []
    lease = VerdictLease(ttl_seconds=0.15, client_factory=lambda: redis)
    original = lease.renew

    async def renew(key, token):
        renewals.append(key)
        return await original(key, token)

    lease.renew = renew
    gate = SlowGate(delay=0.2, verdicts={"breast cancer": (False, ["1"])})
    pod = ExpressionContextService(
        artifacts=ArtifactClient(_settings()), gate=gate,
        store=InMemoryExpressionContextStore(),
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=lease, lease_poll_seconds=0.01,
    )
    resolved = await pod.resolve("ERBB2")
    await pod.evaluate(resolved, None)
    assert renewals  # heartbeat ran during the 0.2 s gate call
    await pod.artifacts.aclose()
