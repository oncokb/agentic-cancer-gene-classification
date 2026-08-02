"""Tests for latency optimization plumbing."""

import asyncio

from src.models.schema import GeneAnnotation, ResolvedGene
from src.pipeline import orchestrator


async def test_run_pipeline_parallelizes_genes_and_reports_timings(monkeypatch):
    active = 0
    max_active = 0
    completed = []

    async def fake_normalize_fusions(inputs):
        return {
            "AAA": (
                ResolvedGene(input_symbol="AAA", canonical_symbol="AAA", resolved=True),
                ["AAA::BBB"],
            ),
            "BBB": (
                ResolvedGene(input_symbol="BBB", canonical_symbol="BBB", resolved=True),
                ["AAA::BBB"],
            ),
        }

    async def fake_annotate_gene(*, gene, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return GeneAnnotation(
            gene=gene,
            cancer_associated=True,
            evidence_support_score=0.9,
            timings_ms={"total": 10.0},
        )

    async def on_annotation(annotation):
        completed.append(annotation.gene)

    monkeypatch.setattr(orchestrator.settings, "annotation_gene_concurrency", 2)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await orchestrator.run_pipeline(["AAA::BBB"], on_annotation=on_annotation)

    assert max_active == 2
    assert sorted(completed) == ["AAA", "BBB"]
    assert result.genes_annotated == 2
    assert result.timings_ms["normalization"] >= 0
    assert result.timings_ms["annotation"] >= 0
    assert result.timings_ms["total"] >= 0
