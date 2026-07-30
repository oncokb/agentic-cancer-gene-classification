from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.models.schema import GeneAnnotation, ResolvedGene
from src.pipeline import orchestrator
from src.pipeline.orchestrator import run_pipeline


class FakeGeneStore:
    def __init__(self, cached=None):
        self.cached = cached or {}
        self.saved = []
        self.checked = []

    async def get_gene_annotation(self, gene):
        return self.cached.get(gene)

    async def save_gene_annotation(self, annotation, updated_at):
        self.saved.append((annotation, updated_at))

    async def mark_gene_pubmed_checked(self, gene, checked_at, annotation=None):
        self.checked.append((gene, checked_at, annotation))


def _resolved_gene(gene: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True)


async def test_run_pipeline_reuses_fresh_high_support_cached_annotation(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=10)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fail_if_called(**_kwargs):
        raise AssertionError("fresh cache should avoid recomputation")

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].gene == "BRAF"
    assert result.annotations[0].fusions == ["TP53::BRAF"]
    assert result.annotations[0].cache_status == "reused"
    assert result.annotations[0].cache_reason == "fresh_high_evidence_support"
    assert store.saved == []


async def test_run_pipeline_reuses_stale_cache_when_pubmed_has_no_new_pmids(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=80)
    checked_at = datetime.now(timezone.utc) - timedelta(days=40)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": checked_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_recent_pmids(*_args, **_kwargs):
        return []

    async def fail_if_called(**_kwargs):
        raise AssertionError("clean PubMed freshness check should avoid recomputation")

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "search_recent_pubmed_pmids", fake_recent_pmids)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "reused"
    assert result.annotations[0].cache_reason == "no_new_pubmed_pmids_since_last_check"
    assert store.checked[0][0] == "BRAF"


async def test_run_pipeline_refreshes_stale_cache_when_pubmed_has_new_pmids(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=80)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_recent_pmids(*_args, **_kwargs):
        return ["99999999"]

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=False,
            cancer_associated=True,
            citations=["99999999"],
            insufficient_evidence=False,
            evidence_support_score=0.6,
            evidence_support_explanation="Medium support.",
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "search_recent_pubmed_pmids", fake_recent_pmids)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].cache_reason == "cache_miss_or_stale"
    assert store.saved[0][0].gene == "BRAF"


async def test_run_pipeline_force_refresh_bypasses_cache(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=True,
        evidence_support_score=1.0,
        evidence_support_explanation="OncoKB cached.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=True,
            evidence_support_score=0.8,
            evidence_support_explanation="Refreshed.",
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(["TP53::BRAF"], run_store=store, force_refresh=True)

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].cache_reason == "force_refresh"
    assert store.saved[0][0].gene == "BRAF"
