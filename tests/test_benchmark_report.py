"""Tests for benchmark diagnostic reports."""

from benchmarks.run_benchmark import build_per_gene_report


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
