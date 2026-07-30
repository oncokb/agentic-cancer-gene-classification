"""Tests for benchmark diagnostic reports."""

from benchmarks.run_benchmark import build_per_gene_report


def test_build_per_gene_report_includes_citation_deltas():
    predictions = [
        {
            "gene": "GENE",
            "in_oncokb": True,
            "retrieval_count": 8,
            "cancer_associated": True,
            "citations": ["111", "222"],
        }
    ]
    ground_truth = [
        {
            "gene": "GENE",
            "cancer_associated": True,
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
