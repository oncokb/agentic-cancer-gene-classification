"""Tests for benchmark diagnostic reports."""

from benchmarks.run_benchmark import (
    build_kinase_curation_benchmark_report,
    build_per_gene_report,
    _load_pipeline_result,
)


def test_build_per_gene_report_includes_citation_deltas_and_tier_match():
    predictions = [
        {
            "gene": "GENE",
            "in_oncokb": True,
            "retrieval_count": 8,
            "cancer_associated": True,
            "cancer_associated_gene_tier": "Class II - Likely Driver",
            "og_or_tsg": "OG",
            "citations": ["111", "222"],
        }
    ]
    ground_truth = [
        {
            "gene": "GENE",
            "cancer_associated": True,
            "cancer_associated_gene_tier": "Class III - Cancer Relevant",
            "og_or_tsg": None,
            "citations": ["111", "333"],
        }
    ]

    rows = build_per_gene_report(predictions, ground_truth)

    assert rows == [
        {
            "gene": "GENE",
            "in_oncokb": True,
            "retrieval_count": 8,
            "pred_cancer_associated": True,
            "gold_cancer_associated": True,
            "pred_tier": "Class II - Likely Driver",
            "gold_tier": "Class III - Cancer Relevant",
            "tier_match": False,
            "pred_og_or_tsg": "OG",
            "gold_og_or_tsg": None,
            "citation_precision": 0.5,
            "citation_recall": 0.5,
            "citation_f1": 0.5,
            "citation_tp": ["111"],
            "citation_fp": ["222"],
            "citation_fn": ["333"],
            "pred_citations": ["111", "222"],
            "gold_citations": ["111", "333"],
        }
    ]


def test_load_pipeline_result_accepts_full_benchmark_report(tmp_path):
    full_report = tmp_path / "report.json"
    full_report.write_text(
        """
{
  "categorical_metrics": {},
  "pipeline_result": {
    "run_id": "run-1",
    "timestamp": "2026-07-13T00:00:00+00:00",
    "fusions_processed": 1,
    "genes_annotated": 1,
    "annotations": [{"gene": "NTRK3", "citations": []}]
  }
}
""".strip()
    )

    assert _load_pipeline_result(full_report) == {
        "run_id": "run-1",
        "timestamp": "2026-07-13T00:00:00+00:00",
        "fusions_processed": 1,
        "genes_annotated": 1,
        "annotations": [{"gene": "NTRK3", "citations": []}],
    }


def test_build_kinase_curation_benchmark_report_includes_generated_rows():
    pipeline_result = {
        "run_id": "run-1",
        "timestamp": "2026-07-13T00:00:00+00:00",
        "fusions_processed": 1,
        "genes_annotated": 2,
        "annotations": [
            {
                "gene": "ETV6",
                "fusions": ["ETV6::NTRK3"],
                "gene_class": "Transcription factor",
                "citations": [],
            },
            {
                "gene": "NTRK3",
                "fusions": ["ETV6::NTRK3"],
                "gene_class": "Receptor tyrosine kinase",
                "citations": ["22222", "33333"],
            },
        ],
    }

    report = build_kinase_curation_benchmark_report(pipeline_result)

    assert report["generated_rows"] == 1
    assert report["generated_fusions"] == ["ETV6::NTRK3"]
    assert report["rows"][0]["kinase_included_in_fusion"] == "NTRK3"
    assert report["comparison"] is None


def test_build_kinase_curation_benchmark_report_compares_truth_csv(tmp_path):
    truth_path = tmp_path / "truth.csv"
    truth_path.write_text(
        "Fusion,Kinase gene,PMIDs\n"
        "ETV6--NTRK3,NTRK3,PMID 22222; PMID 44444\n"
        "KIF5B--RET,RET,PMID 55555\n"
    )
    pipeline_result = {
        "run_id": "run-1",
        "timestamp": "2026-07-13T00:00:00+00:00",
        "fusions_processed": 1,
        "genes_annotated": 1,
        "annotations": [
            {
                "gene": "NTRK3",
                "fusions": ["ETV6::NTRK3"],
                "gene_class": "Receptor tyrosine kinase",
                "citations": ["22222", "33333"],
            },
        ],
    }

    report = build_kinase_curation_benchmark_report(
        pipeline_result,
        truth_csv=truth_path,
    )

    assert report["generated_rows"] == 1
    assert report["truth_csv"] == str(truth_path)
    assert report["comparison"]["summary"] == {
        "pipeline_keys": 1,
        "truth_keys": 2,
        "matched_keys": 1,
        "pipeline_only_keys": 0,
        "truth_only_keys": 1,
        "fusion_kinase_precision": 1.0,
        "fusion_kinase_recall": 0.5,
        "fusion_kinase_f1": 0.6667,
        "matched_citation_precision": 0.5,
        "matched_citation_recall": 0.5,
        "matched_citation_f1": 0.5,
    }
