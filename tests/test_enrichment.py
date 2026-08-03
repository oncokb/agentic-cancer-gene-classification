from src.models.schema import GeneAnnotation, LiteratureRecord
from src.pipeline import enrichment


async def test_enrich_gene_annotation_runs_full_synthesis_and_preserves_cache(monkeypatch):
    original = GeneAnnotation(
        gene="TP53",
        fusions=["TP53::BRAF"],
        in_oncokb=None,
        cancer_associated=True,
        cancer_association_rationale="Core rationale.",
        gene_summary="Core summary.",
        citations=["1"],
        cache_status="reused",
        cache_reason="fresh_final_annotation",
        cached_at="2026-08-03T00:00:00+00:00",
    )
    records = [
        LiteratureRecord(pmid="1", title="Paper 1", abstract="TP53 cancer"),
        LiteratureRecord(pmid="2", title="Paper 2", abstract="TP53 pathway"),
    ]

    async def fake_retrieve_literature(gene, fusions, **kwargs):
        assert gene == "TP53"
        assert fusions == ["TP53::BRAF"]
        return records, 1

    async def fake_check_oncokb_membership(gene):
        assert gene == "TP53"
        return True

    def fake_prevalence(gene):
        assert gene == "TP53"
        return "- Lung adenocarcinoma (mutation)"

    async def fake_select(gene, selected_records, max_papers, **kwargs):
        assert gene == "TP53"
        return selected_records[:1]

    async def fake_synthesize_gene_annotation(**kwargs):
        assert kwargs["mode"] == "full"
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Expanded rationale.",
            "cancer_type_prevalence": "- Lung adenocarcinoma (mutation)",
            "gene_class": "Tumor suppressor",
            "signaling_pathways": "p53 pathway",
            "gene_summary": "Expanded TP53 summary (PMID 1).",
            "citations": ["1"],
            "supporting_quotes": [{"pmid": "1", "quote": "TP53 cancer"}],
        }

    monkeypatch.setattr(enrichment, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(enrichment, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(enrichment, "get_msk_genie_prevalence", fake_prevalence)
    monkeypatch.setattr(enrichment, "select_papers_for_synthesis", fake_select)
    monkeypatch.setattr(enrichment, "synthesize_gene_annotation", fake_synthesize_gene_annotation)

    enriched = await enrichment.enrich_gene_annotation(original)

    assert enriched.gene == "TP53"
    assert enriched.fusions == ["TP53::BRAF"]
    assert enriched.in_oncokb is True
    assert enriched.cancer_association_rationale == "Expanded rationale."
    assert enriched.gene_class == "Tumor suppressor"
    assert enriched.signaling_pathways == "p53 pathway"
    assert enriched.supporting_quotes[0].quote == "TP53 cancer"
    assert enriched.cache_status == "reused"
    assert enriched.cache_reason == "fresh_final_annotation"
    assert enriched.cached_at == "2026-08-03T00:00:00+00:00"
    assert enriched.timings_ms["total"] >= 0


async def test_enrich_gene_annotations_streams_progress(monkeypatch):
    completed = []

    async def fake_enrich_gene_annotation(annotation, **kwargs):
        return GeneAnnotation(
            gene=annotation.gene,
            fusions=annotation.fusions,
            cancer_associated=True,
            gene_class="Enriched",
        )

    async def on_annotation(annotation):
        completed.append(annotation.gene)

    monkeypatch.setattr(enrichment, "enrich_gene_annotation", fake_enrich_gene_annotation)

    result = await enrichment.enrich_gene_annotations(
        [
            GeneAnnotation(gene="BRAF", fusions=["TP53::BRAF"]),
            GeneAnnotation(gene="TP53", fusions=["TP53::BRAF"]),
        ],
        on_annotation=on_annotation,
    )

    assert sorted(completed) == ["BRAF", "TP53"]
    assert [annotation.gene for annotation in result] == ["BRAF", "TP53"]
