"""Tests for OpenEvidence cache warmup (src.pipeline.openevidence_warmup).

Mirrors tests/test_literature_warmup.py's pattern for gene-map fan-out and
per-gene error surfacing. The cache-population and idempotent-rerun tests
additionally exercise OpenEvidenceClient.get_gene_analysis's real
cached_call/Redis path (skipped cleanly if Redis isn't reachable) by
monkeypatching only the underlying HTTP call (_post_streaming_analysis), not
the client itself — this is what actually proves warmup populates the real
Redis cache and that re-running against an already-warm cache makes no
duplicate live call.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.config import settings
from src.models.schema import ResolvedGene
from src.pipeline import cache as cache_module
from src.pipeline import openevidence as openevidence_module
from src.pipeline import openevidence_warmup
from src.pipeline.openevidence import OpenEvidenceClient

_SSE_STREAM = 'data: {"text": "Some supplementary evidence."}\n\n'


def _resolved_gene(gene: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True)


@pytest.fixture
async def _require_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")


class _FakeAnalysis:
    def __init__(self, citations=None):
        self.citations = citations or []


async def test_warm_openevidence_cache_fans_out_over_gene_map(monkeypatch):
    async def fake_normalize_fusions(inputs):
        assert inputs == ["TP53::BRAF"]
        return {
            "BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"]),
            "TP53": (_resolved_gene("TP53"), ["TP53::BRAF"]),
        }

    seen = []

    class FakeClient:
        async def get_gene_analysis(self, gene, tumor_type=None, fusion=None):
            seen.append((gene, tumor_type))
            return _FakeAnalysis(citations=[object()])

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)

    report = await openevidence_warmup.warm_openevidence_cache(
        ["TP53::BRAF"], concurrency=2, client=FakeClient()
    )

    assert report["inputs_processed"] == 1
    assert report["genes_total"] == 2
    assert report["genes_warmed"] == 2
    assert report["genes_failed"] == 0
    assert sorted(item["gene"] for item in report["warmed"]) == ["BRAF", "TP53"]
    assert all(item["citation_count"] == 1 for item in report["warmed"])
    assert sorted(seen) == [("BRAF", None), ("TP53", None)]


async def test_warm_openevidence_cache_reports_gene_errors(monkeypatch):
    async def fake_normalize_fusions(_inputs):
        return {
            "BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"]),
            "TP53": (_resolved_gene("TP53"), ["TP53::BRAF"]),
        }

    class FakeClient:
        async def get_gene_analysis(self, gene, tumor_type=None, fusion=None):
            if gene == "TP53":
                raise RuntimeError("OpenEvidence unavailable")
            return _FakeAnalysis()

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)

    report = await openevidence_warmup.warm_openevidence_cache(
        ["TP53::BRAF"], concurrency=1, client=FakeClient()
    )

    assert report["genes_warmed"] == 1
    assert report["genes_failed"] == 1
    assert report["errors"] == [{"gene": "TP53", "error": "OpenEvidence unavailable"}]


async def test_warm_openevidence_cache_uses_own_concurrency_not_annotation_gene_concurrency(monkeypatch):
    """Warmup must gate on its OWN OPENEVIDENCE_WARMUP_CONCURRENCY setting,
    never ANNOTATION_GENE_CONCURRENCY — proven by setting them to different
    values and observing the warmup semaphore actually allows the wider
    fan-out."""

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (_resolved_gene("BRAF"), ["BRAF"]),
            "TP53": (_resolved_gene("TP53"), ["TP53"]),
            "ALK": (_resolved_gene("ALK"), ["ALK"]),
        }

    active = 0
    max_active = 0

    class FakeClient:
        async def get_gene_analysis(self, gene, tumor_type=None, fusion=None):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return _FakeAnalysis()

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(openevidence_warmup.settings, "annotation_gene_concurrency", 1)
    monkeypatch.setattr(openevidence_warmup.settings, "openevidence_warmup_concurrency", 3)

    report = await openevidence_warmup.warm_openevidence_cache(["BRAF"], client=FakeClient())

    assert report["genes_warmed"] == 3
    # If the warmup semaphore were (incorrectly) gated by
    # annotation_gene_concurrency (1), max_active would be 1.
    assert max_active == 3


async def test_warm_openevidence_cache_populates_redis_cache(_require_redis, monkeypatch):
    """Exercises the real OpenEvidenceClient.get_gene_analysis cached_call
    path (not a mocked client) to prove warmup actually writes into Redis."""

    async def fake_normalize_fusions(inputs):
        return {"BRAF": (_resolved_gene("BRAF"), ["BRAF"])}

    call_count = {"n": 0}

    async def fake_post_streaming_analysis(question, api_key, client):
        call_count["n"] += 1
        return _SSE_STREAM

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(openevidence_module, "_post_streaming_analysis", fake_post_streaming_analysis)

    real_client = OpenEvidenceClient(api_key="test-key")
    report = await openevidence_warmup.warm_openevidence_cache(["BRAF"], client=real_client)

    assert report["genes_warmed"] == 1
    assert call_count["n"] == 1

    cache_key = "openevidence:" + json.dumps(
        {"gene": "BRAF", "tumor_type": "", "model": settings.openevidence_model},
        sort_keys=True,
    )
    cached = await cache_module._get_client().get(cache_key)
    assert cached is not None


async def test_warm_openevidence_cache_rerun_against_warm_cache_is_safe_noop(_require_redis, monkeypatch):
    """Re-running warmup against a gene that's already warm must not make a
    second live call — it should be a Redis cache hit, exactly like a live
    annotation request would get."""

    async def fake_normalize_fusions(inputs):
        return {"BRAF": (_resolved_gene("BRAF"), ["BRAF"])}

    call_count = {"n": 0}

    async def fake_post_streaming_analysis(question, api_key, client):
        call_count["n"] += 1
        return _SSE_STREAM

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(openevidence_module, "_post_streaming_analysis", fake_post_streaming_analysis)

    real_client = OpenEvidenceClient(api_key="test-key")

    first_report = await openevidence_warmup.warm_openevidence_cache(["BRAF"], client=real_client)
    second_report = await openevidence_warmup.warm_openevidence_cache(["BRAF"], client=real_client)

    assert first_report["genes_warmed"] == 1
    assert second_report["genes_warmed"] == 1
    assert second_report["genes_failed"] == 0
    assert call_count["n"] == 1  # second warmup pass was a cache hit, no duplicate live call


async def test_warm_openevidence_cache_warms_fusion_specific_question_for_fusion_gene(
    _require_redis, monkeypatch
):
    """Regression test: get_gene_analysis's cache key does not vary by
    fusion (by design — see openevidence.py's _cache_key), so it holds
    whatever question was actually asked when the entry was populated. If
    warmup asked the generic "is ALK an oncogene..." question here while a
    live annotation request for the same ALK::EML4 fusion would ask the
    fusion-specific "is the EML4::ALK fusion oncogenic..." question, the
    live request would silently get a cache HIT on warmup's stale
    generic-question answer and never actually compute (or cache) the
    fusion-specific one — with no visible error.

    Warms via the warmup path with the same raw fusion input a live request
    would submit, then simulates the live annotate path's lookup for that
    same gene+fusion and confirms it's a cache HIT on the FUSION-SPECIFIC
    question, not a silent hit on a generic-question cache entry. Fails
    before threading `fusion` through warm_one() (the generic question would
    have been sent/cached instead), passes after.
    """

    async def fake_normalize_fusions(inputs):
        assert inputs == ["EML4::ALK"]
        return {"ALK": (_resolved_gene("ALK"), ["EML4::ALK"])}

    questions_sent = []

    async def fake_post_streaming_analysis(question, api_key, client):
        questions_sent.append(question)
        return _SSE_STREAM

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(openevidence_module, "_post_streaming_analysis", fake_post_streaming_analysis)

    real_client = OpenEvidenceClient(api_key="test-key")
    report = await openevidence_warmup.warm_openevidence_cache(["EML4::ALK"], client=real_client)
    assert report["genes_warmed"] == 1

    # Exactly one live HTTP call so far, and it must have been the
    # fusion-specific question — not the generic "is ALK an oncogene..."
    # question a fusion-unaware warmup would have sent instead.
    assert len(questions_sent) == 1
    assert "EML4::ALK" in questions_sent[0]

    # The live annotate path's lookup for the same gene+fusion must be a
    # cache HIT (no second live call) returning that same fusion-specific
    # analysis, not a mismatched generic one.
    live_analysis = await real_client.get_gene_analysis("ALK", fusion="EML4::ALK")
    assert len(questions_sent) == 1  # cache hit, no new live call
    assert live_analysis.question == questions_sent[0]
    assert "EML4::ALK" in live_analysis.question
