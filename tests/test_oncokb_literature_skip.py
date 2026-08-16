from __future__ import annotations

from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import orchestrator


async def test_oncokb_positive_gene_skips_literature_and_synthesis(monkeypatch):
    async def fake_check_oncokb_membership(gene, lookup=None):
        return True

    async def fail_retrieve_literature(*args, **kwargs):
        raise AssertionError("PubMed retrieval should be skipped")

    async def fail_select_papers(*args, **kwargs):
        raise AssertionError("paper selection should be skipped")

    async def fail_synthesize(*args, **kwargs):
        raise AssertionError("LLM synthesis should be skipped")

    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fail_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fail_select_papers)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", fail_synthesize)

    annotation = await orchestrator._annotate_gene(
        gene="BRAF",
        fusions=["BRAF::TP53"],
        resolved_gene=ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True),
        unresolvable=False,
        skip_literature_for_oncokb=True,
    )

    assert annotation.in_oncokb is True
    assert annotation.cancer_associated is True
    assert annotation.retrieval_count == 0
    assert annotation.retrieved_pmids == []
    assert annotation.citations == []
    assert annotation.cache_status == "bypassed"
    assert annotation.cache_reason == "oncokb_literature_skip"
    assert annotation.quality_flags[0].code == "literature_retrieval_skipped"
    assert "literature_retrieval" not in annotation.timings_ms
    assert "synthesis" not in annotation.timings_ms


async def test_oncokb_negative_gene_still_runs_literature_retrieval(monkeypatch):
    calls = []

    async def fake_check_oncokb_membership(gene, lookup=None):
        calls.append("oncokb")
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        calls.append("literature")
        return (
            [
                LiteratureRecord(
                    pmid="123",
                    title="GENE cancer",
                    abstract="GENE was studied in cancer.",
                    publication_types=["Journal Article"],
                )
            ],
            1,
        )

    async def fake_select_papers(*args, **kwargs):
        calls.append("selection")
        return args[1]

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        calls.append("synthesis")
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "GENE has retrieved cancer evidence (PMID 123).",
            "citations": ["123"],
        }

    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation)

    annotation = await orchestrator._annotate_gene(
        gene="GENE",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="GENE", canonical_symbol="GENE", resolved=True),
        unresolvable=False,
        skip_literature_for_oncokb=True,
    )

    assert calls == ["oncokb", "literature", "selection", "synthesis"]
    assert annotation.in_oncokb is False
    assert annotation.retrieval_count == 1
    assert annotation.citations == ["123"]


async def test_skipped_oncokb_annotation_is_not_saved_to_gene_cache(monkeypatch):
    saved_annotations = []

    class FakeRunStore:
        async def get_gene_annotation(self, gene, tumor_type=None):
            return None

        async def save_gene_annotation(self, annotation, updated_at, tumor_type=None):
            saved_annotations.append(annotation)

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (
                ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True),
                ["BRAF"],
            )
        }

    async def fake_check_oncokb_membership(gene, lookup=None):
        return True

    async def fail_retrieve_literature(*args, **kwargs):
        raise AssertionError("PubMed retrieval should be skipped")

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fail_retrieve_literature)

    result = await orchestrator.run_pipeline(
        ["BRAF"],
        run_store=FakeRunStore(),
        skip_literature_for_oncokb=True,
    )

    assert result.annotations[0].cache_reason == "oncokb_literature_skip"
    assert saved_annotations == []
