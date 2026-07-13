"""Tests for shared evaluation helpers."""

from src.evaluation.metrics import citation_scores, mean_citation_scores


def test_shared_citation_scores_match_benchmark_semantics():
    assert citation_scores(["11111", "22222"], ["11111", "33333"]) == (
        0.5,
        0.5,
        0.5,
    )
    assert citation_scores([], []) == (1.0, 1.0, 1.0)
    assert citation_scores([], ["11111"]) == (0.0, 0.0, 0.0)


def test_mean_citation_scores_aggregates_aligned_records():
    scores = mean_citation_scores(
        [["11111"], ["22222", "33333"]],
        [["11111"], ["22222", "44444"]],
    )

    assert scores == {"precision": 0.75, "recall": 0.75, "f1": 0.75}
