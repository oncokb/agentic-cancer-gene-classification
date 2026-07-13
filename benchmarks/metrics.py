"""
Scoring functions for M0 validation harness.
All metrics are implemented without external ML dependencies.

Fields evaluated:
  - cancer_associated      → binary accuracy + Cohen's kappa
  - cancer_associated_gene_tier → per-class F1 + macro F1
  - og_or_tsg              → per-class F1 + macro F1
  - citations              → set precision, recall, F1 per gene; then mean
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from src.evaluation.metrics import (
    citation_scores as citation_scores,
    mean_citation_scores,
    safe_div,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_div(num: float, den: float) -> float:
    return safe_div(num, den)


# ---------------------------------------------------------------------------
# Cohen's kappa (binary)
# ---------------------------------------------------------------------------

def cohen_kappa_binary(
    pred: List[Optional[bool]],
    gold: List[Optional[bool]],
) -> Tuple[float, float]:
    """
    Compute observed accuracy and Cohen's kappa for binary (true/false) labels.
    None values in either pred or gold are skipped.
    Returns (accuracy, kappa).
    """
    pairs = [(p, g) for p, g in zip(pred, gold) if p is not None and g is not None]
    n = len(pairs)
    if n == 0:
        return 0.0, 0.0

    correct = sum(p == g for p, g in pairs)
    accuracy = correct / n

    # Expected agreement by chance
    pred_pos = sum(1 for p, _ in pairs if p is True) / n
    gold_pos = sum(1 for _, g in pairs if g is True) / n
    pred_neg = 1 - pred_pos
    gold_neg = 1 - gold_pos
    p_expected = pred_pos * gold_pos + pred_neg * gold_neg

    kappa = _safe_div(accuracy - p_expected, 1 - p_expected)
    return accuracy, kappa


# ---------------------------------------------------------------------------
# Per-class F1 (multiclass)
# ---------------------------------------------------------------------------

def per_class_f1(
    pred: List[Optional[str]],
    gold: List[Optional[str]],
) -> Tuple[Dict[str, float], float]:
    """
    Compute per-class F1 scores and macro-averaged F1.
    None values in either list are skipped.
    Returns (per_class_dict, macro_f1).
    """
    pairs = [(p, g) for p, g in zip(pred, gold) if p is not None and g is not None]

    classes = sorted({g for _, g in pairs})

    per_class: Dict[str, float] = {}
    for cls in classes:
        tp = sum(1 for p, g in pairs if p == cls and g == cls)
        fp = sum(1 for p, g in pairs if p == cls and g != cls)
        fn = sum(1 for p, g in pairs if p != cls and g == cls)
        precision = _safe_div(tp, tp + fp)
        recall = _safe_div(tp, tp + fn)
        per_class[cls] = _safe_div(2 * precision * recall, precision + recall)

    macro = sum(per_class.values()) / len(per_class) if per_class else 0.0
    return per_class, macro


# ---------------------------------------------------------------------------
# Summary-level report builder (non-LLM fields)
# ---------------------------------------------------------------------------

def compute_categorical_metrics(
    predictions: List[Dict],
    ground_truth: List[Dict],
) -> Dict:
    """
    Compare predicted GeneAnnotation dicts against ground-truth holdout dicts.
    Returns a metrics dict covering cancer_associated, tier, og_or_tsg, citations.
    """
    pred_cancer = [p.get("cancer_associated") for p in predictions]
    gold_cancer = [g.get("cancer_associated") for g in ground_truth]

    pred_tier = [p.get("cancer_associated_gene_tier") for p in predictions]
    gold_tier = [g.get("cancer_associated_gene_tier") for g in ground_truth]

    pred_ogtsg = [p.get("og_or_tsg") for p in predictions]
    gold_ogtsg = [g.get("og_or_tsg") for g in ground_truth]

    pred_cites = [p.get("citations", []) for p in predictions]
    gold_cites = [g.get("citations", []) for g in ground_truth]

    ca_accuracy, ca_kappa = cohen_kappa_binary(pred_cancer, gold_cancer)
    tier_per_class, tier_macro = per_class_f1(pred_tier, gold_tier)
    ogtsg_per_class, ogtsg_macro = per_class_f1(pred_ogtsg, gold_ogtsg)
    cite_scores = mean_citation_scores(pred_cites, gold_cites)

    return {
        "n": len(predictions),
        "cancer_associated": {
            "accuracy": round(ca_accuracy, 4),
            "cohen_kappa": round(ca_kappa, 4),
        },
        "cancer_tier": {
            "macro_f1": round(tier_macro, 4),
            "per_class": {k: round(v, 4) for k, v in tier_per_class.items()},
        },
        "og_or_tsg": {
            "macro_f1": round(ogtsg_macro, 4),
            "per_class": {k: round(v, 4) for k, v in ogtsg_per_class.items()},
        },
        "citations": {k: round(v, 4) for k, v in cite_scores.items()},
    }
