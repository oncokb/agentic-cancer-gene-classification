"""Tests for full annotation result spreadsheet export."""

from __future__ import annotations

import csv

from src.models.schema import AnnotationResult, GeneAnnotation
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
            cancer_associated_gene_tier="Class I - Driver",
            og_or_tsg="OG",
            cancer_type_prevalence="Melanoma",
            gene_class="Serine/threonine kinase",
            signaling_pathways="MAPK",
            gene_summary="BRAF is a cancer-associated kinase.",
            citations=["12345", "67890", "12345"],
            date_annotated="7/13/26",
            retrieval_count=12,
            insufficient_evidence=False,
            confidence=0.91,
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
            "cancer_associated_gene_tier": "Class I - Driver",
            "og_or_tsg": "OG",
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
            "confidence": "0.91",
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
