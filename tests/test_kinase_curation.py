"""Tests for the fusion-level kinase curation export."""

import csv

from src.models.schema import AnnotationResult, GeneAnnotation
from src.pipeline.kinase_curation import (
    build_kinase_fusion_curation_rows,
    compare_kinase_curation_rows,
    read_kinase_fusion_curation_csv,
    write_kinase_curation_comparison_csv,
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
        "PMID 222: https://pubmed.ncbi.nlm.nih.gov/222/; "
        "PMID 333: https://pubmed.ncbi.nlm.nih.gov/333/"
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
            "publication link": "PMID 12345: https://pubmed.ncbi.nlm.nih.gov/12345/",
        }
    ]


def test_read_kinase_truth_csv_accepts_google_sheet_style_headers(tmp_path):
    truth_path = tmp_path / "truth.csv"
    truth_path.write_text(
        "Fusion,Kinase gene,Functional characterization,PMIDs\n"
        "ETV6-NTRK3,NTRK3,validated transforming kinase,PMID 222; PMID 333\n"
    )

    rows = read_kinase_fusion_curation_csv(truth_path)

    assert len(rows) == 1
    assert rows[0].fusion_detected == "ETV6-NTRK3"
    assert rows[0].kinase_included_in_fusion == "NTRK3"
    assert rows[0].biologic_characterization_specs == "validated transforming kinase"
    assert rows[0].publication_link == "PMID 222; PMID 333"


def test_compare_kinase_curation_rows_reports_overlap_and_deltas():
    pipeline_rows = [
        GeneAnnotation(
            gene="NTRK3",
            fusions=["ETV6::NTRK3"],
            gene_class="Receptor tyrosine kinase",
            citations=["22222", "99999"],
        ),
        GeneAnnotation(
            gene="ALK",
            fusions=["EML4::ALK"],
            gene_class="Tyrosine kinase",
            citations=["44444"],
        ),
    ]
    truth_rows = [
        GeneAnnotation(
            gene="NTRK3",
            fusions=["ETV6--NTRK3"],
            gene_class="Receptor tyrosine kinase",
            citations=["22222", "33333"],
        ),
        GeneAnnotation(
            gene="RET",
            fusions=["KIF5B::RET"],
            gene_class="Receptor tyrosine kinase",
            citations=["55555"],
        ),
    ]

    report = compare_kinase_curation_rows(
        build_kinase_fusion_curation_rows(_result(*pipeline_rows)),
        build_kinase_fusion_curation_rows(_result(*truth_rows)),
    )

    assert report["summary"] == {
        "pipeline_keys": 2,
        "truth_keys": 2,
        "matched_keys": 1,
        "pipeline_only_keys": 1,
        "truth_only_keys": 1,
        "fusion_kinase_precision": 0.5,
        "fusion_kinase_recall": 0.5,
        "fusion_kinase_f1": 0.5,
        "matched_citation_precision": 0.5,
        "matched_citation_recall": 0.5,
        "matched_citation_f1": 0.5,
    }
    matched = next(
        row
        for row in report["per_row"]
        if row["comparison_status"] == "matched"
    )
    assert matched["fusion_detected"] == "ETV6::NTRK3"
    assert matched["kinase_included_in_fusion"] == "NTRK3"
    assert matched["citation_tp"] == ["22222"]
    assert matched["citation_fp_pipeline_only"] == ["99999"]
    assert matched["citation_fn_truth_only"] == ["33333"]


def test_write_kinase_comparison_csv_flattens_citation_lists(tmp_path):
    report = {
        "summary": {},
        "per_row": [
            {
                "comparison_status": "matched",
                "fusion_detected": "ETV6::NTRK3",
                "kinase_included_in_fusion": "NTRK3",
                "citation_precision": 0.5,
                "citation_recall": 0.5,
                "citation_f1": 0.5,
                "citation_tp": ["222"],
                "citation_fp_pipeline_only": ["999"],
                "citation_fn_truth_only": ["333"],
                "pipeline_publication_link": "PMID 222; PMID 999",
                "truth_publication_link": "PMID 222; PMID 333",
                "pipeline_biologic_characterization_specs": "pipeline specs",
                "truth_biologic_characterization_specs": "truth specs",
            }
        ],
    }
    output_path = tmp_path / "comparison.csv"

    write_kinase_curation_comparison_csv(report, output_path)

    with open(output_path, newline="") as f:
        written = list(csv.DictReader(f))

    assert written[0]["citation_tp"] == "222"
    assert written[0]["citation_fp_pipeline_only"] == "999"
    assert written[0]["citation_fn_truth_only"] == "333"
