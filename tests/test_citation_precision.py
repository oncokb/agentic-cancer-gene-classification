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


def test_build_gene_annotation_sets_cancer_associated_from_synthesis():
    annotation = build_gene_annotation(
        gene="RFX7",
        fusions=["RFX7::LMTK2"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[],
        synthesis_result={
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Recurrent somatic mutations in lymphoma.",
        },
    )

    assert annotation.cancer_associated is True
    assert annotation.cancer_association_rationale == "Recurrent somatic mutations in lymphoma."
    assert annotation.insufficient_evidence is False


def test_build_gene_annotation_propagates_insufficient_evidence():
    annotation = build_gene_annotation(
        gene="RP1",
        fusions=["RP1::SPIDR"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[],
        synthesis_result={
            "cancer_associated": False,
            "insufficient_evidence": True,
        },
    )

    assert annotation.cancer_associated is False
    assert annotation.insufficient_evidence is True


def test_build_gene_annotation_adds_lazy_evidence_cards_and_quality_flags():
    annotation = build_gene_annotation(
        gene="TP53",
        fusions=["TP53::BRAF"],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[
            LiteratureRecord(
                pmid="12345",
                title="TP53 mutation predicts cancer prognosis",
                abstract="Patient cohort data link TP53 mutation to cancer prognosis.",
                journal="J Clin Oncol",
                publication_types=["Journal Article"],
            )
        ],
        synthesis_result={
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Context-dependent cancer association.",
            "gene_summary": "TP53 has context-dependent cancer evidence (PMID 12345).",
            "citations": ["12345"],
            "supporting_quotes": [{"pmid": "12345", "quote": "Patient cohort data."}],
            "_synthesis_escalated": True,
            "_synthesis_escalation_reason": "too_few_verified_citations",
        },
        retrieval_tier=2,
        mode="full",
    )

    assert annotation.evidence_cards[0].pmid == "12345"
    assert annotation.evidence_cards[0].evidence_type == "clinical"
    assert annotation.evidence_cards[0].quote == "Patient cohort data."
    assert {flag.code for flag in annotation.quality_flags} == {
        "tier2_retrieval_used",
        "deep_model_escalated",
        "contradictory_evidence_detected",
    }


def test_build_gene_annotation_omits_evidence_cards_in_core_mode():
    annotation = build_gene_annotation(
        gene="TP53",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[
            LiteratureRecord(
                pmid="12345",
                title="TP53 mutation",
                abstract="TP53 mutation in cancer.",
            )
        ],
        synthesis_result={
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Mutation evidence.",
            "gene_summary": "TP53 mutation evidence (PMID 12345).",
            "citations": ["12345"],
        },
        mode="core",
    )

    assert annotation.evidence_cards == []
