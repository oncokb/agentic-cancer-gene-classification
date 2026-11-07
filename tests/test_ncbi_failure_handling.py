"""NCBI failures (429s, error bodies inside HTTP 200) must never look like "no papers".

No live NCBI calls: E-utilities are served by httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

from src.models.schema import GeneAnnotation, ResolvedGene
from src.pipeline import literature, orchestrator
from src.pipeline.orchestrator import run_pipeline

RATE_LIMIT_BODY = {"error": "API rate limit exceeded", "api-key": "203.0.113.7", "count": "4", "limit": "3"}
_real_sleep = asyncio.sleep


class FakeGeneStore:
    def __init__(self, cached=None):
        self.cached = cached or {}
        self.saved = []
        self.checked = []

    async def get_gene_annotation(self, gene, tumor_type=None):
        return self.cached.get(gene)

    async def save_gene_annotation(self, annotation, updated_at, tumor_type=None):
        self.saved.append((annotation, updated_at, tumor_type))

    async def mark_gene_pubmed_checked(self, gene, checked_at, annotation=None, tumor_type=None):
        self.checked.append((gene, checked_at, annotation, tumor_type))


@pytest.fixture
def cache(monkeypatch):
    """Redis stand-in with cached_call's semantics: a raising compute stores nothing."""
    store = {}

    async def cached_call(key, compute, **_kwargs):
        if key not in store:
            store[key] = await compute()
        return store[key]

    monkeypatch.setattr(literature, "cached_call", cached_call)
    return store


@pytest.fixture
def sleeps(monkeypatch):
    """Record backoff sleeps without waiting, and skip the real token bucket."""
    recorded = []

    async def sleep(delay):
        recorded.append(delay)
        await _real_sleep(0)

    monkeypatch.setattr(literature.asyncio, "sleep", sleep)
    monkeypatch.setattr(literature, "_get_ncbi_rate_limiter", contextlib.nullcontext)
    return recorded


@pytest.fixture
def ncbi(monkeypatch, cache, sleeps):
    """Route every E-utilities call through `ncbi.handler`; returns the request log."""
    calls = []

    class Ncbi:
        handler = None

    def dispatch(request):
        calls.append(request)
        return Ncbi.handler(request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(dispatch))
    monkeypatch.setattr(literature, "_get_ncbi_client", lambda: client)
    Ncbi.calls = calls
    Ncbi.client = client
    Ncbi.cache = cache
    Ncbi.sleeps = sleeps
    return Ncbi


def _resolved(gene: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True)


def _pipeline_stubs(monkeypatch, gene: str):
    async def fake_normalize_fusions(_inputs):
        return {gene: (_resolved(gene), [gene])}

    async def not_in_oncokb(_gene, lookup=None):
        return False

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "check_oncokb_membership", not_in_oncokb)
    monkeypatch.setattr(orchestrator, "get_msk_genie_prevalence", lambda _gene: None)


# ---------------------------------------------------------------------------
# Embedded error detection + retry in _ncbi_post
# ---------------------------------------------------------------------------


async def test_embedded_rate_limit_in_200_is_retried_then_succeeds(ncbi):
    def handler(request):
        if len(ncbi.calls) == 1:
            return httpx.Response(200, json=RATE_LIMIT_BODY)
        return httpx.Response(200, json={"esearchresult": {"idlist": ["42"]}})

    ncbi.handler = handler
    assert await literature._esearch("BRAF", 10, ncbi.client) == ["42"]
    assert len(ncbi.calls) == 2
    assert ncbi.sleeps == [1]
    assert list(ncbi.cache.values()) == [["42"]]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json=RATE_LIMIT_BODY),
        httpx.Response(200, json={"esearchresult": {"ERROR": "Search Backend failed", "idlist": []}}),
        httpx.Response(200, json={"ERROR": "Invalid db name specified"}),
        httpx.Response(200, text="<eSearchResult><ERROR>Search Backend failed</ERROR></eSearchResult>"),
    ],
    ids=["json-rate-limit", "esearchresult-ERROR", "top-level-ERROR", "xml-ERROR"],
)
async def test_persistent_embedded_error_in_esearch_raises_and_is_not_cached(ncbi, response):
    ncbi.handler = lambda _request: response

    with pytest.raises(literature.NCBIEmbeddedError) as caught:
        await literature._esearch("BRAF", 10, ncbi.client)

    assert isinstance(caught.value, httpx.HTTPError)  # existing PubMed handlers catch it
    assert len(ncbi.calls) == literature.NCBI_MAX_ATTEMPTS
    assert ncbi.sleeps == [1, 2]
    assert ncbi.cache == {}
    assert "203.0.113.7" not in literature.pubmed_error_message(caught.value)


async def test_embedded_error_in_efetch_raises_and_is_not_cached(ncbi):
    # efetch asks for XML; NCBI may still answer a rate limit as JSON, or as <ERROR>.
    bodies = iter([
        httpx.Response(200, json=RATE_LIMIT_BODY),
        httpx.Response(200, text="<eFetchResult><ERROR>API rate limit exceeded</ERROR></eFetchResult>"),
        httpx.Response(200, json=RATE_LIMIT_BODY),
    ])
    ncbi.handler = lambda _request: next(bodies)

    with pytest.raises(literature.NCBIEmbeddedError) as caught:
        await literature._efetch(["1", "2"], ncbi.client)

    assert literature.pubmed_error_message(caught.value) == "PubMed search failed (NCBI rate limit)"
    assert len(ncbi.calls) == literature.NCBI_MAX_ATTEMPTS
    assert ncbi.cache == {}


async def test_zero_hit_search_with_errorlist_is_a_cacheable_empty_result(ncbi):
    # NCBI reports a legitimate zero-hit search through errorlist/warninglist, not ERROR.
    ncbi.handler = lambda _request: httpx.Response(
        200,
        json={
            "esearchresult": {
                "count": "0",
                "idlist": [],
                "errorlist": {"phrasesnotfound": ["NOTAGENE"], "fieldsnotfound": []},
                "warninglist": {"quotedphrasesnotfound": ["NOTAGENE"], "outputmessages": ["No items found."]},
            }
        },
    )

    assert await literature._esearch('"NOTAGENE"', 10, ncbi.client) == []
    assert len(ncbi.calls) == 1
    assert list(ncbi.cache.values()) == [[]]


# ---------------------------------------------------------------------------
# Pipeline: a failed search is never persisted as insufficient_evidence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(429),
        httpx.Response(200, json=RATE_LIMIT_BODY),
    ],
    ids=["exhausted-429", "embedded-rate-limit"],
)
async def test_failed_literature_search_is_not_cached_and_surfaces_error(monkeypatch, ncbi, failure):
    ncbi.handler = lambda _request: failure
    _pipeline_stubs(monkeypatch, "NOTCH9")

    async def fail(*_args, **_kwargs):
        raise AssertionError("a failed PubMed search must not reach Tier 2, selection or synthesis")

    monkeypatch.setattr(literature, "_tier2_agentic_retrieve", fail)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fail)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", fail)
    store = FakeGeneStore()

    result = await run_pipeline(["NOTCH9"], run_store=store)

    annotation = result.annotations[0]
    assert store.saved == []
    assert annotation.error is not None and "Literature retrieval failed" in annotation.error
    assert annotation.cache_status == "bypassed"
    assert annotation.cache_reason == "literature_retrieval_failed"
    assert annotation.citations == []
    # The failing query was retried with the existing backoff before giving up.
    assert len(ncbi.calls) >= literature.NCBI_MAX_ATTEMPTS
    assert ncbi.sleeps[:2] == [1, 2]
    assert ncbi.cache == {}


async def test_genuine_zero_hit_search_is_still_cached_as_insufficient_evidence(monkeypatch, ncbi):
    ncbi.handler = lambda _request: httpx.Response(200, json={"esearchresult": {"count": "0", "idlist": []}})
    _pipeline_stubs(monkeypatch, "NOTCH9")

    async def tier2_finds_nothing(gene, fusions, initial_records, tumor_type=None):
        return initial_records

    async def select(_gene, records, *_args, **_kwargs):
        return records

    async def synthesize(**_kwargs):
        return {
            "cancer_associated": False,
            "insufficient_evidence": True,
            "cancer_association_rationale": "No cancer literature was retrieved.",
            "gene_summary": "No retrieved literature.",
            "citations": [],
        }

    monkeypatch.setattr(literature, "_tier2_agentic_retrieve", tier2_finds_nothing)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", select)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", synthesize)
    store = FakeGeneStore()

    result = await run_pipeline(["NOTCH9"], run_store=store)

    annotation = result.annotations[0]
    assert annotation.error is None
    assert annotation.insufficient_evidence is True
    assert len(store.saved) == 1
    assert store.saved[0][0].insufficient_evidence is True
    assert ncbi.sleeps == []


async def test_tier2_ncbi_outage_fails_retrieval_instead_of_returning_partial_records(monkeypatch, ncbi):
    ncbi.handler = lambda _request: httpx.Response(200, json=RATE_LIMIT_BODY)

    async def suggest(**_kwargs):
        return {"queries": ['"NOTCH9" cancer', '"NOTCH9" tumor']}

    monkeypatch.setattr(literature, "complete_with_tool", suggest)

    with pytest.raises(literature.LiteratureRetrievalError):
        await literature._tier2_local_retrieve("NOTCH9", [], [], None)
    assert len(ncbi.calls) == literature.NCBI_MAX_ATTEMPTS  # stopped at the first failing query


# ---------------------------------------------------------------------------
# Freshness probe: a failed check never extends freshness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [httpx.Response(429), httpx.Response(200, json=RATE_LIMIT_BODY)],
    ids=["exhausted-429", "embedded-rate-limit"],
)
async def test_failed_freshness_check_reuses_cache_without_extending_freshness(monkeypatch, ncbi, failure):
    ncbi.handler = lambda _request: failure
    _pipeline_stubs(monkeypatch, "BRAF")
    updated_at = datetime.now(timezone.utc) - timedelta(days=80)
    checked_at = datetime.now(timezone.utc) - timedelta(days=40)
    cached = GeneAnnotation(
        gene="BRAF",
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {"BRAF": {"annotation": cached.model_dump(), "updated_at": updated_at, "last_pubmed_checked_at": checked_at}}
    )

    async def no_retractions(_annotation):
        return set()

    async def fail_if_called(**_kwargs):
        raise AssertionError("a failed freshness probe should fall back to the cached annotation")

    monkeypatch.setattr(orchestrator.settings, "gene_cache_final_annotation_days", 0)
    monkeypatch.setattr(orchestrator, "find_retracted_annotation_pmids", no_retractions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["BRAF"], run_store=store)

    annotation = result.annotations[0]
    assert annotation.cache_status == "reused"
    assert annotation.cache_reason == "pubmed_freshness_check_failed"
    assert annotation.last_pubmed_checked_at == checked_at.isoformat()
    assert "pubmed_freshness_check_unavailable" in {flag.code for flag in annotation.quality_flags}
    assert store.checked == []  # last_pubmed_checked_at is not advanced
    assert store.saved == []
    assert len(ncbi.calls) >= literature.NCBI_MAX_ATTEMPTS
    assert all(parse_qs(call.content.decode())["sort"] == ["pub date"] for call in ncbi.calls)
