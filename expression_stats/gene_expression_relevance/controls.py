"""Curated expression and alteration controls: measured statistics, never PubMed verdicts.

Stdlib-only: the publisher loads this file with runpy, without the stats dependencies.
"""

import hashlib
import json
from pathlib import Path

PANEL_PATH = Path(__file__).resolve().parents[1] / "controls.yaml"


def panel_sha256():
    return hashlib.sha256(PANEL_PATH.read_bytes()).hexdigest()


def expected_counts(panel):
    ids, pairs = set(), set()
    for row in panel["controls"]:
        pair = (row["gene"], row["tcga"])
        if row["id"] in ids or pair in pairs:
            raise ValueError(f"Duplicate control id or (gene, tcga): {row['id']} {pair}")
        ids.add(row["id"])
        pairs.add(pair)
    return (
        sum(row["expected"] is True for row in panel["controls"]),
        sum(row["expected"] is False for row in panel["controls"]),
    )


ALL_COHORTS = "*"


def expand_alteration_controls(panel, codes):
    """Explicit alteration control rows; tcga "*" means one row per build cohort."""
    rows, ids, keys = [], set(), set()
    for control in panel.get("alteration_controls", []):
        targets = list(codes) if control["tcga"] == ALL_COHORTS else [control["tcga"]]
        for code in targets:
            row = dict(control, tcga=code)
            if control["tcga"] == ALL_COHORTS:
                row["id"] = f"{control['id']}:{code}"
            key = (row["gene"], row["tcga"], row["category"])
            if row["id"] in ids or key in keys:
                raise ValueError(f"Duplicate alteration control id or pair: {row['id']} {key}")
            ids.add(row["id"])
            keys.add(key)
            rows.append(row)
    return rows


def expected_alteration_counts(panel, codes):
    rows = expand_alteration_controls(panel, codes)
    return (
        sum(row["expected"] is True for row in rows),
        sum(row["expected"] is False for row in rows),
    )


def evaluate_alterations(genes_dir, codes, *, subset=False, panel=None, skipped=False):
    """Category statuses for alteration controls; missing pairs fail closed."""
    panel = json.loads(PANEL_PATH.read_text()) if panel is None else panel
    rows, failures, documents = [], [], {}
    for control in expand_alteration_controls(panel, codes):
        gene = control["gene"]
        if gene not in documents:
            path = Path(genes_dir) / f"{gene}.json"
            documents[gene] = json.loads(path.read_text()) if path.exists() else {}
        record = next(
            (
                r
                for r in documents[gene].get("cancer_types", [])
                if r["oncotree_code"] == control["tcga"]
            ),
            {},
        )
        entry = ((record.get("alterations") or {}).get("categories") or {}).get(control["category"])
        observed = entry["status"] if entry else None
        directions = (entry or {}).get("enriched_directions") or []
        expected = control["expected"]
        enriched = observed == "enriched" and (
            control.get("direction") is None or control["direction"] in directions
        )
        scored = expected is not None and not subset and not skipped
        matched = observed is not None and (enriched if expected else observed != "enriched")
        if scored and not matched:
            failures.append(control["id"])
        rows.append(
            dict(
                **control,
                observed=observed,
                enriched_directions=directions,
                n_events=(entry or {}).get("n_events"),
                n_profiled=(entry or {}).get("n_profiled"),
                frequency=(entry or {}).get("frequency"),
                rest_frequency=(entry or {}).get("rest_frequency"),
                ratio=(entry or {}).get("ratio"),
                q_value=(entry or {}).get("q_value"),
                reason=(entry or {}).get("reason"),
                scored=scored,
                matched=matched if scored else None,
            )
        )
    positives = [r for r in rows if r["expected"] is True]
    negatives = [r for r in rows if r["expected"] is False]
    status = "skipped" if skipped else "skipped_subset" if subset else None
    return dict(
        status=status or ("failed" if failures else "passed"),
        positive_passed=sum(r["matched"] is True for r in positives),
        positive_total=len(positives),
        negative_passed=sum(r["observed"] == "enriched" for r in negatives),
        negative_total=len(negatives),
        missing=sum(r["observed"] is None for r in positives + negatives),
        failures=failures,
        controls=rows,
    )


def evaluate(genes_dir, *, subset=False, panel=None, codes=None, alterations=None):
    """Missing scored pairs fail closed, including missing negative controls.

    With `codes`, alteration controls are evaluated too (skipped when alterations=False)
    and any alteration failure fails the whole gate.
    """
    panel = json.loads(PANEL_PATH.read_text()) if panel is None else panel
    expected_counts(panel)
    rows, failures = [], []
    for control in panel["controls"]:
        path = Path(genes_dir) / f"{control['gene']}.json"
        document = json.loads(path.read_text()) if path.exists() else {}
        record = next(
            (r for r in document.get("cancer_types", []) if r["oncotree_code"] == control["tcga"]),
            {},
        )
        observed = record.get("stats_pass")
        expected = control["expected"]
        scored = expected is not None and not subset
        matched = isinstance(observed, bool) and observed == expected
        if scored and not matched:
            failures.append(control["id"])
        rows.append(
            dict(
                **control,
                observed=observed,
                pass_mode=record.get("pass_mode"),
                scored=scored,
                matched=matched if scored else None,
            )
        )
    positives = [r for r in rows if r["expected"] is True]
    negatives = [r for r in rows if r["expected"] is False]
    extra = {}
    if codes is not None:
        extra["alterations"] = evaluate_alterations(
            genes_dir, codes, subset=subset, panel=panel, skipped=alterations is False
        )
        failures = failures + extra["alterations"]["failures"]
    return dict(
        status="skipped_subset" if subset else "failed" if failures else "passed",
        controls_sha256=panel_sha256(),
        positive_passed=sum(r["observed"] is True for r in positives),
        positive_total=len(positives),
        negative_passed=sum(r["observed"] is True for r in negatives),
        negative_total=len(negatives),
        missing=sum(r["observed"] is None for r in positives + negatives),
        failures=failures,
        controls=rows,
        **extra,
    )


def require_passed(report):
    if report["status"] != "passed":
        raise ValueError(f"Controls gate {report['status']}: {report['failures']}")
