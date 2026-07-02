"""Tests for citation precision controls."""

from src.models.schema import LiteratureRecord
from src.pipeline.citation_precision import filter_and_rank_citations
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import _verify_citations, build_gene_annotation


async def test_selection_preserves_model_relevance_order(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"selected_pmids": ["333", "111", "333", "999", "222"]}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid="111", title="First", abstract="Abstract 1"),
        LiteratureRecord(pmid="222", title="Second", abstract="Abstract 2"),
        LiteratureRecord(pmid="333", title="Third", abstract="Abstract 3"),
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert [record.pmid for record in selected] == ["333", "111"]


async def test_selection_can_abstain_when_no_papers_are_relevant(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"selected_pmids": []}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid=str(i), title=f"Paper {i}", abstract=f"Abstract {i}")
        for i in range(10)
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert selected == []


def test_verify_citations_deduplicates_rejects_unretrieved_and_caps():
    records = [
        LiteratureRecord(pmid="111", title="GENE cancer", abstract="GENE knockdown in cancer."),
        LiteratureRecord(pmid="222", title="GENE carcinoma", abstract="GENE proliferation."),
        LiteratureRecord(pmid="333", title="GENE tumor", abstract="GENE invasion."),
        LiteratureRecord(pmid="444", title="GENE oncology", abstract="GENE survival."),
    ]
    verified = _verify_citations(
        "GENE",
        ["111", "222", "222", "999", "333"],
        records,
        max_citations=2,
    )

    assert verified == ["111", "222"]


def test_filter_and_rank_citations_rejects_same_symbol_lncRNA_ambiguity():
    records = [
        LiteratureRecord(
            pmid="bad",
            title="The lncRNA RP1 promotes breast cancer progression",
            abstract="lncRNA RP1 increases tumor proliferation and invasion.",
        ),
        LiteratureRecord(
            pmid="good",
            title="RP1 axonemal microtubule associated gene in cancer sequencing",
            abstract="RP1 mutation was evaluated in a cancer cohort.",
        ),
    ]

    citations = filter_and_rank_citations(
        gene="RP1",
        emitted_citations=["bad", "good"],
        records=records,
        max_citations=4,
        gene_identity="HGNC name: RP1 axonemal microtubule associated; Locus type: gene with protein product",
    )

    assert citations == ["good"]


def test_filter_and_rank_citations_prefers_direct_cancer_support():
    records = [
        LiteratureRecord(
            pmid="weak",
            title="GENE appears in a broad expression signature",
            abstract="A panel of differentially expressed genes was reported.",
        ),
        LiteratureRecord(
            pmid="strong",
            title="GENE knockdown suppresses carcinoma proliferation",
            abstract="GENE knockdown reduced tumor invasion and xenograft growth.",
        ),
    ]

    citations = filter_and_rank_citations(
        gene="GENE",
        emitted_citations=["weak", "strong"],
        records=records,
        max_citations=2,
    )

    assert citations == ["strong"]


def test_build_gene_annotation_downgrades_non_oncokb_class_i_call():
    annotation = build_gene_annotation(
        gene="RFX7",
        fusions=["RFX7::LMTK2"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[],
        synthesis_result={
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_associated_gene_tier": "Class I - Driver",
            "og_or_tsg": "TSG",
            "confidence": 0.9,
        },
    )

    assert annotation.cancer_associated_gene_tier == "Class II - Likely Driver"
    assert annotation.og_or_tsg == "TSG"


def test_build_gene_annotation_clears_classification_when_insufficient():
    annotation = build_gene_annotation(
        gene="RP1",
        fusions=["RP1::SPIDR"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[],
        synthesis_result={
            "cancer_associated": False,
            "insufficient_evidence": True,
            "cancer_associated_gene_tier": "Class II - Likely Driver",
            "og_or_tsg": "OG",
            "confidence": 0.0,
        },
    )

    assert annotation.cancer_associated_gene_tier is None
    assert annotation.og_or_tsg is None
