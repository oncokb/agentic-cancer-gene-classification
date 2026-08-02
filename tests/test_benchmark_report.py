"""Tests for benchmark diagnostic reports."""

from benchmarks.run_benchmark import _run_pipeline_via_local_route, build_per_gene_report
from src.models.schema import AnnotationResult, GeneAnnotation


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


async def test_run_pipeline_via_local_route_posts_to_annotate(monkeypatch):
    seen = {}

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        mode="full",
        on_annotation=None,
    ):
        seen.update(
            {
                "fusions": [item.fusion for item in fusions],
                "local_backend": local_backend,
                "force_refresh": force_refresh,
                "mode": mode,
            }
        )
        return AnnotationResult(
            run_id="run-1",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="TP53", cancer_associated=True)],
        )

    monkeypatch.setattr("src.main.run_pipeline", fake_run_pipeline)

    result = await _run_pipeline_via_local_route(
        ["TP53::BRAF"],
        local_backend="codex",
        mode="core",
    )

    assert result["annotations"][0]["gene"] == "TP53"
    assert seen == {
        "fusions": ["TP53::BRAF"],
        "local_backend": "codex",
        "force_refresh": True,
        "mode": "core",
    }
