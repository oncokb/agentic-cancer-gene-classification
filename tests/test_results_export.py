"""Tests for full annotation result spreadsheet export."""

from __future__ import annotations

import csv

from src.models.schema import (
    AnnotationResult,
    ClinicalActionability,
    ClinicalActionabilityScoreComponent,
    GeneAnnotation,
)
from src.pipeline.results_export import (
    ANNOTATION_RESULTS_CSV_HEADERS,
    build_annotation_results_csv_rows,
    write_annotation_results_csv,
)


def _result(*annotations: GeneAnnotation) -> AnnotationResult:
    return AnnotationResult(
        run_id="run-1",
        timestamp="2026-07-13T00:00:00Z",
        fusions_processed=1,
        genes_annotated=len(annotations),
        annotations=list(annotations),
    )


def test_build_annotation_results_csv_rows_flattens_gene_annotations():
    result = _result(
        GeneAnnotation(
            gene="BRAF",
            fusions=["TP53::BRAF", "TP53::BRAF"],
            in_oncokb=True,
            cancer_associated=True,
            cancer_association_rationale="Known driver kinase.",
            cancer_type_prevalence="Melanoma",
            gene_class="Serine/threonine kinase",
            signaling_pathways="MAPK",
            gene_summary="BRAF is a cancer-associated kinase.",
            citations=["12345", "67890", "12345"],
            date_annotated="7/13/26",
            retrieval_count=12,
            insufficient_evidence=False,
            evidence_support_score=0.91,
            evidence_support_explanation="Strong support from verified citations.",
            clinical_actionability=ClinicalActionability(
                confidence_score=0.9,
                summary="High-confidence therapeutic precedent for BRAF.",
                confidence_explanation="Clinical actionability confidence 0.90.",
                pmids=["12345"],
                score_components=[
                    ClinicalActionabilityScoreComponent(
                        code="clinical_evidence",
                        label="Direct human clinical evidence",
                        delta=0.35,
                        pmids=["12345"],
                        detail="Patient cohort evidence.",
                    )
                ],
            ),
            cache_status="reused",
            cache_reason="fresh_high_evidence_support",
            cached_at="2026-07-13T00:00:00+00:00",
            last_pubmed_checked_at="2026-07-20T00:00:00+00:00",
        )
    )

    rows = build_annotation_results_csv_rows(result)

    assert rows == [
        {
            "gene": "BRAF",
            "fusions": "TP53::BRAF",
            "in_oncokb": "TRUE",
            "cancer_associated": "TRUE",
            "cancer_association_rationale": "Known driver kinase.",
            "cancer_type_prevalence": "Melanoma",
            "gene_class": "Serine/threonine kinase",
            "signaling_pathways": "MAPK",
            "gene_summary": "BRAF is a cancer-associated kinase.",
            "citations": "12345; 67890",
            "publication_links": (
                "https://pubmed.ncbi.nlm.nih.gov/12345/; "
                "https://pubmed.ncbi.nlm.nih.gov/67890/"
            ),
            "date_annotated": "7/13/26",
            "retrieval_count": "12",
            "insufficient_evidence": "FALSE",
            "evidence_support_score": "0.91",
            "evidence_support_explanation": "Strong support from verified citations.",
            "clinical_actionability_score": "0.9",
            "clinical_actionability_summary": "High-confidence therapeutic precedent for BRAF.",
            "clinical_actionability_pmids": "12345",
            "clinical_actionability_explanation": "Clinical actionability confidence 0.90.",
            "clinical_actionability_score_components": (
                "+0.35 Direct human clinical evidence. Patient cohort evidence. PMID(s): 12345"
            ),
            "quality_flags": "",
            "evidence_card_count": "0",
            "cache_status": "reused",
            "cache_reason": "fresh_high_evidence_support",
            "cached_at": "2026-07-13T00:00:00+00:00",
            "last_pubmed_checked_at": "2026-07-20T00:00:00+00:00",
            "error": "",
        }
    ]


def test_write_annotation_results_csv_preserves_headers_and_blank_unknowns(tmp_path):
    output_path = tmp_path / "annotation_results.csv"
    result = _result(
        GeneAnnotation(
            gene="UNKNOWN",
            fusions=["A::UNKNOWN"],
            in_oncokb=None,
            cancer_associated=None,
            citations=[],
            date_annotated="7/13/26",
            insufficient_evidence=True,
            error="unresolvable",
        )
    )

    write_annotation_results_csv(result, output_path)

    with open(output_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    assert reader.fieldnames == ANNOTATION_RESULTS_CSV_HEADERS
    assert rows[0]["gene"] == "UNKNOWN"
    assert rows[0]["in_oncokb"] == ""
    assert rows[0]["cancer_associated"] == ""
    assert rows[0]["citations"] == ""
    assert rows[0]["publication_links"] == ""
    assert rows[0]["insufficient_evidence"] == "TRUE"
    assert rows[0]["error"] == "unresolvable"
