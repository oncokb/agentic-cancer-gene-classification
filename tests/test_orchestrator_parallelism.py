"""Tests for per-gene pipeline orchestration."""

from __future__ import annotations

import asyncio

from src.models.schema import GeneAnnotation, ResolvedGene
from src.pipeline import orchestrator


async def test_run_pipeline_annotates_genes_concurrently(monkeypatch):
    active = 0
    max_active = 0
    seen = []

    async def fake_normalize_fusions(fusions):
        return {
            "A": (
                ResolvedGene(input_symbol="A", canonical_symbol="A", resolved=True),
                ["A::B"],
            ),
            "B": (
                ResolvedGene(input_symbol="B", canonical_symbol="B", resolved=True),
                ["A::B"],
            ),
            "C": (
                ResolvedGene(input_symbol="C", canonical_symbol="C", resolved=True),
                ["C::D"],
            ),
        }

    async def fake_annotate_gene(gene, fusions, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        seen.append(gene)
        await asyncio.sleep(0.01)
        active -= 1
        return GeneAnnotation(
            gene=gene,
            fusions=fusions,
            in_oncokb=True,
            insufficient_evidence=False,
        )

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator.settings, "max_gene_annotation_concurrency", 2)

    result = await orchestrator.run_pipeline(["A::B", "C::D"])

    assert max_active == 2
    assert set(seen) == {"A", "B", "C"}
    assert [annotation.gene for annotation in result.annotations] == ["A", "B", "C"]


async def test_run_pipeline_stops_early_on_model_capacity_error(monkeypatch):
    async def fake_normalize_fusions(fusions):
        return {
            "A": (
                ResolvedGene(input_symbol="A", canonical_symbol="A", resolved=True),
                ["A::B"],
            ),
            "B": (
                ResolvedGene(input_symbol="B", canonical_symbol="B", resolved=True),
                ["A::B"],
            ),
        }

    async def fake_annotate_gene(gene, fusions, **kwargs):
        if gene == "A":
            return GeneAnnotation(
                gene=gene,
                fusions=fusions,
                in_oncokb=True,
                insufficient_evidence=False,
            )
        await asyncio.sleep(0.01)
        raise RuntimeError("insufficient tokens for document retrieval")

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator.settings, "max_gene_annotation_concurrency", 2)

    result = await orchestrator.run_pipeline(["A::B"])

    assert [annotation.gene for annotation in result.annotations] == ["A"]
    assert result.run_error is not None
    assert "insufficient tokens" in result.run_error
