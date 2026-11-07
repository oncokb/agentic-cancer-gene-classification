"""Curated expression controls: measured statistics, never PubMed verdicts."""

import hashlib
import json
from pathlib import Path

PANEL_PATH = Path(__file__).resolve().parents[1] / "controls.yaml"


def panel_sha256():
    return hashlib.sha256(PANEL_PATH.read_bytes()).hexdigest()


def evaluate(genes_dir, *, subset=False, panel=None):
    """Missing scored pairs fail closed, including missing negative controls."""
    panel = json.loads(PANEL_PATH.read_text()) if panel is None else panel
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
    )


def require_passed(report):
    if report["status"] != "passed":
        raise ValueError(f"Controls gate {report['status']}: {report['failures']}")
