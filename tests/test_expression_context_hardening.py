"""Cross-review hardening of the bundle transport (PR #167, review of b6f8cef).

1. Discovery picks the newest *published* bundle across every page, and never
   guesses when the page cap is hit before the end of the list.
2. One authoritative single-loader lock (MySQL-side, in the bundle store): a
   pod whose Redis is down can't load concurrently, and losing the lock
   aborts a load before activation.
3. Bundles that just stopped being active/previous are kept for a grace
   period, and readers whose cached bundle vanished fall back to the active
   one instead of answering 404.
4. aliases.json is a required, validated asset.
5. Expired PubMed pre-checks (source.precheck_expires_at) don't exclude pairs.
6. Settings keep the cluster's GitHub API budget bounded.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import httpx
import pytest
from pydantic import ValidationError

from src.expression_context.bundle_store import InMemoryBundleStore
from src.expression_context.catalog import StatsCatalog
from src.expression_context.github_releases import ReleasesClient
from src.expression_context.models import JobRecord
from src.expression_context.service import ExpressionContextService
from src.expression_context.settings import ExpressionContextSettings
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.expression_context_helpers import (
    APP_RELEASE,
    GITHUB_API,
    LIST_URL,
    DownRedis,
    FakeLiteratureGate,
    LeaseFakeRedis,
    add_assets,
    add_release_list,
    asset_url,
    build_bundle,
    make_service,
    make_settings,
    release_json,
    seeded_bundles,
)
from tests import test_expression_context_bundle as bundle_tests
from tests.test_expression_context_bundle import BUNDLE_2, BUNDLE_3, INTERVAL, Clock, _load_first

pods = bundle_tests.pods  # shared fixture

pytestmark = pytest.mark.no_real_redis

PAGE_2 = f"{GITHUB_API}/repositories/1/releases?per_page=100&page=2"
PAGE_3 = f"{GITHUB_API}/repositories/1/releases?per_page=100&page=3"


# ------------------------------------------------- 1. discovery across pages


async def test_discovery_picks_newest_published_bundle_on_a_later_page(httpx_mock):
    older = build_bundle(published_at="2026-11-02T06:00:00Z")
    # Created earlier (so listed on a later page) but published later.
    newer = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    httpx_mock.add_response(
        url=LIST_URL, json=[APP_RELEASE] * 99 + [release_json(older)],
        headers={"Link": f'<{PAGE_2}>; rel="next"'},
    )
    httpx_mock.add_response(url=PAGE_2, json=[release_json(newer)])
    client = ReleasesClient(make_settings())
    try:
        assert (await client.latest_bundle()).tag == newer.tag
    finally:
        await client.aclose()


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_page_cap_hit_keeps_the_active_bundle(httpx_mock, pods, monkeypatch):
    sent = []
    from src import observability

    monkeypatch.setattr(observability, "increment",
                        lambda name, value=1, tags=None: sent.append((name, tags)))
    bundles, first, pod = await _load_first(httpx_mock, pods, max_release_pages=2)
    newer = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    pod.clock.advance(INTERVAL)
    httpx_mock.add_response(
        url=LIST_URL, json=[release_json(newer)] + [APP_RELEASE] * 99,
        headers={"Link": f'<{PAGE_2}>; rel="next"'},
    )
    httpx_mock.add_response(
        url=PAGE_2, json=[APP_RELEASE] * 100, headers={"Link": f'<{PAGE_3}>; rel="next"'}
    )
    add_assets(httpx_mock, newer)
    assert await pod.refresh() == "discovery_incomplete"
    state = await bundles.get_state()
    assert state.active_bundle_tag == first.tag
    assert "incomplete" in state.last_error
    assert not any(str(r.url) == asset_url(newer.tag, "genes.jsonl.gz") for r in httpx_mock.get_requests())
    assert ("expression_context.bundle.refresh", ["outcome:discovery_incomplete"]) in sent


# -------------------------------------------------- 2. one authoritative lock


def _slow_genes(httpx_mock, bundle, delay):
    async def respond(request):
        await asyncio.sleep(delay)
        return httpx.Response(200, content=bundle.genes_gz)

    httpx_mock.add_callback(respond, url=asset_url(bundle.tag, "genes.jsonl.gz"), is_reusable=True)
    httpx_mock.add_response(url=asset_url(bundle.tag, "manifest.json"), json=bundle.manifest, is_reusable=True)
    httpx_mock.add_response(url=asset_url(bundle.tag, "aliases.json"), json=bundle.aliases, is_reusable=True)


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_redis_down_pod_cannot_load_while_another_pod_loads(httpx_mock, pods):
    """Split brain: A has Redis, B's Redis is unreachable. Only one may load."""
    bundles = InMemoryBundleStore()
    bundle = build_bundle()
    add_release_list(httpx_mock, [release_json(bundle)])
    _slow_genes(httpx_mock, bundle, delay=0.2)
    clock = Clock()
    a = pods(bundles, redis=LeaseFakeRedis(), clock=clock)
    b = pods(bundles, redis=DownRedis(), clock=clock)

    async def b_slightly_later():
        await asyncio.sleep(0.05)  # A is mid-download
        return await b.refresh()

    outcomes = await asyncio.gather(a.refresh(), b_slightly_later())
    assert outcomes == ["loaded", "locked"]
    downloads = [r for r in httpx_mock.get_requests() if str(r.url).endswith("genes.jsonl.gz")]
    assert len(downloads) == 1


class RenewFailsStore(InMemoryBundleStore):
    """The lock backend becomes unreachable after the lock was taken."""

    async def renew_loader_lock(self, token, ttl_seconds):
        raise ConnectionError("lock backend unreachable")


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_lock_renewal_failure_aborts_the_load(httpx_mock, pods):
    bundles = RenewFailsStore()
    bundle = build_bundle()
    add_release_list(httpx_mock, [release_json(bundle)])
    _slow_genes(httpx_mock, bundle, delay=0.1)
    # Redis is fine for this pod: the Redis lease must not be what keeps the load alive.
    pod = pods(bundles, redis=LeaseFakeRedis(), bundle_lock_ttl_seconds=0.03)
    assert await pod.refresh() == "load_failed"
    assert (await bundles.get_state()).active_bundle_tag is None
    assert "lock" in (await bundles.get_state()).last_error.lower()


# ------------------------------------------- 3. pruning vs. in-flight readers


def _reader(bundles, cache_seconds=30.0):
    gate = FakeLiteratureGate()
    return ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=cache_seconds), gate=gate,
        store=InMemoryExpressionContextStore(),
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
    )


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_two_rapid_activations_do_not_break_readers_of_the_old_bundle(httpx_mock, pods):
    bundles, first, pod = await _load_first(httpx_mock, pods)
    reader = _reader(bundles)
    assert (await reader.resolve("ERBB2")).bundle_tag == first.tag  # cached for 30 s

    for pubmed, published in ((BUNDLE_2, "2026-11-09T06:00:00Z"), (BUNDLE_3, "2026-11-16T06:00:00Z")):
        nxt = build_bundle(pubmed=pubmed, published_at=published)
        pod.clock.advance(INTERVAL)
        add_release_list(httpx_mock, [release_json(nxt)], reusable=False)
        add_assets(httpx_mock, nxt, reusable=False)
        assert await pod.refresh() == "loaded"

    # The reader still holds the first tag: it must keep answering.
    assert (await reader.resolve("ERBB2")).hugo_symbol == "ERBB2"
    assert (await reader.stats_view("her2")).hugo_symbol == "ERBB2"
    assert first.tag in bundles.bundles  # retired, inside the grace period


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_retired_bundles_are_pruned_after_the_grace_period(httpx_mock, pods):
    bundles, first, pod = await _load_first(httpx_mock, pods)
    tags = [first.tag]
    for pubmed, published in ((BUNDLE_2, "2026-11-09T06:00:00Z"), (BUNDLE_3, "2026-11-16T06:00:00Z")):
        nxt = build_bundle(pubmed=pubmed, published_at=published)
        tags.append(nxt.tag)
        pod.clock.advance(INTERVAL)
        add_release_list(httpx_mock, [release_json(nxt)], reusable=False)
        add_assets(httpx_mock, nxt, reusable=False)
        assert await pod.refresh() == "loaded"
    assert set(bundles.bundles) == set(tags)
    pod.clock.advance(INTERVAL)  # well past the 1 h grace
    add_release_list(httpx_mock, [release_json(build_bundle(pubmed=BUNDLE_3, published_at="2026-11-16T06:00:00Z"))], reusable=False)
    assert await pod.refresh() == "up_to_date"
    assert set(bundles.bundles) == set(tags[1:])
    assert not any(tag == first.tag for tag, _ in bundles.genes)


async def test_reader_falls_back_to_the_active_bundle_if_its_bundle_vanished():
    bundles = seeded_bundles()
    reader = _reader(bundles)
    old = (await reader.resolve("ERBB2")).bundle_tag
    # Another bundle becomes active and the cached one disappears entirely.
    newer = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    fresh = seeded_bundles(newer)
    bundles.bundles = {newer.tag: fresh.bundles[newer.tag]}
    bundles.genes = dict(fresh.genes)
    bundles.aliases = dict(fresh.aliases)
    bundles.state = fresh.state
    resolved = await reader.resolve("ERBB2")
    assert resolved.bundle_tag == newer.tag != old
    assert (await reader.stats_view("HER2")).bundle.bundle_tag == newer.tag


# ---------------------------------------------------- 4. aliases.json required


@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
@pytest.mark.parametrize("problem", ["missing", "not_an_object", "non_string_value"])
async def test_bad_or_missing_aliases_reject_the_bundle(httpx_mock, pods, problem):
    bundles, good, pod = await _load_first(httpx_mock, pods)
    bad = build_bundle(pubmed=BUNDLE_2, published_at="2026-11-09T06:00:00Z")
    overrides = {}
    if problem == "missing":
        overrides["omit_assets"] = ("aliases.json",)
    elif problem == "not_an_object":
        bad.aliases = ["HER2", "ERBB2"]
    else:
        bad.aliases = {"HER2": ["ERBB2"]}
    pod.clock.advance(INTERVAL)
    add_release_list(httpx_mock, [release_json(bad, **overrides)], reusable=False)
    add_assets(httpx_mock, bad, reusable=False)
    assert await pod.refresh() == "load_failed"
    state = await bundles.get_state()
    assert state.active_bundle_tag == good.tag
    assert "aliases.json" in state.last_error


# -------------------------------------------------- 5. expired PubMed prechecks


def _expiring_bundle(expires_at):
    bundle = build_bundle()
    bundle.manifest["source"]["precheck_expires_at"] = expires_at
    return bundle


def _gate():
    return FakeLiteratureGate({
        "breast cancer": (False, ["12345678"]),
        "endometrial cancer": (False, ["45678901"]),
    })


async def test_expired_precheck_does_not_exclude_jobs_or_page():
    # ERBB2 UCEC: stats_pass true, literature_precheck_pass false (pubmed_count 0).
    gate = _gate()
    service = make_service(gate=gate, bundles=seeded_bundles(_expiring_bundle("2026-01-01T00:00:00Z")))
    view = await service.stats_view("ERBB2")
    ucec = next(p for p in view.pairs if p.oncotree_code == "UCEC")
    assert (ucec.precheck, ucec.literature_eligible, ucec.status) == ("expired", True, "literature_pending")

    resolved = await service.resolve("ERBB2")
    result, _, _, _ = await service.evaluate(resolved, "alice@example.org")
    assert ("ERBB2", "endometrial cancer") in gate.calls
    assert "UCEC" in {p.oncotree_code for p in result.surfaced}
    stored = next(p for p in result.surfaced if p.oncotree_code == "UCEC")
    assert stored.stats.literature_precheck_pass is False  # raw value kept for display


async def test_fresh_precheck_still_excludes():
    gate = _gate()
    service = make_service(gate=gate, bundles=seeded_bundles(_expiring_bundle("2099-01-01T00:00:00Z")))
    ucec = next(p for p in (await service.stats_view("ERBB2")).pairs if p.oncotree_code == "UCEC")
    assert (ucec.precheck, ucec.status, ucec.exclusion_reason) == ("fail", "excluded", "precheck")
    resolved = await service.resolve("ERBB2")
    await service.evaluate(resolved, None)
    assert ("ERBB2", "endometrial cancer") not in gate.calls


# ------------------------------------------------------ 6. GitHub API budget


def test_default_settings_fit_the_unauthenticated_budget():
    settings = ExpressionContextSettings(_env_file=None, enabled=True)
    assert settings.github_requests_per_hour_worst_case <= 30


@pytest.mark.parametrize(
    "overrides",
    [
        {"refresh_interval": 300, "refresh_error_retry": 300, "max_release_pages": 20},
        {"refresh_error_retry": 60},
        {"max_release_pages": 20},
    ],
)
def test_settings_reject_configs_that_could_exceed_the_budget(overrides):
    with pytest.raises(ValidationError, match="GitHub API budget"):
        ExpressionContextSettings(_env_file=None, enabled=True, **overrides)


def test_a_token_allows_a_larger_budget():
    settings = ExpressionContextSettings(
        _env_file=None, enabled=True, github_token="ghp_x", max_release_pages=20,
        refresh_error_retry=timedelta(minutes=5),
    )
    assert settings.github_requests_per_hour_worst_case > 30
