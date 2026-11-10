"""Fencing of bundle writes by the single-loader lease (re-review of 4a5a347).

1. Every mutating bundle-store operation carries the loader token and checks
   ownership atomically with the write: a loader whose lease expired can't
   insert into, reset, clean up, finish, activate or prune anything.
2. Pruning reads the CURRENT active/previous pointers inside its own
   transaction, so a re-activated bundle can't be deleted.
3. Retirement starts on the database clock at the pointer swap, and the
   grace period is measured on the database clock.
4. A refresh is never due earlier than the cadence the API budget assumes.

Each scenario runs on the in-memory store and on real MySQL (CI mode).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.expression_context.bundle_store import (
    BUNDLE_TABLES,
    BundleRecord,
    BundleState,
    InMemoryBundleStore,
    LoaderLockNotHeld,
    MySQLBundleStore,
)
from src.expression_context.store import MySQLExpressionContextStore
from tests import test_expression_context_bundle as bundle_tests
from tests.test_expression_context_bundle import BUNDLE_2, BUNDLE_3, INTERVAL, Clock
from tests.expression_context_helpers import (
    add_assets,
    add_bundle_release,
    add_release_list,
    build_bundle,
    release_json,
)

pytestmark = pytest.mark.no_real_redis

pods = bundle_tests.pods  # shared fixture

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)


def _record(tag: str) -> BundleRecord:
    return BundleRecord(
        bundle_tag=tag, release_id="datahub-aaaaaaaaaaaa-m2-bbbbbbbbbbbb", schema_version="1",
        method_version="2", pubmed_snapshot_date=date(2026, 11, 2), gene_count=2,
        sha256="a" * 64, size_bytes=10, manifest={"schema_version": "1"}, status="loading",
        load_started_at=T0,
    )


class InMemoryBackend:
    """In-memory store whose lease and 'database' clocks the test controls."""

    def __init__(self):
        self.lock_now = [0.0]
        self.db_now = [T0]
        self.store = InMemoryBundleStore(
            lock_clock=lambda: self.lock_now[0], db_clock=lambda: self.db_now[0]
        )

    async def expire_leases(self, ttl):
        self.lock_now[0] += ttl + 1

    async def pass_time(self, seconds):
        self.db_now[0] += timedelta(seconds=seconds)


class MySQLBackend:
    def __init__(self, store, results):
        self.store = store
        self.results = results

    async def expire_leases(self, ttl):
        await asyncio.sleep(ttl + 0.15)

    async def pass_time(self, seconds):
        await asyncio.sleep(seconds)


@pytest.fixture(params=["memory", "mysql"])
async def backend(request):
    if request.param == "memory":
        yield InMemoryBackend()
        return
    try:
        results = await MySQLExpressionContextStore.create(maxsize=4)
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")

    async def drop():
        async with results.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for table in BUNDLE_TABLES:
                    await cursor.execute(f"DROP TABLE IF EXISTS {table}")

    await drop()
    store = MySQLBundleStore(results.pool)
    await store.ensure_schema()
    yield MySQLBackend(store, results)
    await drop()
    await results.close()


TTL = 0.25


async def _load(store, tag, token, genes):
    await store.begin_load(_record(tag), token)
    await store.add_genes(tag, genes, token)
    await store.add_aliases(tag, [("HER2", "ERBB2")], token)
    await store.finish_load(tag, len(genes), T0, token)


# ------------------------------------------------- 1. writes fenced by lease


async def test_lease_lost_mid_insert_cannot_touch_the_new_owners_rows(backend):
    store, tag = backend.store, "tag-x"
    assert await store.acquire_loader_lock("A", TTL)
    await store.begin_load(_record(tag), "A")
    await store.add_genes(tag, [("ERBB2", 2064, b'{"by": "A"}')], "A")

    await backend.expire_leases(TTL)  # A stalls; its lease runs out
    assert await store.acquire_loader_lock("B", 60)
    await store.begin_load(_record(tag), "B")  # B resets the tag and imports it
    await store.add_genes(tag, [("ERBB2", 2064, b'{"by": "B"}'), ("MYCN", 4613, b"{}")], "B")

    # A wakes up mid-import: every write it attempts is refused atomically.
    for attempt in (
        store.add_genes(tag, [("TP53", 7157, b'{"by": "A"}')], "A"),
        store.add_aliases(tag, [("P53", "TP53")], "A"),
        store.begin_load(_record(tag), "A"),
        store.fail_load(tag, "A gives up", T0, "A"),
        store.finish_load(tag, 1, T0, "A"),
        store.prune("A", 0),
    ):
        with pytest.raises(LoaderLockNotHeld):
            await attempt
    assert not await store.activate(tag, T0, "A")

    assert await store.get_gene_doc(tag, "ERBB2") == b'{"by": "B"}'
    assert await store.get_gene_doc(tag, "MYCN") == b"{}"
    assert await store.get_gene_doc(tag, "TP53") is None
    assert (await store.get_bundle(tag)).status == "loading"
    await store.add_aliases(tag, [("HER2", "ERBB2")], "B")
    await store.finish_load(tag, 2, T0, "B")
    assert await store.activate(tag, T0, "B")
    assert (await store.get_bundle(tag)).loaded_rows == 2


async def test_unknown_or_missing_token_is_refused(backend):
    store = backend.store
    with pytest.raises(LoaderLockNotHeld):
        await store.begin_load(_record("t"), "nobody")
    assert await store.get_bundle("t") is None


# ------------------------------------- 2. prune reads the current pointers


async def test_reactivated_bundle_is_never_pruned(backend):
    store = backend.store
    assert await store.acquire_loader_lock("L", 60)
    for tag in ("t1", "t2", "t3"):
        await _load(store, tag, "L", [("ERBB2", 2064, b"{}")])
        assert await store.activate(tag, T0, "L")
    # t1 was retired by the t3 swap; long after, t1 is re-activated (rollback).
    await backend.pass_time(0.3)
    assert await store.activate("t1", T0, "L")
    state = await store.get_state()
    assert (state.active_bundle_tag, state.previous_bundle_tag) == ("t1", "t3")
    assert (await store.get_bundle("t1")).retired_at is None  # cleared on re-activation
    assert (await store.get_bundle("t2")).retired_at is not None  # t2 left the pair now
    removed = await store.prune("L", 0.2)
    assert "t1" not in removed and "t3" not in removed
    assert await store.get_gene_doc("t1", "ERBB2") == b"{}"


# ------------------------------- 3. grace on the database clock, from the swap


async def test_retirement_starts_at_the_swap_and_grace_uses_the_db_clock(backend):
    store = backend.store
    assert await store.acquire_loader_lock("L", 60)
    for tag in ("t1", "t2"):
        await _load(store, tag, "L", [("ERBB2", 2064, b"{}")])
        assert await store.activate(tag, T0, "L")
    await _load(store, "t3", "L", [("ERBB2", 2064, b"{}")])
    pod_clock_far_in_the_past = T0 - timedelta(days=30)  # pods' clocks are irrelevant
    assert await store.activate("t3", pod_clock_far_in_the_past, "L")
    assert (await store.get_bundle("t1")).retired_at is not None
    assert await store.prune("L", 0.4) == []  # retired just now on the DB clock
    await backend.pass_time(0.5)
    assert await store.prune("L", 0.4) == ["t1"]
    assert await store.get_gene_doc("t1", "ERBB2") is None


async def test_refresh_retires_on_the_db_clock_not_the_refresh_start(httpx_mock, pods):
    """The pod's clock (refresh start) lags the database by 3 h: retired_at must
    be the database's time at the swap."""
    pod_clock = Clock()
    db_now = [T0 + timedelta(hours=3)]
    bundles = InMemoryBundleStore(db_clock=lambda: db_now[0])
    first = build_bundle()
    add_bundle_release(httpx_mock, first, reusable=False)
    pod = pods(bundles, clock=pod_clock)
    assert await pod.refresh() == "loaded"
    tags = [first.tag]
    for pubmed, published in ((BUNDLE_2, "2026-11-09T06:00:00Z"), (BUNDLE_3, "2026-11-16T06:00:00Z")):
        nxt = build_bundle(pubmed=pubmed, published_at=published)
        tags.append(nxt.tag)
        pod_clock.advance(INTERVAL)
        db_now[0] += INTERVAL
        add_release_list(httpx_mock, [release_json(nxt)], reusable=False)
        add_assets(httpx_mock, nxt, reusable=False)
        assert await pod.refresh() == "loaded"
    assert bundles.bundles[tags[0]].retired_at == db_now[0]  # the swap, on the DB clock

    grace = pod.settings.bundle_prune_grace
    pod_clock.advance(INTERVAL)  # the pod thinks plenty of time has passed...
    db_now[0] += grace - timedelta(minutes=1)  # ...but the database says otherwise
    add_release_list(httpx_mock, [release_json(build_bundle(pubmed=BUNDLE_3, published_at="2026-11-16T06:00:00Z"))], reusable=False)
    assert await pod.refresh() == "up_to_date"
    assert tags[0] in bundles.bundles
    pod_clock.advance(INTERVAL)
    db_now[0] += timedelta(minutes=2)
    add_release_list(httpx_mock, [release_json(build_bundle(pubmed=BUNDLE_3, published_at="2026-11-16T06:00:00Z"))], reusable=False)
    assert await pod.refresh() == "up_to_date"
    assert tags[0] not in bundles.bundles


# ------------------------------------------------- 4. cadence matches budget


async def test_refresh_is_never_due_before_the_budgeted_cadence(pods, httpx_mock):
    clock = Clock()
    bundles = InMemoryBundleStore()
    pod = pods(bundles, clock=clock)
    interval = pod.settings.refresh_interval
    bundles.state = BundleState(
        active_bundle_tag="tag", last_checked_at=clock.now - interval * 0.96
    )
    assert await pod.refresh() == "not_due"
    assert httpx_mock.get_requests() == []
    # Nothing loaded yet: the faster cadence is the error retry, same rule.
    retry = pod.settings.refresh_error_retry
    bundles.state = BundleState(last_checked_at=clock.now - retry * 0.96)
    assert await pod.refresh() == "not_due"
    assert pod.settings.min_check_period_seconds == min(
        pod.settings.refresh_interval_seconds, pod.settings.refresh_error_retry_seconds
    )
    assert pod.settings.github_requests_per_hour_worst_case == pytest.approx(
        3600 / pod.settings.min_check_period_seconds
        * pod.settings.max_release_pages * pod.settings.http_retry_attempts
    )
