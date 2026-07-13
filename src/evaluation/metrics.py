"""Shared metric helpers used by benchmarks and curation reports."""

from __future__ import annotations

from typing import Dict, List, Tuple


def safe_div(num: float, den: float) -> float:
    return num / den if den > 0 else 0.0


def citation_scores(
    pred_citations: List[str],
    gold_citations: List[str],
) -> Tuple[float, float, float]:
    """
    Set-based precision, recall, F1 for a single citation set.
    Both lists are PMID strings.
    """
    pred_set = set(pred_citations)
    gold_set = set(gold_citations)

    if not pred_set and not gold_set:
        return 1.0, 1.0, 1.0
    if not pred_set:
        return 0.0, 0.0, 0.0
    if not gold_set:
        return 0.0, 1.0, 0.0

    tp = len(pred_set & gold_set)
    precision = safe_div(tp, len(pred_set))
    recall = safe_div(tp, len(gold_set))
    f1 = safe_div(2 * precision * recall, precision + recall)
    return precision, recall, f1


def mean_citation_scores(
    all_pred: List[List[str]],
    all_gold: List[List[str]],
) -> Dict[str, float]:
    """Aggregate citation scores across aligned records."""
    scores = [citation_scores(p, g) for p, g in zip(all_pred, all_gold)]
    n = len(scores)
    if n == 0:
        return {"precision": 0.0, "recall": 0.0, "f1": 0.0}
    return {
        "precision": sum(s[0] for s in scores) / n,
        "recall": sum(s[1] for s in scores) / n,
        "f1": sum(s[2] for s in scores) / n,
    }
