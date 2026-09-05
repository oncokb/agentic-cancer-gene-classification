"""Tests for the OpenEvidence supplementary evidence client.

No test in this file makes a real network call to openevidence.com — all
HTTP is mocked via httpx.MockTransport, following the pattern used for
OncoKB (tests/test_db_lookups.py) and PubMed (tests/test_literature_cache.py).

Event fixtures below are the REAL, confirmed shapes — verified against both
the official OpenEvidence API docs and a live-captured streaming response
(HTTP 200, real API key, question about BRAF V600E in melanoma). There is no
`[DONE]` sentinel anywhere in the real contract; citation data is nested
under event["reference"]["reference_detail"], not flat top-level fields.
"""

from __future__ import annotations

import json

import httpx
import pytest
from tenacity import RetryError

from src.config import settings
from src.pipeline import cache as cache_module
from src.pipeline.openevidence import (
    OpenEvidenceClient,
    OpenEvidenceConfigurationError,
    _build_analysis,
    _iter_sse_payloads,
    _parse_sse_events,
    mark_refresh_attempted,
    was_refresh_recently_attempted,
)

# Verbatim (real, live-captured) NCCN guideline citation — note the absence
# of doi/journal fields, unlike the journal-article example below.
_NCCN_CITATION_EVENT = (
    '{"text": "[[1]]", "reference": {"citation_key": 1, '
    '"reference_text": "National Comprehensive Cancer Network. Melanoma: Cutaneous.", '
    '"reference_detail": {"title": "Melanoma: Cutaneous", '
    '"authors_string": "National Comprehensive Cancer Network", '
    '"publication_info_string": "Updated 2026-09-02", '
    '"publication_date": "2026-09-02", '
    '"url": "https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=77"}, '
    '"source_texts": []}}'
)

# From the official docs: a journal-article citation, showing doi/journal_name/
# journal_short_name/source_texts present (which the NCCN example lacks).
_PSORIASIS_CITATION_EVENT = (
    '{"text": "[[2]]", "reference": {"citation_key": 2, '
    '"reference_text": "Lebwohl M, Ting PT, Koo JY. Psoriasis Treatment: Traditional Therapy. '
    'Annals of the Rheumatic Diseases. 2005;64 Suppl 2:ii83-6. doi:10.1136/ard.2004.030791.", '
    '"reference_detail": {"title": "Psoriasis Treatment: Traditional Therapy", '
    '"authors_string": "Lebwohl M, Ting PT, Koo JY.", '
    '"publication_info_string": "Annals of the Rheumatic Diseases. 2005;64 Suppl 2:ii83-6. doi:10.1136/ard.2004.030791.", '
    '"journal_name": "Annals of the Rheumatic Diseases", '
    '"journal_short_name": "Ann Rheum Dis", '
    '"publication_date": "2005-03-01", '
    '"doi": "10.1136/ard.2004.030791", '
    '"url": "https://pubmed.ncbi.nlm.nih.gov/15708945"}, '
    '"source_texts": ["Even before the recent development of biological agents, a long list '
    'of effective treatments has been available for patients with psoriasis..."]}}'
)

_TABLE_EVENT = '{"table": {"rows": [{"gene": "BRAF", "alteration": "V600E"}]}}'

# A real streaming response has no [DONE] terminator — it just ends.
SSE_STREAM = (
    'data: {"text": "BRAF mutations "}\n\n'
    'data: {"text": "are common in melanoma."}\n\n'
    f"data: {_NCCN_CITATION_EVENT}\n\n"
    f"data: {_PSORIASIS_CITATION_EVENT}\n\n"
    f"data: {_TABLE_EVENT}\n\n"
)


@pytest.fixture
async def _require_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")


def test_parse_sse_events_parses_all_events_with_no_done_marker():
    """There is no [DONE] sentinel in the real API — all 5 events parse
    (2 text deltas + 2 citation events + 1 table event), nothing is skipped
    or mistaken for a stream terminator."""
    events = _parse_sse_events(SSE_STREAM)
    assert len(events) == 5


def test_parse_sse_events_skips_malformed_payload():
    raw = 'data: {"text": "ok"}\n\ndata: not-json\n\n'
    events = _parse_sse_events(raw)
    assert events == [{"text": "ok"}]


def test_iter_sse_payloads_joins_multiline_data_with_newline_not_concatenation():
    """Per the SSE spec, multiple `data:` lines within one event block must be
    joined with "\\n" between them, not concatenated directly. Plain
    concatenation ("".join) would merge "first line" and "second line" into
    "first linesecond line", silently dropping the separator that was part of
    the original payload's semantics — which can turn a JSON payload that was
    validly split across physical lines into something that fails to parse
    or parses to the wrong value."""
    raw = "data: first line\ndata: second line\n\n"
    payloads = _iter_sse_payloads(raw)
    assert payloads == ["first line\nsecond line"]


def test_parse_sse_events_reassembles_multiline_json_payload():
    """A single JSON event split across two `data:` lines (e.g. a
    pretty-printed payload) must reassemble into one JSON object via the
    "\\n" join, not into unparseable or merged text via concatenation."""
    raw = 'data: {"text":\ndata: "hello"}\n\n'
    events = _parse_sse_events(raw)
    assert events == [{"text": "hello"}]


def test_build_analysis_accumulates_text_from_all_events_including_citations():
    """Per the official docs, concatenating every event's `text` field
    produces the full analysis text — including citation-bearing events,
    whose `text` is an inline marker like "[[1]]", not just plain message
    events."""
    events = _parse_sse_events(SSE_STREAM)
    analysis = _build_analysis("What about BRAF?", events)

    assert analysis.question == "What about BRAF?"
    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"


def test_build_analysis_extracts_citations_from_real_nested_shape():
    """Citation fields are nested under event["reference"]["reference_detail"],
    not flat top-level fields — this is the real, confirmed shape."""
    events = _parse_sse_events(SSE_STREAM)
    analysis = _build_analysis("What about BRAF?", events)

    assert len(analysis.citations) == 2
    by_key = {c.citation_key: c for c in analysis.citations}

    nccn = by_key["1"]
    assert nccn.title == "Melanoma: Cutaneous"
    assert nccn.authors == "National Comprehensive Cancer Network"
    assert nccn.journal == ""  # no journal_name/journal_short_name in this fixture
    assert nccn.date == "2026-09-02"
    assert nccn.doi == ""  # no doi in this fixture
    assert nccn.url == "https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=77"
    assert nccn.source_texts == []

    psoriasis = by_key["2"]
    assert psoriasis.title == "Psoriasis Treatment: Traditional Therapy"
    assert psoriasis.authors == "Lebwohl M, Ting PT, Koo JY."
    assert psoriasis.journal == "Annals of the Rheumatic Diseases"  # journal_name preferred
    assert psoriasis.date == "2005-03-01"
    assert psoriasis.doi == "10.1136/ard.2004.030791"
    assert psoriasis.url == "https://pubmed.ncbi.nlm.nih.gov/15708945"
    assert psoriasis.source_texts == [
        "Even before the recent development of biological agents, a long list "
        "of effective treatments has been available for patients with psoriasis..."
    ]


def test_build_analysis_dedupes_repeated_citation_key_merging_source_texts():
    second_occurrence = (
        '{"text": "[[2]]", "reference": {"citation_key": 2, "reference_text": "x", '
        '"reference_detail": {"title": "Psoriasis Treatment: Traditional Therapy"}, '
        '"source_texts": ["A second supporting passage."]}}'
    )
    events = _parse_sse_events(
        f"data: {_PSORIASIS_CITATION_EVENT}\n\n" f"data: {second_occurrence}\n\n"
    )
    analysis = _build_analysis("q", events)

    assert len(analysis.citations) == 1
    citation = analysis.citations[0]
    assert citation.source_texts == [
        "Even before the recent development of biological agents, a long list "
        "of effective treatments has been available for patients with psoriasis...",
        "A second supporting passage.",
    ]


def test_build_analysis_ignores_table_events_without_crashing():
    """`table` events are an intentional v1 limitation — dropped entirely,
    never crash parsing, never pollute accumulated text or citations."""
    events = _parse_sse_events(
        'data: {"text": "before "}\n\n'
        f"data: {_TABLE_EVENT}\n\n"
        'data: {"text": "after"}\n\n'
    )
    analysis = _build_analysis("q", events)

    assert analysis.text == "before after"
    assert analysis.citations == []


@pytest.mark.asyncio
async def test_get_gene_analysis_requires_api_key():
    """On a genuine cache MISS, a live call is about to be made, so an API
    key is genuinely required."""
    client = OpenEvidenceClient(api_key="")
    with pytest.raises(OpenEvidenceConfigurationError):
        await client.get_gene_analysis("BRAF")


@pytest.mark.asyncio
async def test_get_gene_analysis_cache_hit_is_consumable_without_api_key(_require_redis):
    """A genuine Redis cache hit must be returned regardless of whether an
    API key is configured on THIS client instance — a cache entry existing
    does not depend on this process being able to make a NEW live call. It
    may have been warmed by a different process (e.g.
    benchmarks/warm_openevidence_cache.py) that did have a key. Before the
    fix, the API-key check ran before the cache was even consulted, so a
    keyless process could never consume an otherwise-valid cache entry."""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SSE_STREAM)

    # First, warm the cache using a client that DOES have a key.
    warming_client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        await warming_client.get_gene_analysis("BRAF", client=http_client)
    assert len(requests) == 1

    # Now a keyless client must still be able to consume that cache entry —
    # no live call, no OpenEvidenceConfigurationError.
    keyless_client = OpenEvidenceClient(api_key="")
    analysis = await keyless_client.get_gene_analysis("BRAF")

    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"
    assert len(requests) == 1  # still just the one warming call, no new attempt


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
    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"
    assert {c.citation_key for c in analysis.citations} == {"1", "2"}


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
async def test_get_gene_analysis_clean_stream_with_no_done_marker_is_cached(_require_redis):
    """Round 4 fix: there is no [DONE] sentinel in the real API (confirmed
    absent from the official docs and a live capture). A stream that simply
    ends normally — the HTTP body finishes, no transport exception — must be
    treated as a complete, valid, CACHEABLE result. (Round 3 had this
    backwards: it required seeing a literal "[DONE]" payload, which would
    have caused every real production call to be treated as incomplete and
    exhaust its retries.)"""
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=SSE_STREAM)  # ends cleanly, no [DONE]

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        first = await client.get_gene_analysis("BRAF", client=http_client)
        second = await client.get_gene_analysis("BRAF", client=http_client)

    assert first == second
    assert len(first.citations) == 2
    assert len(requests) == 1  # cached after the first successful (DONE-less) call


@pytest.mark.asyncio
async def test_get_gene_analysis_transport_error_retries_and_is_not_cached(_require_redis):
    """A genuinely dropped/reset connection mid-stream is how an incomplete
    response actually surfaces against the real API — httpx itself raises a
    transport exception, which the retry predicate treats as transient. This
    is the "transport-exception-based incompleteness detection" that
    replaces the old (incorrect) [DONE]-sentinel check."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ReadError("connection reset mid-stream", request=request)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(RetryError):
            await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 3  # all retry attempts consumed, never succeeded

    cache_key = "openevidence:" + json.dumps(
        {"gene": "BRAF", "tumor_type": "", "model": settings.openevidence_model},
        sort_keys=True,
    )
    cached = await cache_module._get_client().get(cache_key)
    assert cached is None  # nothing was cached — compute() never returned successfully


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
    assert analysis.text == "BRAF mutations are common in melanoma.[[1]][[2]]"


@pytest.mark.asyncio
async def test_get_gene_analysis_does_not_retry_permanent_4xx():
    """A 401 (bad API key) can never succeed on retry — it must fail fast on
    the first attempt rather than burning the retry budget."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        return httpx.Response(401, text="unauthorized")

    client = OpenEvidenceClient(api_key="bad-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_get_gene_analysis_does_not_retry_on_timeout():
    """OpenEvidence's real-world latency can run into minutes for a complex
    question (a live smoke test ran ~220s without finishing). Retrying a
    slow-but-functioning server would only multiply an already multi-minute
    wait for what's meant to be a quick, best-effort lookup — a timeout must
    fail fast: single attempt, no retry."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ReadTimeout("timed out", request=request)

    client = OpenEvidenceClient(api_key="test-key")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(httpx.ReadTimeout):
            await client.get_gene_analysis("BRAF", client=http_client)

    assert attempts["count"] == 1


@pytest.mark.asyncio
async def test_was_refresh_recently_attempted_reflects_mark_refresh_attempted(_require_redis):
    """The cooldown primitives used by the gene-annotation reuse check to
    bound repeated freshness-driven re-synthesis attempts: unattempted by
    default, True immediately after marking, for a distinct gene/tumor_type
    only (not globally)."""
    assert await was_refresh_recently_attempted("BRAF") is False

    await mark_refresh_attempted("BRAF")

    assert await was_refresh_recently_attempted("BRAF") is True
    # A different gene, or the same gene with a different tumor_type, is a
    # distinct cooldown key — unaffected by BRAF's mark.
    assert await was_refresh_recently_attempted("TP53") is False
    assert await was_refresh_recently_attempted("BRAF", tumor_type="melanoma") is False


@pytest.mark.asyncio
async def test_was_refresh_recently_attempted_expires_after_cooldown(_require_redis, monkeypatch):
    """The cooldown is bounded, not permanent — once
    OPENEVIDENCE_REFRESH_COOLDOWN_SECONDS elapses, another attempt is
    allowed again."""
    import asyncio

    monkeypatch.setattr(settings, "openevidence_refresh_cooldown_seconds", 1)

    await mark_refresh_attempted("BRAF")
    assert await was_refresh_recently_attempted("BRAF") is True

    await asyncio.sleep(1.2)  # let the 1-second TTL genuinely expire
    assert await was_refresh_recently_attempted("BRAF") is False
