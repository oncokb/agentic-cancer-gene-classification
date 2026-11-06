"""Bundle transport: discover → download → verify → import → atomic swap → prune.

GitHub (API and asset downloads) is mocked with pytest-httpx; the bundle is
the small fixture bundle built in tests/expression_context_helpers.py; the
bundle store is the module's in-memory implementation (the MySQL one runs the
same contract in test_expression_context_store_gate.py).
"""

from __future__ import annotations

import asyncio
import gzip
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from src.expression_context.bundle_loader import (
    BundleContractError,
    BundleLoader,
    BundleRefresher,
    GzipLineReader,
    LoaderLockLost,
    bundle_lock_key,
)
from src.expression_context.bundle_store import BUNDLE_TABLES, InMemoryBundleStore, MySQLBundleStore
from src.expression_context.catalog import BundleNotLoaded, StatsCatalog
from src.expression_context.github_releases import ReleasesClient, parse_release
from src.expression_context.lease import VerdictLease
from src.expression_context.models import JobRecord
from src.expression_context.service import ExpressionContextService
from src.expression_context.store import InMemoryExpressionContextStore, MySQLExpressionContextStore
from src.job_store import JobStore
from tests.expression_context_helpers import (
    APP_RELEASE,
    GITHUB_API,
    LIST_URL,
    RELEASE_ID,
    REPO,
    DownRedis,
    FakeLiteratureGate,
    LeaseFakeRedis,
    add_assets,
    add_bundle_release,
    add_release_list,
    asset_url,
    build_bundle,
    make_settings,
    module_redis,
    release_json,
)

pytestmark = pytest.mark.no_real_redis

T0 = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
INTERVAL = timedelta(hours=6)
ERROR_RETRY = timedelta(minutes=15)
BUNDLE_2 = date(2026, 11, 9)
BUNDLE_3 = date(2026, 11, 16)


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class Pod:
    """One ACGC pod's refresher, sharing the cluster's bundle store and Redis."""

    def __init__(self, bundles, *, redis=None, clock=None, **settings):
        self.settings = make_settings(**settings)
        self.releases = ReleasesClient(self.settings)
        self.redis = redis if redis is not None else LeaseFakeRedis()
        self.catalog = StatsCatalog(bundles, cache_seconds=0)
        self.clock = clock or Clock()
        self.refresher = BundleRefresher(
            self.settings,
            bundles=bundles,
            releases=self.releases,
            lease=VerdictLease(
                ttl_seconds=self.settings.bundle_lock_ttl_seconds, redis=module_redis(self.redis)
            ),
            on_activated=self.catalog.invalidate,
            clock=self.clock,
        )

    async def refresh(self) -> str:
        return await self.refresher.refresh_once()


@pytest.fixture
async def pods():
    created = []

    def make(bundles, **kwargs):
        pod = Pod(bundles, **kwargs)
        created.append(pod)
        return pod

    yield make
    for pod in created:
        await pod.releases.aclose()


def _requests(httpx_mock, url):
    return [r for r in httpx_mock.get_requests() if str(r.url) == url]


async def _load_first(httpx_mock, pods, bundles=None, **kwargs):
    bundles = bundles if bundles is not None else InMemoryBundleStore()
    bundle = build_bundle()
    add_bundle_release(httpx_mock, bundle, reusable=False)
    pod = pods(bundles, **kwargs)
    assert await pod.refresh() == "loaded"
    return bundles, bundle, pod


# --------------------------------------------------------------------- load


async def test_refresh_loads_newest_bundle_and_activates(httpx_mock, pods):
    bundles, bundle, pod = await _load_first(httpx_mock, pods)

    state = await bundles.get_state()
    assert state.active_bundle_tag == bundle.tag and state.previous_bundle_tag is None
    assert state.last_checked_at == T0 and state.last_error is None
    record = await bundles.get_bundle(bundle.tag)
    assert record.status == "ready" and record.loaded_rows == 2 == record.gene_count
    assert record.release_id == RELEASE_ID and record.method_version == "2"
    assert record.pubmed_snapshot_date == date(2026, 11, 2)
    assert record.release_url == f"https://github.com/{REPO}/releases/tag/{bundle.tag}"
    assert set(bundles.aliases.values()) == {"ERBB2"}

    active = await pod.catalog.active()
    assert active.tag == bundle.tag and active.release_id == RELEASE_ID
    erbb2 = await pod.catalog.gene(active, "ERBB2")
    assert erbb2.gene.entrez_gene_id == 2064 and len(erbb2.cancer_types) == 5
    assert await pod.catalog.alias(active, "HER2") == "ERBB2"
    provenance = active.provenance()
    assert provenance.source["datahub_commit"].startswith("abc1234def56")
    assert provenance.gene_count == 2


async def test_rows_are_inserted_in_bounded_batches(httpx_mock, pods):
    bundles = InMemoryBundleStore()
    await _load_first(httpx_mock, pods, bundles, load_batch_size=1)
    assert bundles.gene_batches == [1, 1]


async def test_streamed_in_small_chunks_and_multi_member_gzip(httpx_mock, pods):
    docs = [json.dumps({"x": n}).encode() + b"\n" for n in range(3)]
    stream = gzip.compress(docs[0], mtime=0) + gzip.compress(docs[1] + docs[2], mtime=0)
    reader = GzipLineReader(max_line_bytes=1024)
    lines = []
    for start in range(0, len(stream), 7):
        lines += reader.feed(stream[start:start + 7])
    lines += reader.finish()
    assert [json.loads(line)["x"] for line in lines] == [0, 1, 2]


def test_gzip_reader_bounds_memory_on_huge_lines():
    bomb = gzip.compress(b"A" * (4 * 1024 * 1024), mtime=0)  # 4 MiB, no newline, ~4 KiB gz
    reader = GzipLineReader(max_line_bytes=1024 * 1024)
    with pytest.raises(BundleContractError, match="exceeds"):
        reader.feed(bomb)


@pytest.mark.parametrize("data", [b"", b"\x1f\x8b\x08\x00garbage", gzip.compress(b"{}\n")[:-5]])
def test_gzip_reader_rejects_empty_corrupt_or_truncated(data):
    reader = GzipLineReader(max_line_bytes=1024)
    with pytest.raises(BundleContractError):
        reader.feed(data)
        reader.finish()


# ------------------------------------------------------- polling and swapping


async def test_not_due_makes_no_github_calls_on_any_pod(httpx_mock, pods):
    bundles, _, pod = await _load_first(httpx_mock, pods)
    calls = len(httpx_mock.get_requests())
    other_pod = pods(bundles, clock=pod.clock)
    pod.clock.advance(timedelta(hours=1))
    assert await pod.refresh() == "not_due"
    assert await other_pod.refresh() == "not_due"
    assert len(httpx_mock.get_requests()) == calls


async def test_up_to_date_after_interval_is_one_api_call(httpx_mock, pods):
    bundles, bundle, pod = await _load_first(httpx_mock, pods)
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(bundle)], reusable=False)
    before = len(httpx_mock.get_requests())
    assert await pod.refresh() == "up_to_date"
    assert [str(r.url) for r in httpx_mock.get_requests()[before:]] == [LIST_URL]
    assert (await bundles.get_state()).last_checked_at == pod.clock.now


async def test_polling_stays_far_below_the_unauthenticated_rate_limit(httpx_mock, pods):
    """A day of wake-ups on 5 pods: about one discovery call per interval."""
    bundles, bundle, first = await _load_first(httpx_mock, pods)
    add_release_list(httpx_mock, [release_json(bundle)])
    clock = first.clock
    fleet = [first] + [pods(bundles, clock=clock) for _ in range(4)]
    before = len(_requests(httpx_mock, LIST_URL))
    step = timedelta(seconds=first.refresher.wake_seconds)
    for _ in range(int(timedelta(days=1) / step)):
        clock.advance(step)
        for pod in fleet:
            await pod.refresh()
    discovery_calls = len(_requests(httpx_mock, LIST_URL)) - before
    assert 3 <= discovery_calls <= 5  # 24h / 6h, regardless of pod count
    assert first.refresher.wake_seconds == 15 * 60


async def test_new_bundle_swaps_keeps_previous_and_prunes_older(httpx_mock, pods):
    bundles, first_bundle, pod = await _load_first(httpx_mock, pods)
    second = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    third = build_bundle(pubmed=BUNDLE_3, published_at="2026-11-16T06:00:00Z")

    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(second), release_json(first_bundle)], reusable=False)
    add_assets(httpx_mock, second, reusable=False)
    assert await pod.refresh() == "loaded"
    state = await bundles.get_state()
    assert (state.active_bundle_tag, state.previous_bundle_tag) == (second.tag, first_bundle.tag)
    assert {tag for tag, _ in bundles.genes} == {first_bundle.tag, second.tag}
    assert (await pod.catalog.active()).tag == second.tag

    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(third), release_json(second)], reusable=False)
    add_assets(httpx_mock, third, reusable=False)
    assert await pod.refresh() == "loaded"
    state = await bundles.get_state()
    assert (state.active_bundle_tag, state.previous_bundle_tag) == (third.tag, second.tag)
    assert set(bundles.bundles) == {second.tag, third.tag}
    assert {tag for tag, _ in bundles.genes} == {second.tag, third.tag}
    assert first_bundle.tag not in {tag for tag, _ in bundles.aliases}


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_already_imported_previous_bundle_is_reactivated_without_download(httpx_mock, pods):
    bundles, first_bundle, pod = await _load_first(httpx_mock, pods)
    second = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(second), release_json(first_bundle)], reusable=False)
    add_assets(httpx_mock, second, reusable=False)
    assert await pod.refresh() == "loaded"

    # The newer release is deleted on GitHub: the cluster follows the newest
    # published bundle, which is already imported intact.
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(first_bundle)], reusable=False)
    add_assets(httpx_mock, first_bundle)
    before = len(_requests(httpx_mock, asset_url(first_bundle.tag, "genes.jsonl.gz")))
    assert await pod.refresh() == "loaded"
    assert (await bundles.get_state()).active_bundle_tag == first_bundle.tag
    assert len(_requests(httpx_mock, asset_url(first_bundle.tag, "genes.jsonl.gz"))) == before


# ---------------------------------------------------------- failures/rollback


def _corrupt(kind: str):
    """(bundle, release overrides) for a newer bundle that must be rejected."""
    common = dict(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    release_overrides = {}
    if kind == "sha256":
        bundle = build_bundle(**common, manifest_overrides={"sha256": "0" * 64})
    elif kind == "size":
        bundle = build_bundle(**common)
        bundle.manifest["size_bytes"] += 1
        release_overrides["genes_asset_size"] = bundle.manifest["size_bytes"]
    elif kind == "asset_size":
        bundle = build_bundle(**common)
        release_overrides["genes_asset_size"] = 3
    elif kind == "truncated":
        full = build_bundle(**common)
        bundle = build_bundle(**common, gz_bytes=full.genes_gz[:-8])
    elif kind == "gene_count":
        bundle = build_bundle(**common, manifest_overrides={"gene_count": 3})
    elif kind == "line_schema_version":
        bundle = build_bundle(**common, edit_gene=lambda doc: doc.update(schema_version="9"))
    elif kind == "line_release_id":
        bundle = build_bundle(**common, edit_gene=lambda doc: doc.update(release_id="other"))
    elif kind == "line_invalid":
        bundle = build_bundle(**common, edit_gene=lambda doc: doc["cancer_types"][0].pop("n"))
    elif kind == "duplicate_gene":
        bundle = build_bundle(("ERBB2", "ERBB2"), **common)
    elif kind == "not_json":
        bundle = build_bundle(**common, gz_bytes=gzip.compress(b"{nope\n", mtime=0),
                              manifest_overrides={"gene_count": 1})
    elif kind == "manifest_schema_version":
        bundle = build_bundle(**common, manifest_overrides={"schema_version": "2"})
    elif kind == "manifest_tag":
        bundle = build_bundle(**common)
        bundle.manifest["bundle_tag"] = "expression-stats-something-else"
    elif kind == "missing_genes_asset":
        bundle = build_bundle(**common)
        release_overrides["omit_assets"] = ("genes.jsonl.gz",)
    else:  # pragma: no cover
        raise AssertionError(kind)
    return bundle, release_overrides


CORRUPTIONS = [
    "sha256", "size", "asset_size", "truncated", "gene_count", "line_schema_version",
    "line_release_id", "line_invalid", "duplicate_gene", "not_json",
    "manifest_schema_version", "manifest_tag", "missing_genes_asset",
]


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("kind", CORRUPTIONS)
async def test_rejected_bundle_keeps_serving_the_last_good_one(httpx_mock, pods, kind, monkeypatch):
    sent = []
    from src import observability

    monkeypatch.setattr(observability, "increment", lambda name, value=1, tags=None: sent.append((name, tags)))
    bundles, good, pod = await _load_first(httpx_mock, pods)
    bad, overrides = _corrupt(kind)
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(bad, **overrides), release_json(good)], reusable=False)
    add_assets(httpx_mock, bad, reusable=False)

    assert await pod.refresh() == "load_failed"
    state = await bundles.get_state()
    assert state.active_bundle_tag == good.tag  # pointer untouched
    assert (await pod.catalog.active()).tag == good.tag
    assert await pod.catalog.gene(await pod.catalog.active(), "ERBB2") is not None
    assert bad.tag in state.last_error and state.last_error_at == pod.clock.now
    # A contract violation won't fix itself: no re-download for a full interval.
    assert state.next_check_after == pod.clock.now + INTERVAL
    assert not any(tag == bad.tag for tag, _ in bundles.genes)  # partial rows removed
    record = await bundles.get_bundle(bad.tag)
    assert record is None or record.status == "failed"
    assert ("expression_context.bundle.refresh", ["outcome:load_failed"]) in sent

    before = len(httpx_mock.get_requests())
    pod.clock.advance(INTERVAL / 2)
    assert await pod.refresh() == "not_due"
    assert len(httpx_mock.get_requests()) == before


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_transient_download_failure_retries_after_error_backoff(httpx_mock, pods):
    bundles, good, pod = await _load_first(httpx_mock, pods)
    bad = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(bad), release_json(good)])
    httpx_mock.add_response(url=asset_url(bad.tag, "manifest.json"), json=bad.manifest, is_reusable=True)
    httpx_mock.add_response(url=asset_url(bad.tag, "aliases.json"), json=bad.aliases, is_reusable=True)
    httpx_mock.add_response(url=asset_url(bad.tag, "genes.jsonl.gz"), status_code=502)
    assert await pod.refresh() == "load_failed"
    state = await bundles.get_state()
    assert state.active_bundle_tag == good.tag
    assert state.next_check_after == pod.clock.now + ERROR_RETRY

    pod.clock.advance(ERROR_RETRY)
    httpx_mock.add_response(url=asset_url(bad.tag, "genes.jsonl.gz"), content=bad.genes_gz)
    assert await pod.refresh() == "loaded"
    assert (await bundles.get_state()).active_bundle_tag == bad.tag


async def test_rate_limited_discovery_backs_off_until_reset(httpx_mock, pods, caplog):
    bundles, good, pod = await _load_first(httpx_mock, pods)
    pod.clock.advance(INTERVAL)
    reset = pod.clock.now + timedelta(minutes=50)
    httpx_mock.add_response(
        url=LIST_URL, status_code=403,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(reset.timestamp()))},
    )
    assert await pod.refresh() == "rate_limited"
    state = await bundles.get_state()
    assert state.active_bundle_tag == good.tag
    assert state.next_check_after == reset and "rate limit" in state.last_error
    assert "rate limit" in caplog.text

    before = len(httpx_mock.get_requests())
    pod.clock.advance(timedelta(minutes=49))
    assert await pod.refresh() == "not_due"
    assert len(httpx_mock.get_requests()) == before

    pod.clock.advance(timedelta(minutes=2))
    add_release_list(httpx_mock, [release_json(good)], reusable=False)
    assert await pod.refresh() == "up_to_date"
    assert (await bundles.get_state()).last_error is None


async def test_discovery_error_retries_after_error_backoff(httpx_mock, pods):
    bundles, good, pod = await _load_first(httpx_mock, pods)
    pod.clock.advance(INTERVAL)
    httpx_mock.add_response(url=LIST_URL, status_code=500, is_reusable=True)
    assert await pod.refresh() == "error"
    assert (await bundles.get_state()).next_check_after == pod.clock.now + ERROR_RETRY


async def test_no_bundle_yet_rechecks_sooner_and_requests_get_503(httpx_mock, pods):
    bundles = InMemoryBundleStore()
    pod = pods(bundles)
    add_release_list(httpx_mock, [APP_RELEASE])
    assert await pod.refresh() == "no_bundle"
    with pytest.raises(BundleNotLoaded):
        await pod.catalog.active()
    pod.clock.advance(ERROR_RETRY - timedelta(minutes=1))
    assert await pod.refresh() == "not_due"
    pod.clock.advance(timedelta(minutes=1))
    assert await pod.refresh() == "no_bundle"


# ------------------------------------------------------------ single loader


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_only_one_pod_loads_at_a_time(httpx_mock, pods):
    bundles, redis = InMemoryBundleStore(), LeaseFakeRedis()
    bundle = build_bundle()
    add_bundle_release(httpx_mock, bundle)
    clock = Clock()
    a, b = pods(bundles, redis=redis, clock=clock), pods(bundles, redis=redis, clock=clock)
    outcomes = sorted(await asyncio.gather(a.refresh(), b.refresh()))
    assert "loaded" in outcomes and outcomes.count("loaded") == 1
    assert set(outcomes) - {"loaded"} <= {"locked", "not_due"}
    assert len(_requests(httpx_mock, asset_url(bundle.tag, "genes.jsonl.gz"))) == 1
    assert redis.keys_with_prefix("expression_context:lease:") == []  # released


async def test_held_lease_means_locked_and_no_github_call(httpx_mock, pods):
    bundles, redis = InMemoryBundleStore(), LeaseFakeRedis()
    await redis.set(bundle_lock_key(REPO), "another-pod", px=60_000)
    pod = pods(bundles, redis=redis)
    assert await pod.refresh() == "locked"
    assert httpx_mock.get_requests() == []


async def test_redis_down_falls_back_to_mysql_advisory_lock(httpx_mock, pods):
    bundles = InMemoryBundleStore()
    bundle = build_bundle()
    add_bundle_release(httpx_mock, bundle, reusable=False)
    pod = pods(bundles, redis=DownRedis())
    assert await pod.refresh() == "loaded"
    assert bundles.advisory_lock_calls == 1

    async with bundles.advisory_lock() as held:  # another pod holds the advisory lock
        assert held
        pod.clock.advance(INTERVAL)
        assert await pod.refresh() == "locked"


async def test_lost_lease_aborts_the_load_before_activation(httpx_mock):
    bundles = InMemoryBundleStore()
    bundle = build_bundle()
    add_assets(httpx_mock, bundle, reusable=False)
    settings = make_settings(load_batch_size=1)
    releases = ReleasesClient(settings)
    try:
        payload = release_json(bundle)
        with pytest.raises(LoaderLockLost):
            await BundleLoader(settings, releases, bundles).load(
                parse_release(payload), should_abort=lambda: True
            )
    finally:
        await releases.aclose()
    assert (await bundles.get_bundle(bundle.tag)).status == "failed"
    assert bundles.genes == {} and (await bundles.get_state()).active_bundle_tag is None


async def test_heartbeat_flags_a_lost_lease(pods):
    redis = LeaseFakeRedis()
    pod = pods(InMemoryBundleStore(), redis=redis, bundle_lock_ttl_seconds=0.03)
    async with pod.refresher._single_loader() as acquired:
        assert acquired
        await redis.delete(bundle_lock_key(REPO))
        await redis.set(bundle_lock_key(REPO), "thief", px=60_000)
        await asyncio.sleep(0.05)
        assert pod.refresher._lease_lost is True
    assert (await redis.get(bundle_lock_key(REPO))) == b"thief"  # never released someone else's


# --------------------------------------------------------------------- pinning


async def test_pinned_bundle_is_loaded_and_never_advanced(httpx_mock, pods):
    bundles = InMemoryBundleStore()
    pinned = build_bundle()
    httpx_mock.add_response(
        url=f"{GITHUB_API}/repos/{REPO}/releases/tags/{pinned.tag}", json=release_json(pinned)
    )
    add_assets(httpx_mock, pinned, reusable=False)
    pod = pods(bundles, pinned_bundle_tag=pinned.tag)
    assert await pod.refresh() == "loaded"
    assert (await bundles.get_state()).active_bundle_tag == pinned.tag

    pod.clock.advance(INTERVAL * 4)  # a newer bundle exists, but we're pinned
    assert await pod.refresh() == "pinned_active"
    assert len(httpx_mock.get_requests()) == 4  # tag lookup + 3 assets; nothing since


async def test_missing_pinned_tag_is_an_error(httpx_mock, pods):
    tag = "expression-stats-gone-pubmed-20260101"
    httpx_mock.add_response(url=f"{GITHUB_API}/repos/{REPO}/releases/tags/{tag}", status_code=404)
    bundles = InMemoryBundleStore()
    pod = pods(bundles, pinned_bundle_tag=tag)
    assert await pod.refresh() == "error"
    assert "not found" in (await bundles.get_state()).last_error


# ------------------------------------------- PubMed refresh (same release_id)


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_refresh_flipping_a_precheck_from_fail_to_pass_takes_effect(httpx_mock, pods):
    """A PubMed refresh is a new bundle_tag with the same release_id: the
    pair whose pre-check now passes reaches the literature gate on the next
    job, while every other verdict is reused (verdicts are keyed by release_id)."""
    bundles, first, pod = await _load_first(httpx_mock, pods)
    gate = FakeLiteratureGate({
        "breast cancer": (False, ["12345678"]),
        "gastric cancer": (True, ["34567890"]),
        "esophageal cancer": (False, []),
        "endometrial cancer": (False, ["45678901"]),
    })
    service = ExpressionContextService(
        catalog=pod.catalog, gate=gate, store=InMemoryExpressionContextStore(),
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(LeaseFakeRedis())),
        lease_poll_seconds=0.01,
    )
    resolved = await service.resolve("ERBB2")
    result, evaluated, _, _ = await service.evaluate(resolved, "alice@example.org")
    ucec = next(p for p in result.excluded if p.oncotree_code == "UCEC")
    assert ucec.exclusion_reason == "precheck" and evaluated == 3
    assert ("ERBB2", "endometrial cancer") not in gate.calls

    def flip(doc):
        if doc["gene"]["hugo_symbol"] == "ERBB2":
            row = next(ct for ct in doc["cancer_types"] if ct["oncotree_code"] == "UCEC")
            row.update(literature_precheck_pass=True, pubmed_count=4)

    refreshed = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z", edit_gene=flip)
    assert refreshed.release_id == first.release_id and refreshed.tag != first.tag
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(refreshed), release_json(first)], reusable=False)
    add_assets(httpx_mock, refreshed, reusable=False)
    assert await pod.refresh() == "loaded"

    view = await service.stats_view("ERBB2")
    assert view.bundle.bundle_tag == refreshed.tag
    ucec_view = next(p for p in view.pairs if p.oncotree_code == "UCEC")
    assert (ucec_view.precheck, ucec_view.status) == ("pass", "literature_pending")
    assert ucec_view.stats.pubmed_count == 4

    gate.calls.clear()
    resolved = await service.resolve("ERBB2")
    assert resolved.bundle_tag == refreshed.tag
    result, evaluated, reused, _ = await service.evaluate(resolved, "alice@example.org")
    assert gate.calls == [("ERBB2", "endometrial cancer")]
    assert (evaluated, reused) == (1, 3)
    assert {p.oncotree_code for p in result.surfaced} == {"BRCA", "UCEC"}


# -------------------------------------------------------------- run loop


async def test_run_forever_survives_a_crashing_refresh(monkeypatch, pods):
    pod = pods(InMemoryBundleStore())
    calls = []
    done = asyncio.Event()

    async def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        done.set()
        return "not_due"

    monkeypatch.setattr(pod.refresher, "refresh_once", flaky)
    monkeypatch.setattr(BundleRefresher, "wake_seconds", property(lambda self: 0.001))
    task = asyncio.create_task(pod.refresher.run_forever())
    await asyncio.wait_for(done.wait(), timeout=2)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert len(calls) >= 2


async def test_metrics_and_download_failures_never_break_refresh(httpx_mock, pods, monkeypatch):
    from src import observability

    def broken(*args, **kwargs):
        raise RuntimeError("statsd down")

    monkeypatch.setattr(observability, "increment", broken)
    bundles = InMemoryBundleStore()
    pod = pods(bundles)
    httpx_mock.add_exception(httpx.ConnectError("down"), url=LIST_URL, is_reusable=True)
    assert await pod.refresh() == "error"


# ------------------------------------------------------------- real MySQL


@pytest.fixture
async def mysql_bundles():
    try:
        store = await MySQLExpressionContextStore.create(maxsize=4)
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")

    async def drop():
        async with store.pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for table in BUNDLE_TABLES:
                    await cursor.execute(f"DROP TABLE IF EXISTS {table}")

    await drop()
    bundles = MySQLBundleStore(store.pool)
    await bundles.ensure_schema()
    yield bundles
    await drop()
    await store.close()


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_load_swap_and_reject_on_real_mysql(httpx_mock, pods, mysql_bundles):
    first = build_bundle()
    add_bundle_release(httpx_mock, first, reusable=False)
    # Redis down: the single-loader lock is MySQL GET_LOCK.
    pod = pods(mysql_bundles, redis=DownRedis(), load_batch_size=1)
    assert await pod.refresh() == "loaded"
    active = await pod.catalog.active()
    assert active.tag == first.tag
    erbb2 = await pod.catalog.gene(active, "ERBB2")
    assert erbb2.model_dump() == type(erbb2).model_validate(first.genes[0]).model_dump()
    assert await pod.catalog.alias(active, "HER2") == "ERBB2"

    second = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(second), release_json(first)], reusable=False)
    add_assets(httpx_mock, second, reusable=False)
    assert await pod.refresh() == "loaded"
    state = await mysql_bundles.get_state()
    assert (state.active_bundle_tag, state.previous_bundle_tag) == (second.tag, first.tag)

    bad = build_bundle(pubmed=BUNDLE_3, published_at="2026-11-16T06:00:00Z",
                       manifest_overrides={"sha256": "0" * 64})
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(bad), release_json(second)], reusable=False)
    add_assets(httpx_mock, bad, reusable=False)
    assert await pod.refresh() == "load_failed"
    assert (await pod.catalog.active()).tag == second.tag
    assert await mysql_bundles.get_gene_doc(bad.tag, "ERBB2") is None
    assert (await mysql_bundles.get_bundle(bad.tag)).status == "failed"
