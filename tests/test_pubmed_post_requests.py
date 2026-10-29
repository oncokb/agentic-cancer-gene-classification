"""Regression coverage for bounded PubMed POST requests (no live NCBI calls)."""
import asyncio
from datetime import datetime, timezone
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

import httpx
import pytest

from src import main
from src.models.schema import FusionInput, LiteratureRecord
from src.pipeline import literature


async def _uncached(_key, compute, **_kwargs):
    return await compute()


@pytest.fixture(autouse=True)
def uncached(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)


@pytest.mark.parametrize("recent", [False, True])
async def test_search_posts_form_with_api_key(monkeypatch, recent):
    monkeypatch.setattr(literature.settings, "ncbi_api_key", "test-key")

    def handler(request):
        assert request.method == "POST"
        assert not request.url.query
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        form = parse_qs(request.content.decode())
        assert form["api_key"] == ["test-key"]
        assert form["db"] == ["pubmed"]
        assert "test query" in form["term"][0]
        if recent:
            assert form["mindate"] == ["2026/01/02"]
        return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if recent:
            result = await literature._esearch_since("test query", datetime(2026, 1, 2, tzinfo=timezone.utc), 10, client)
        else:
            result = await literature._esearch("test query", 10, client)
    assert result == ["1"]


async def test_406_pmids_chunk_cached_in_order_and_used_by_retraction_check(monkeypatch):
    calls = []
    cache = {}

    async def cached(key, compute, **kwargs):
        if key not in cache:
            cache[key] = await compute()
        return cache[key]

    monkeypatch.setattr(literature, "cached_call", cached)
    monkeypatch.setattr(literature.settings, "ncbi_api_key", "test-key")

    def handler(request):
        assert request.method == "POST"
        assert not request.url.query
        form = parse_qs(request.content.decode())
        assert form["api_key"] == ["test-key"]
        pmids = form["id"][0].split(",")
        calls.append(pmids)
        articles = "".join(
            f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
            "<ArticleTitle>Study</ArticleTitle><Abstract><AbstractText>Evidence</AbstractText></Abstract>"
            + ("<PublicationType>Retracted Publication</PublicationType>" if pmid == "405" else "")
            + "</Article></MedlineCitation></PubmedArticle>" for pmid in reversed(pmids)
        )
        return httpx.Response(200, text=f"<PubmedArticleSet>{articles}</PubmedArticleSet>")

    pmids = [str(i) for i in range(406)]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setattr(literature, "_get_ncbi_client", lambda: client)
        records = await literature._efetch(pmids, client, filter_retracted=False)
        assert [record.pmid for record in records] == pmids
        assert await literature.find_retracted_pmids(pmids) == {"405"}
        await literature._efetch(pmids[:200], client)
    assert [len(chunk) for chunk in calls] == [200, 200, 6]
    assert len(cache) == 3


@pytest.mark.parametrize("status", [400, 401, 404, 414])
async def test_client_errors_not_retried_and_stored_readably(monkeypatch, status, caplog):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async def retrieve(*args, **kwargs):
            return await literature._esearch("fusion", 20, client)

        monkeypatch.setattr(main, "retrieve_fusion_evidence", retrieve)
        result = await main._retrieve_fusion_evidence_input(FusionInput(fusion="EML4::ALK"), asyncio.Semaphore(1))
    assert len(calls) == 1
    assert result.retrieval_status == "failed"
    assert result.literature_scope is None
    assert result.interpretation == f"PubMed search failed (HTTP {status})"
    assert f"HTTP {status}" in caplog.text
    assert "RetryError" not in result.interpretation


@pytest.mark.parametrize("retry_after, expected_calls", [("2", 2), ("120", 1)])
async def test_429_retry_after_bounded_by_deadline(monkeypatch, retry_after, expected_calls):
    sleeps = []
    now = [0.0]
    calls = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(literature, "monotonic", lambda: now[0])
    monkeypatch.setattr(literature.asyncio, "sleep", sleep)
    monkeypatch.setattr(literature, "NCBI_RETRY_DEADLINE_SECONDS", 5)

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if expected_calls == 1:
            with pytest.raises(httpx.HTTPStatusError) as caught:
                await literature._esearch("query", 10, client)
            assert literature.pubmed_error_message(caught.value) == "PubMed search failed (HTTP 429)"
        else:
            assert await literature._esearch("query", 10, client) == ["1"]
    assert len(calls) == expected_calls
    assert sleeps == ([2.0] if expected_calls == 2 else [])


@pytest.mark.parametrize("failed_index", [0, 1])
async def test_one_search_fails_keeps_other_results_and_scope(monkeypatch, failed_index):
    monkeypatch.setattr(literature, "resolve_fusion_partner_aliases", AsyncMock(return_value=([], [])))
    calls = []

    async def search(*args):
        calls.append(args)
        if len(calls) - 1 == failed_index:
            response = httpx.Response(414, request=httpx.Request("POST", literature.ESEARCH_URL))
            response.raise_for_status()
        return ["42"]

    monkeypatch.setattr(literature, "_esearch", search)
    monkeypatch.setattr(literature, "_efetch", AsyncMock(return_value=[LiteratureRecord(
        pmid="42", title="EML4-ALK fusion in lung adenocarcinoma", abstract="Recurrent fusion."
    )]))
    result = await literature._retrieve_fusion_evidence_uncached("EML4::ALK", "LUAD", 20)
    assert result.pmids == ["42"]
    assert result.partial is True
    assert result.retrieval_status == "ok"
    assert result.literature_scope == (None if failed_index == 0 else "requested_tumor")
    assert result.requested_tumor_pmids == ["42"]
    assert "One search failed" in result.interpretation
    assert "HTTP 414" in result.interpretation


def test_fusion_queries_cap_phrases_and_drop_junk_only_from_query():
    aliases = ["FLJ123", "KIAA123", "DKFZp123", "MGC123", "ABC-OT1", "MOZ", "MYST3", "ZNF220", "A1", "A2", "A3"]
    variants = literature._fusion_query_variants("KAT6A::CREBBP", aliases, ["CBP", "RSTS", "B1", "B2", "B3", "B4"])
    assert len(variants) <= 120
    assert any("MOZ-CBP" == variant for variant in variants)
    assert not any(junk in variant for junk in aliases[:5] for variant in variants)
    record = LiteratureRecord(pmid="1", title="FLJ123-CREBBP fusion", abstract="Fusion in cancer")
    assert literature.record_discusses_exact_fusion(record, "KAT6A::CREBBP", aliases, [])


@pytest.mark.parametrize("failure", [503, "timeout"])
async def test_transient_failures_retry_with_backoff_and_real_exception(monkeypatch, failure):
    sleeps = []
    calls = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr(literature.asyncio, "sleep", sleep)

    def handler(request):
        assert request.method == "POST"
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("slow PubMed", request=request)
        return httpx.Response(failure)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises((httpx.HTTPStatusError, httpx.TimeoutException)) as caught:
            await literature._esearch("query", 10, client)
    assert len(calls) == 3
    assert sleeps == [1, 2]
    expected = "timeout" if failure == "timeout" else "HTTP 503"
    assert literature.pubmed_error_message(caught.value) == f"PubMed search failed ({expected})"


async def test_deadline_includes_limiter_wait(monkeypatch):
    class BlockedLimiter:
        async def __aenter__(self):
            await asyncio.Event().wait()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(literature, "_get_ncbi_rate_limiter", BlockedLimiter)
    monkeypatch.setattr(literature, "NCBI_RETRY_DEADLINE_SECONDS", 0.01)
    handler = AsyncMock(side_effect=AssertionError("limiter never admitted a request"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.TimeoutException):
            await literature._esearch("query", 10, client)
    handler.assert_not_called()


async def test_all_searches_fail_has_no_literature_scope(monkeypatch):
    monkeypatch.setattr(literature, "resolve_fusion_partner_aliases", AsyncMock(return_value=([], [])))
    monkeypatch.setattr(literature, "_esearch", AsyncMock(side_effect=httpx.ReadTimeout("timeout")))
    monkeypatch.setattr(main, "retrieve_fusion_evidence", literature.retrieve_fusion_evidence)
    fetch = AsyncMock()
    monkeypatch.setattr(literature, "_efetch", fetch)
    result = await main._retrieve_fusion_evidence_input(
        FusionInput(fusion="EML4::ALK", tumor_type="LUAD"), asyncio.Semaphore(1),
    )
    assert result.retrieval_status == "failed"
    assert result.literature_scope is None
    assert result.interpretation == "PubMed search failed (timeout)"
    fetch.assert_not_called()


async def test_non_pair_input_is_not_a_retryable_lookup_failure():
    result = await literature._retrieve_fusion_evidence_uncached("BRAF", None, 20)
    assert result.retrieval_status == "ok"
    assert result.literature_scope is None


async def test_sanitizing_partial_results_keeps_failure_note():
    from src.models.schema import FusionEvidenceResult
    from src.pipeline.result_sanitizer import sanitize_fusion_evidence_result

    result = FusionEvidenceResult(
        fusion="EML4::ALK", partial=True, pmids=["42"], retrieved_count=1,
        literature_scope="any_tumor", interpretation="One search failed; results are partial.",
    )
    assert await sanitize_fusion_evidence_result(result, {"42"})
    assert result.partial is True
    assert result.literature_scope is None
    assert "One search failed" in result.interpretation


@pytest.mark.parametrize("force_refresh", [False, True])
async def test_partial_fusion_result_is_not_cached(monkeypatch, fake_redis, force_refresh):
    from src.models.schema import FusionEvidenceResult
    from src.pipeline import cache

    monkeypatch.setattr(literature, "cached_call", cache.cached_call)
    compute = AsyncMock(side_effect=[
        FusionEvidenceResult(fusion="EML4::ALK", partial=True),
        FusionEvidenceResult(fusion="EML4::ALK", literature_scope="no_literature"),
    ])
    monkeypatch.setattr(literature, "_retrieve_fusion_evidence_uncached", compute)
    first = await literature.retrieve_fusion_evidence("EML4::ALK", "LUAD", force_refresh=force_refresh)
    assert first.partial
    assert not fake_redis.keys_with_prefix("fusion_evidence:")
    second = await literature.retrieve_fusion_evidence("EML4::ALK", "LUAD")
    assert not second.partial
    assert len(fake_redis.keys_with_prefix("fusion_evidence:")) == 1
    third = await literature.retrieve_fusion_evidence("EML4::ALK", "LUAD")
    assert third == second
    assert compute.await_count == 2


@pytest.mark.parametrize("failed_index", [0, 1])
@pytest.mark.parametrize("paper", [None, "other", "tumor"])
async def test_partial_scope_is_unknown_if_empty_or_tumor_search_failed(monkeypatch, failed_index, paper):
    monkeypatch.setattr(literature, "resolve_fusion_partner_aliases", AsyncMock(return_value=([], [])))
    error = httpx.HTTPStatusError("upstream error", request=httpx.Request("POST", literature.ESEARCH_URL), response=httpx.Response(503))
    outcomes = [[], []] if paper is None else [["42"], ["42"]]
    outcomes[failed_index] = error
    monkeypatch.setattr(literature, "_esearch", AsyncMock(side_effect=outcomes))
    records = [] if paper is None else [LiteratureRecord(
        pmid="42", title="EML4-ALK fusion in " + ("lung adenocarcinoma" if paper == "tumor" else "cancer"), abstract="Fusion evidence",
    )]
    monkeypatch.setattr(literature, "_efetch", AsyncMock(return_value=records))
    result = await literature._retrieve_fusion_evidence_uncached("EML4::ALK", "LUAD", 20)
    assert result.partial
    assert result.retrieval_status == "ok"
    expected = None if failed_index == 0 or paper is None else ("requested_tumor" if paper == "tumor" else "other_or_unspecified")
    assert result.literature_scope == expected
    assert "One search failed" in result.interpretation


async def test_sanitizer_keeps_unknown_partial_scope_and_all_verified_pmids():
    from src.models.schema import FusionEvidenceResult
    from src.pipeline.result_sanitizer import sanitize_fusion_evidence_result

    result = FusionEvidenceResult(
        fusion="EML4::ALK", partial=True, tumor_type="LUAD", pmids=[str(i) for i in range(7)],
        retrieved_count=7, interpretation="One search failed; results are partial.",
    )
    assert await sanitize_fusion_evidence_result(result, {"0"})
    assert result.pmids == [str(i) for i in range(1, 7)]
    assert result.literature_scope is None
    assert "One search failed" in result.interpretation


def test_canonical_partner_pairs_survive_phrase_cap():
    variants = literature._fusion_query_variants(
        "FMN1::APBA2", [f"F{i}" for i in range(6)], [f"A{i}" for i in range(6)],
    )
    assert len(variants) == 120
    assert "F5-APBA2" in variants
    assert "FMN1-A5" in variants
    first_alias_pair = variants.index("F0::A0")
    assert variants.index("F5-APBA2") < first_alias_pair
    assert variants.index("FMN1-A5") < first_alias_pair
