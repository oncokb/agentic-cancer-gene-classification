"""Single-flight of literature verdicts across concurrent jobs and pods, and
the process-wide literature concurrency bound."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from src.expression_context.catalog import StatsCatalog
from src.expression_context.lease import Claim, VerdictLease, lease_key
from src.expression_context.models import JobRecord
from src.expression_context.service import ExpressionContextService
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from src.expression_context.redis_client import ModuleRedis, build_client
from tests.conftest import destructive_tests_allowed
from tests.expression_context_helpers import (
    RELEASE_ID,
    DownRedis,
    FakeLiteratureGate,
    LeaseFakeRedis,
    make_service,
    module_redis,
    seeded_bundles,
)
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

    async def evaluate(self, hugo_symbol, tumor):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(self.delay)
            return await super().evaluate(hugo_symbol, tumor)
        finally:
            self.active -= 1


def _pod(gate, store, redis, *, concurrency=4, wait=5.0, lease_ttl=5.0):
    """One 'pod': its own service (in-flight map, semaphore) on shared store + Redis."""
    return make_service(
        gate=gate, store=store, redis=redis, concurrency=concurrency,
        lease_wait=wait, lease_ttl=lease_ttl,
    )


# ------------------------------------------------------------------ lease unit


KEY = lease_key("ERBB2", "BRCA", RELEASE_ID, "acgc_tumor_specific_v2")


async def test_lease_claim_hold_release_renew():
    redis = LeaseFakeRedis()
    lease = VerdictLease(ttl_seconds=10, redis=module_redis(redis))
    first = await lease.claim(KEY)
    assert first.outcome == "claimed" and first.token
    assert (await lease.claim(KEY)).outcome == "held"
    # Only the owner can renew or release.
    assert await lease.renew(KEY, "someone-else") is False
    assert await lease.release(KEY, "someone-else") is False
    assert redis._live(KEY).decode() == first.token
    redis.advance(5)
    assert await lease.renew(KEY, first.token) is True
    assert redis.expires_at(KEY) == redis.now + 10
    assert await lease.release(KEY, first.token) is True
    assert redis._live(KEY) is None
    # TTL bounds a dead holder.
    assert (await lease.claim(KEY)).outcome == "claimed"
    redis.advance(11)
    assert (await lease.claim(KEY)).outcome == "claimed"


async def _expire_and_reclaim(redis, new_lease, holder):
    """Another pod's move: the old lease expires and a new holder claims it."""
    redis.advance(2)
    holder.append(await new_lease.claim(KEY))


@pytest.mark.parametrize("operation", ["renew", "release"])
async def test_stale_holder_never_touches_new_holders_lease(operation):
    """Expiry + a new claim lands between the old holder's ownership check and
    its mutation: the new holder's lease must survive intact."""
    redis = LeaseFakeRedis()
    old = VerdictLease(ttl_seconds=1, redis=module_redis(redis))
    new = VerdictLease(ttl_seconds=10, redis=module_redis(redis))
    old_claim = await old.claim(KEY)
    new_claim = []
    redis.race = lambda: _expire_and_reclaim(redis, new, new_claim)

    result = await getattr(old, operation)(KEY, old_claim.token)

    assert result is False
    assert new_claim[0].outcome == "claimed"
    assert redis._live(KEY).decode() == new_claim[0].token
    assert redis.expires_at(KEY) == redis.now + 10  # not shortened to the old TTL either


def test_lease_scripts_are_compare_and_act():
    from src.expression_context import lease

    for script, action in ((lease.RENEW_SCRIPT, "PEXPIRE"), (lease.RELEASE_SCRIPT, "DEL")):
        assert 'redis.call("GET", KEYS[1]) == ARGV[1]' in script
        assert action in script


@pytest.fixture
async def real_redis():
    if not destructive_tests_allowed():
        pytest.skip("real Redis only in CI mode (ACGC_ALLOW_DESTRUCTIVE_TESTS=1)")
    module = ModuleRedis(factory=build_client)
    try:
        await module.client().ping()
    except Exception as exc:
        await module.aclose()  # don't leak the client (and its pool) on skip
        pytest.skip(f"Redis not reachable: {exc}")
    yield module
    try:
        await module.client().delete(KEY)
    finally:
        await module.aclose()


async def test_lease_scripts_on_real_redis(real_redis):
    old = VerdictLease(ttl_seconds=30, redis=real_redis)
    first = await old.claim(KEY)
    assert first.outcome == "claimed"
    assert await old.renew(KEY, "intruder") is False
    assert await old.release(KEY, "intruder") is False
    assert await old.renew(KEY, first.token) is True
    assert 0 < await real_redis.client().pttl(KEY) <= 30_000
    assert await old.release(KEY, first.token) is True
    assert await real_redis.client().get(KEY) is None


async def test_lease_fails_open_without_redis():
    lease = VerdictLease(ttl_seconds=1, redis=module_redis(DownRedis()))
    assert await lease.claim("k") == Claim("unavailable")
    assert await lease.renew("k", "t") is True
    assert await lease.release("k", "t") is False  # no raise


async def test_module_redis_backs_off_after_error():
    calls = []

    class Counting(DownRedis):
        async def set(self, *a, **k):
            calls.append(1)
            raise ConnectionError("down")

    now = [0.0]
    lease = VerdictLease(
        ttl_seconds=1, redis=ModuleRedis(factory=lambda: Counting(), clock=lambda: now[0])
    )
    assert (await lease.claim("k")).outcome == "unavailable"
    assert (await lease.claim("k")).outcome == "unavailable"  # skipped during back-off
    assert calls == [1]
    now[0] += 31
    assert (await lease.claim("k")).outcome == "unavailable"
    assert calls == [1, 1]


async def test_module_redis_is_lazy_and_closed():
    built, closed = [], []

    class Client:
        async def aclose(self):
            closed.append(1)

    module = ModuleRedis(factory=lambda: built.append(1) or Client())
    assert built == []
    module.client()
    module.client()
    assert built == [1]
    await module.aclose()
    await module.aclose()
    assert closed == [1]


# -------------------------------------------------------- in-process (one pod)


def test_concurrent_duplicate_jobs_call_gate_once_per_key():
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


async def test_concurrency_bound_is_shared_across_jobs():
    gate = SlowGate(delay=0.02)
    pod = _pod(gate, InMemoryExpressionContextStore(), LeaseFakeRedis(), concurrency=1)
    resolved = await pod.resolve("ERBB2")
    other = resolved.__class__(
        query="x",
        hugo_symbol="OTHER1",
        manifest=resolved.manifest,
        artifact=resolved.artifact,
        bundle=resolved.bundle,
    )
    await asyncio.gather(pod.evaluate(resolved, None), pod.evaluate(other, None))
    assert len(gate.calls) == 6  # distinct keys: no dedupe
    assert gate.max_active == 1  # but never more than one at a time process-wide


# ----------------------------------------------------------- cross-pod (Redis)


async def test_two_pods_share_one_evaluation_per_key():
    store, redis = InMemoryExpressionContextStore(), LeaseFakeRedis()
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

async def test_waiter_takes_over_when_holder_fails():
    store, redis = InMemoryExpressionContextStore(), LeaseFakeRedis()
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

async def test_waiter_falls_back_after_wait_timeout():
    redis = LeaseFakeRedis()
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


async def test_redis_down_still_dedupes_in_process():
    gate = SlowGate(delay=0.05)
    pod = _pod(gate, InMemoryExpressionContextStore(), DownRedis())
    resolved = await pod.resolve("ERBB2")
    results = await asyncio.gather(*(pod.evaluate(resolved, None) for _ in range(3)))
    assert sorted(q for _, q in gate.calls) == QUERIES
    assert sum(evaluated for _, evaluated, _, _ in results) == 3


async def test_lease_renewed_while_gate_runs():
    redis = LeaseFakeRedis()
    renewals = []
    lease = VerdictLease(ttl_seconds=0.15, redis=module_redis(redis))
    original = lease.renew

    async def renew(key, token):
        renewals.append(key)
        return await original(key, token)

    lease.renew = renew
    gate = SlowGate(delay=0.2, verdicts={"breast cancer": (False, ["1"])})
    pod = ExpressionContextService(
        catalog=StatsCatalog(seeded_bundles(), cache_seconds=0), gate=gate,
        store=InMemoryExpressionContextStore(),
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=lease, lease_poll_seconds=0.01,
    )
    resolved = await pod.resolve("ERBB2")
    await pod.evaluate(resolved, None)
    assert renewals  # heartbeat ran during the 0.2 s gate call


async def test_waiters_recover_when_single_flight_leader_is_cancelled():
    redis = LeaseFakeRedis()
    gate = SlowGate(delay=0.2)
    pod = _pod(gate, InMemoryExpressionContextStore(), redis)
    resolved = await pod.resolve("ERBB2")

    leader = asyncio.create_task(pod.evaluate(resolved, None))
    await asyncio.sleep(0.05)  # leader owns every in-flight key and lease
    waiters = [asyncio.create_task(pod.evaluate(resolved, None)) for _ in range(2)]
    await asyncio.sleep(0.05)  # waiters are parked on the leader's futures
    leader.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leader

    results = await asyncio.gather(*waiters)
    assert all(errors == [] for _, _, _, errors in results)
    assert all([p.oncotree_code for p in res.surfaced] == ["BRCA"] for res, _, _, _ in results)
    # The leader's 3 calls were cancelled mid-flight (SlowGate records only
    # completed calls); one waiter re-ran each key, the other reused it.
    assert gate.max_active >= 3
    assert sorted(q for _, q in gate.calls) == QUERIES
    assert sum(evaluated for _, evaluated, _, _ in results) == 3
    assert sum(reused for _, _, reused, _ in results) == 3
    assert pod._inflight == {}
    assert redis.keys_with_prefix("expression_context:lease:") == []


async def test_cancelled_waiter_does_not_take_over():
    gate = SlowGate(delay=0.2)
    pod = _pod(gate, InMemoryExpressionContextStore(), LeaseFakeRedis())
    resolved = await pod.resolve("ERBB2")
    leader = asyncio.create_task(pod.evaluate(resolved, None))
    await asyncio.sleep(0.05)
    waiter = asyncio.create_task(pod.evaluate(resolved, None))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    _, evaluated, _, errors = await leader
    assert evaluated == 3 and errors == []
    assert len(gate.calls) == 3


def _config(kwargs):
    """The configuration values only (drops per-instance objects such as the
    pool weakref and registries)."""
    return {k: v for k, v in kwargs.items() if isinstance(v, (str, int, float, bool, type(None)))}


def _core_client_kwargs(client):
    pool = client.connection_pool
    return pool.connection_class, _config(pool.connection_kwargs)


def test_module_redis_matches_core_cache_client_for_url(monkeypatch):
    from src.config import settings
    from src.pipeline import cache

    monkeypatch.setattr(settings, "redis_sentinel_enabled", False)
    monkeypatch.setattr(settings, "redis_url", "redis://cache.internal:6380/3")
    monkeypatch.setattr(cache, "_client", None)
    core = cache._get_client()  # comparison only; the module never calls this
    ours = build_client()
    assert _core_client_kwargs(ours) == _core_client_kwargs(core)
    monkeypatch.setattr(cache, "_client", None)


def test_module_redis_matches_core_cache_client_for_sentinel(monkeypatch):
    """Sentinel config parity: same sentinels, sentinel/connection kwargs,
    password and master set as the core cache client."""
    from src.config import settings
    from src.pipeline import cache

    monkeypatch.setattr(settings, "redis_sentinel_enabled", True)
    monkeypatch.setattr(settings, "redis_sentinel_hosts", "s-0:26379, s-1 ,s-2:26380")
    monkeypatch.setattr(settings, "redis_sentinel_master_set", "acgc-master")
    monkeypatch.setattr(settings, "redis_sentinel_password", "pw")
    monkeypatch.setattr(cache, "_client", None)
    core = cache._get_client()
    ours = build_client()
    monkeypatch.setattr(cache, "_client", None)

    core_pool, our_pool = core.connection_pool, ours.connection_pool
    assert our_pool.service_name == core_pool.service_name == "acgc-master"
    assert _config(our_pool.connection_kwargs) == _config(core_pool.connection_kwargs)
    assert _config(our_pool.connection_kwargs)["password"] == "pw"
    core_sentinel, our_sentinel = core_pool.sentinel_manager, our_pool.sentinel_manager
    assert _config(our_sentinel.sentinel_kwargs) == _config(core_sentinel.sentinel_kwargs)
    assert _config(our_sentinel.connection_kwargs) == _config(core_sentinel.connection_kwargs)
    hosts = lambda s: [  # noqa: E731
        (c.connection_pool.connection_kwargs["host"], c.connection_pool.connection_kwargs["port"])
        for c in s.sentinels
    ]
    assert hosts(our_sentinel) == hosts(core_sentinel) == [
        ("s-0", 26379), ("s-1", 26379), ("s-2", 26380),
    ]


def test_explicit_module_redis_url_overrides_app_settings(monkeypatch):
    from src.config import settings

    monkeypatch.setattr(settings, "redis_sentinel_enabled", True)
    monkeypatch.setattr(settings, "redis_sentinel_hosts", "s-0:26379")
    client = build_client("redis://ec.internal:6379/5")
    kwargs = client.connection_pool.connection_kwargs
    assert (kwargs["host"], kwargs["port"], kwargs["db"]) == ("ec.internal", 6379, 5)
