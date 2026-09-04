"""Tests for the OpenEvidence supplementary evidence client.

No test in this file makes a real network call to openevidence.com — all
HTTP is mocked via httpx.MockTransport, following the pattern used for
OncoKB (tests/test_db_lookups.py) and PubMed (tests/test_literature_cache.py).
"""

from __future__ import annotations

import httpx
import pytest

from src.pipeline import cache as cache_module
from src.pipeline.openevidence import (
    OpenEvidenceClient,
    OpenEvidenceConfigurationError,
    _build_analysis,
    _parse_sse_events,
)

SSE_STREAM = (
    'data: {"text": "BRAF mutations "}\n\n'
    'data: {"text": "are common in melanoma."}\n\n'
    'data: {"citation_key": "c1", "title": "BRAF in melanoma", "authors": "Smith J",'
    ' "journal": "Nature", "date": "2020", "doi": "10.1/abc", "url": "https://example.com/1",'
    ' "reference_text": "BRAF V600E drives melanoma."}\n\n'
    'data: {"citation_key": "c1", "title": "BRAF in melanoma", "authors": "Smith J",'
    ' "journal": "Nature", "date": "2020", "doi": "10.1/abc", "url": "https://example.com/1",'
    ' "reference_text": "Additional supporting passage."}\n\n'
    "data: [DONE]\n\n"
)


@pytest.fixture
async def _require_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")


def test_parse_sse_events_stops_at_done_marker():
    events = _parse_sse_events(SSE_STREAM)
    # Two text deltas + two citation events, [DONE] excluded.
    assert len(events) == 4


def test_parse_sse_events_skips_malformed_payload():
    raw = 'data: {"text": "ok"}\n\ndata: not-json\n\ndata: [DONE]\n\n'
    events = _parse_sse_events(raw)
    assert events == [{"text": "ok"}]


def test_build_analysis_accumulates_text_and_dedupes_citations():
    events = _parse_sse_events(SSE_STREAM)
    analysis = _build_analysis("What about BRAF?", events)

    assert analysis.question == "What about BRAF?"
    assert analysis.text == "BRAF mutations are common in melanoma."
    assert len(analysis.citations) == 1

    citation = analysis.citations[0]
    assert citation.citation_key == "c1"
    assert citation.title == "BRAF in melanoma"
    assert citation.journal == "Nature"
    assert citation.doi == "10.1/abc"
    assert citation.url == "https://example.com/1"
    # Both source_texts merged, deduplicated, order preserved.
    assert citation.source_texts == [
        "BRAF V600E drives melanoma.",
        "Additional supporting passage.",
    ]


@pytest.mark.asyncio
async def test_get_gene_analysis_requires_api_key():
    client = OpenEvidenceClient(api_key="")
    with pytest.raises(OpenEvidenceConfigurationError):
        await client.get_gene_analysis("BRAF")


@pytest.mark.asyncio
async def test_get_gene_analysis_parses_mocked_stream():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/streaming/analysis"
        assert request.headers["authorization"] == "Token test-key"
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        analysis = await client.get_gene_analysis("BRAF", tumor_type="melanoma", client=http_client)

    assert len(requests) == 1
    assert analysis.text == "BRAF mutations are common in melanoma."
    assert len(analysis.citations) == 1
    assert analysis.citations[0].citation_key == "c1"


@pytest.mark.asyncio
async def test_get_gene_analysis_caches_across_calls(_require_redis):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        first = await client.get_gene_analysis("BRAF", client=http_client)
        second = await client.get_gene_analysis("BRAF", client=http_client)

    assert first == second
    assert len(requests) == 1  # second call was a cache hit, no new HTTP request


@pytest.mark.asyncio
async def test_get_gene_analysis_retries_on_transient_failure():
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, text=SSE_STREAM)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        analysis = await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 2
    assert analysis.text == "BRAF mutations are common in melanoma."
