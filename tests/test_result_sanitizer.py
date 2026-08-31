from __future__ import annotations

from src.models.schema import (
    AnnotationResult,
    EvidenceCard,
    FusionEvidenceCard,
    FusionEvidenceResult,
    GeneAnnotation,
    SupportingQuote,
)
from src.pipeline import result_sanitizer
from src.pipeline.result_sanitizer import sanitize_annotation_result


async def test_sanitize_annotation_result_removes_retracted_and_bad_fusion_evidence(monkeypatch):
    async def fake_find_retracted_pmids(_pmids):
        return {"12345"}

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", fake_find_retracted_pmids)

    result = AnnotationResult(
        run_id="run-1",
        timestamp="2026-08-31T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=1,
        annotations=[
            GeneAnnotation(
                gene="GENE",
                citations=["12345", "67890"],
                retrieved_pmids=["12345", "67890"],
                supporting_quotes=[
                    SupportingQuote(pmid="12345", quote="Retracted support."),
                    SupportingQuote(pmid="67890", quote="Valid support."),
                ],
                evidence_cards=[
                    EvidenceCard(pmid="12345", title="Retracted paper"),
                    EvidenceCard(pmid="67890", title="Valid paper"),
                ],
            )
        ],
        fusion_evidence=[
            FusionEvidenceResult(
                fusion="PLAGL1::MYB",
                retrieved_count=2,
                pmids=["21901247", "222"],
                evidence_cards=[
                    FusionEvidenceCard(
                        fusion="PLAGL1::MYB",
                        pmid="21901247",
                        title="Studies of genomic imbalances and the MYB-NFIB gene fusion",
                        quote="PLAGL1 methylation was also evaluated.",
                    ),
                    FusionEvidenceCard(
                        fusion="PLAGL1::MYB",
                        pmid="222",
                        title="Recurrent PLAGL1-MYB fusion in leukemia",
                        quote="The PLAGL1-MYB fusion was detected by RNA sequencing.",
                    ),
                ],
            )
        ],
    )

    sanitized, changed = await sanitize_annotation_result(result)

    assert changed is True
    assert sanitized.annotations[0].citations == ["67890"]
    assert sanitized.annotations[0].retrieved_pmids == ["67890"]
    assert [quote.pmid for quote in sanitized.annotations[0].supporting_quotes] == ["67890"]
    assert [card.pmid for card in sanitized.annotations[0].evidence_cards] == ["67890"]
    assert sanitized.annotations[0].quality_flags[0].code == "retracted_citations_removed"
    assert sanitized.fusion_evidence[0].pmids == ["222"]
    assert [card.pmid for card in sanitized.fusion_evidence[0].evidence_cards] == ["222"]
