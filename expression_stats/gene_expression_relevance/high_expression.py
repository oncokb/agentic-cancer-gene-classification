"""Within-cohort alteration/expression association. Detail only; contract 1.6."""

import numpy as np
from scipy.stats import fisher_exact, mannwhitneyu

from . import COPY_NUMBER_ALTERATION, HIGH_EXPRESSION_PARAMS, POINT_MUTATION, STRUCTURAL_VARIANT
from .alterations import CNA_MISSING
from .stats import bh

AMP = f"{COPY_NUMBER_ALTERATION} (amp)"
DEL = f"{COPY_NUMBER_ALTERATION} (deep del)"
CATEGORIES = (POINT_MUTATION, STRUCTURAL_VARIANT, AMP, DEL)


def metrics(expression, profiled, altered):
    """Use finite, profiled expression; percentile ties follow the validated analysis."""
    measured = profiled & np.isfinite(expression)
    altered = altered & measured
    unaltered = measured & ~altered
    n, a = int(measured.sum()), int(altered.sum())
    result = dict(
        n_profiled=n,
        n_altered=a,
        delta_median=None,
        p=None,
        q=None,
        or_top10=None,
        p_top10=None,
        frac_top10_altered=None,
        k_top10=0,
        k_top5=0,
        median_altered=None,
        median_unaltered=None,
    )
    if not n:
        return result
    values = expression[measured]
    for percentile, key in ((90, "top10"), (95, "top5")):
        threshold = np.percentile(values, percentile)
        top = measured & (
            expression >= threshold if threshold > values.min() else expression > threshold
        )
        k, t = int((altered & top).sum()), int(top.sum())
        result[f"k_{key}"] = k
        if key == "top10":
            result["frac_top10_altered"] = k / t if t else None
            if a and unaltered.any() and 0 < t < n:
                odds, p = fisher_exact([[k, t - k], [a - k, n - t - a + k]], alternative="greater")
                # JSON has no infinity: preserve an unbounded OR explicitly.
                result["or_top10"] = float(odds) if np.isfinite(odds) else "Infinity"
                result["p_top10"] = float(p)
    if a and unaltered.any():
        alt, unalt = expression[altered], expression[unaltered]
        result.update(
            median_altered=float(np.median(alt)),
            median_unaltered=float(np.median(unalt)),
            delta_median=float(np.median(alt) - np.median(unalt)),
            p=1.0
            if np.ptp(values) == 0
            else float(
                mannwhitneyu(alt, unalt, alternative="two-sided", method="asymptotic").pvalue
            ),
        )
    return result


def verdict(row, scope_reason=None, params=HIGH_EXPRESSION_PARAMS):
    if scope_reason:
        return "not_testable", scope_reason
    if row["n_profiled"] < params["min_profiled"]:
        return "not_testable", "fewer than 20 measured profiled tumors"
    if row["n_altered"] < params["min_altered"]:
        if 1 <= row["n_altered"] <= 2 and row["k_top5"] == row["n_altered"]:
            return "anecdotal", "1–2 altered tumors in cohort top 5%; below minimum events"
        return "not_testable", "fewer than 3 measured altered tumors"
    if row["delta_median"] is None:
        return "not_testable", "no measured unaltered tumors"
    gates = [
        (row["delta_median"] >= params["delta_median"], "delta median < 1.0"),
        (row["q"] is not None and row["q"] < params["fdr"], "MW q >= 0.05"),
        (row["k_top10"] >= params["min_top10"], "fewer than 2 altered top-decile tumors"),
        (
            row["or_top10"] is not None and float(row["or_top10"]) >= params["min_or_top10"],
            "top-decile OR < 3.0",
        ),
        (
            row["p_top10"] is not None and row["p_top10"] < params["fisher_p"],
            "top-decile Fisher p >= 0.05",
        ),
    ]
    failed = [reason for passed, reason in gates if not passed]
    return ("not_associated", "; ".join(failed)) if failed else ("associated_high_expression", None)


class Association:
    """Compute sparse active rows, then BH across testable cohorts per gene/category."""

    def __init__(
        self, data, *, inputs, mapping, selection, genes, events, profiled, driver_mask, cna_allowed
    ):
        self.rows = {}
        self.scope = {}
        offset = 0
        for j, item in enumerate(mapping):
            study = item["study_id"]
            samples = selection[study]
            index = {s: k for k, s in enumerate(samples)}
            expression = np.log2(data[:, offset : offset + len(samples)] + 1)
            offset += len(samples)
            for category in CATEGORIES:
                if category in (AMP, DEL):
                    source = inputs[study]["cna"]
                    raw = source["data"]
                    masks = {}
                    used = set()
                    rows = genes.rows(raw["symbol"], raw["entrez"])
                    eligible = (rows >= 0) & cna_allowed[
                        np.maximum(rows, 0), 0 if category == AMP else 1
                    ]
                    for col, sample in enumerate(raw["samples"]):
                        if sample not in index or sample not in source["cases"] or sample in used:
                            continue
                        used.add(sample)
                        k = index[sample]
                        for r in np.flatnonzero(eligible):
                            row = int(rows[r])
                            if not cna_allowed[row, 0 if category == AMP else 1]:
                                continue
                            prof, alt = masks.setdefault(
                                row, (np.zeros(len(samples), bool), np.zeros(len(samples), bool))
                            )
                            call = raw["calls"][r, col]
                            prof[k] |= call != CNA_MISSING
                            alt[k] |= call == (2 if category == AMP else -2)
                else:
                    prof = np.array([s in profiled[category] for s in samples])
                    masks = {}
                    for row, sample in events[category]:
                        if sample in index:
                            if row not in masks:
                                masks[row] = (prof, np.zeros(len(samples), bool))
                            masks[row][1][index[sample]] = True
                    # Keep profiled denominators even for genes with no events.
                    self.scope[j, category] = (np.isfinite(expression) & prof).sum(axis=1)
                for row, (prof, alt) in masks.items():
                    self.rows[row, j, category] = metrics(expression[row], prof, alt)
        self.driver_mask, self.cna_allowed = driver_mask, cna_allowed
        families = {}
        for (i, _, category), row in self.rows.items():
            if row["n_profiled"] >= 20 and row["n_altered"] >= 3 and row["p"] is not None:
                families.setdefault((i, category), []).append(row)
        for testable in families.values():
            for row, q in zip(testable, bh(np.array([r["p"] for r in testable])), strict=True):
                row["q"] = float(q)

    def record(self, i, j):
        result = {}
        for category in CATEGORIES:
            scope = None
            if category == POINT_MUTATION and not self.driver_mask[i]:
                scope = "gene not in mutation driver list"
            if category in (AMP, DEL) and not self.cna_allowed[i, 0 if category == AMP else 1]:
                scope = "not a copy-number driver in this direction"
            row = dict(
                self.rows.get(
                    (i, j, category),
                    dict(
                        n_profiled=(
                            int(self.scope[j, category][i]) if (j, category) in self.scope else 0
                        ),
                        n_altered=0,
                        delta_median=None,
                        q=None,
                        or_top10=None,
                        p_top10=None,
                        frac_top10_altered=None,
                        k_top10=0,
                        k_top5=0,
                    ),
                )
            )
            row["status"], row["reason"] = verdict(row, scope)
            result[category] = row
        return result
