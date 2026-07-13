"""Tests for the fusion-level kinase curation export."""

import csv

from src.models.schema import AnnotationResult, GeneAnnotation
from src.pipeline.kinase_curation import (
    build_kinase_fusion_curation_rows,
    write_kinase_fusion_curation_csv,
)


def _result(*annotations: GeneAnnotation) -> AnnotationResult:
    return AnnotationResult(
        run_id="run-1",
        timestamp="2026-07-13T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=len(annotations),
        annotations=list(annotations),
    )


def test_build_kinase_curation_rows_targets_functional_kinase_partner():
    result = _result(
        GeneAnnotation(
            gene="ETV6",
            fusions=["ETV6::NTRK3"],
            gene_class="Transcription factor",
            cancer_associated=True,
            citations=["111"],
        ),
        GeneAnnotation(
            gene="NTRK3",
            fusions=["ETV6::NTRK3"],
            gene_class="Receptor tyrosine kinase",
            signaling_pathways="RAS/MAPK, PI3K/AKT",
            cancer_association_rationale="Recurrent oncogenic fusions with functional evidence",
            cancer_associated_gene_tier="Class I - Driver",
            og_or_tsg="OG",
            gene_summary="NTRK3 fusions can activate kinase signaling.",
            cancer_associated=True,
            citations=["222", "333"],
        ),
    )

    rows = build_kinase_fusion_curation_rows(result)

    assert len(rows) == 1
    row = rows[0]
    assert row.fusion_detected == "ETV6::NTRK3"
    assert row.kinase_included_in_fusion == "NTRK3"
    assert "Fusion partners: ETV6, NTRK3" in row.fusion_metadata
    assert "not provided in input" in row.fusion_metadata
    assert "gene_class: Receptor tyrosine kinase" in row.biologic_characterization_specs
    assert "tier: Class I - Driver" in row.biologic_characterization_specs
    assert row.publication_link == (
        "https://pubmed.ncbi.nlm.nih.gov/222/; https://pubmed.ncbi.nlm.nih.gov/333/"
    )


def test_build_kinase_curation_rows_skips_non_kinase_and_pseudokinase():
    result = _result(
        GeneAnnotation(
            gene="FOO",
            fusions=["FOO::BAR"],
            gene_class="RNA-binding protein",
            citations=["111"],
        ),
        GeneAnnotation(
            gene="BAR",
            fusions=["FOO::BAR"],
            gene_class="Pseudokinase adaptor",
            citations=["222"],
        ),
    )

    assert build_kinase_fusion_curation_rows(result) == []


def test_write_kinase_curation_csv_uses_requested_headers(tmp_path):
    result = _result(
        GeneAnnotation(
            gene="ABL1",
            fusions=["BCR--ABL1"],
            gene_class="Tyrosine kinase",
            cancer_association_rationale="Fusion-driven kinase activation",
            citations=["12345"],
        )
    )
    rows = build_kinase_fusion_curation_rows(result)
    output_path = tmp_path / "kinase_fusions.csv"

    write_kinase_fusion_curation_csv(rows, output_path)

    with open(output_path, newline="") as f:
        written = list(csv.DictReader(f))

    assert written == [
        {
            "Fusion detected": "BCR--ABL1",
            (
                "Fusion meta data (gene transcripts/ genomic/transcriptiomic "
                "breakpoints exons incl etc)"
            ): (
                "Fusion partners: BCR, ABL1; gene transcripts/genomic breakpoints/"
                "transcriptomic breakpoints/exons: not provided in input"
            ),
            "Kinase included in fusion": "ABL1",
            "Biologic characterization specs": (
                "ABL1: gene_class: Tyrosine kinase; rationale: "
                "Fusion-driven kinase activation"
            ),
            "publication link": "https://pubmed.ncbi.nlm.nih.gov/12345/",
        }
    ]
