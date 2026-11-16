import json

import numpy as np
import pytest

from gene_expression_relevance.controls import PANEL_PATH, evaluate, recheck
from gene_expression_relevance.high_expression import metrics, verdict


def test_association_uses_profiled_finite_samples_and_strict_percentile_ties():
    expression = np.array([10.0, 11.0, 12.0, *([1.0] * 27), np.nan, 100.0])
    profiled = np.array([True] * 31 + [False])
    altered = np.array([True] * 3 + [False] * 29)
    row = metrics(expression, profiled, altered)
    row["q"] = row["p"]
    assert (row["n_profiled"], row["n_altered"], row["k_top10"]) == (30, 3, 3)
    assert row["delta_median"] == 10
    assert row["or_top10"] == "Infinity"
    assert verdict(row) == ("associated_high_expression", None)
    flat = metrics(np.zeros(30), np.ones(30, bool), np.arange(30) < 3)
    flat["q"] = flat["p"]
    assert flat["k_top10"] == 0 and verdict(flat)[0] == "not_associated"


@pytest.mark.parametrize(
    "field,value",
    [
        ("n_profiled", 19),
        ("n_altered", 2),
        ("delta_median", 0.999),
        ("q", 0.05),
        ("k_top10", 1),
        ("or_top10", 2.999),
        ("p_top10", 0.05),
    ],
)
def test_every_required_gate(field, value):
    row = dict(
        n_profiled=20,
        n_altered=3,
        delta_median=1.0,
        q=0.049,
        k_top10=2,
        k_top5=0,
        or_top10=3.0,
        p_top10=0.049,
    )
    assert verdict(row)[0] == "associated_high_expression"
    row[field] = value
    assert verdict(row)[0] != "associated_high_expression"


def test_anecdotal_requires_top_five_and_never_calls():
    row = dict(n_profiled=30, n_altered=2, k_top5=2)
    assert verdict(row)[0] == "anecdotal"
    row["k_top5"] = 1
    assert verdict(row)[0] == "not_testable"
    assert verdict(row, "out of scope") == ("not_testable", "out of scope")


def test_high_expression_panel_is_gating_and_rederived():
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from test_bundle import CODES, PANEL, synthesize

    with TemporaryDirectory() as directory:
        source = Path(directory)
        report = synthesize(PANEL, CODES, source, "test")
        assert report["status"] == "passed"
        assert report["high_expression"]["positive_passed"] == 9
        assert report["high_expression"]["negative_total"] == 384
        assert recheck(report, source, CODES) == []
        path = source / "MET.json"
        doc = json.loads(path.read_text())
        next(r for r in doc["cancer_types"] if r["oncotree_code"] == "LUAD")[
            "alteration_high_expression"
        ]["Structural Variant"]["status"] = "not_associated"
        path.write_text(json.dumps(doc))
        assert evaluate(source, codes=CODES)["status"] == "failed"
        assert recheck(report, source, CODES)
    panel = json.loads(PANEL_PATH.read_text())
    assert all(r["id"] != "CNA-MET-LUAD" for r in panel["alteration_controls"])
    assert "expected" not in panel["alteration_known_misses"][0]


def test_bh_family_excludes_low_event_cohorts_and_respects_scope():
    from alteration_fixtures import empty_inputs
    from scipy.stats import mannwhitneyu

    from gene_expression_relevance import STRUCTURAL_VARIANT
    from gene_expression_relevance.alterations import Genes
    from gene_expression_relevance.high_expression import Association

    mapping = [dict(study_id=f"s{i}", oncotree_code=f"C{i}") for i in range(3)]
    samples = {f"s{i}": [f"{i}-{s}" for s in range(30)] for i in range(3)}
    inputs, _, _ = empty_inputs(None, None, samples)
    first = np.arange(30, dtype=float)
    second = first.copy()
    second[:3] = [25, 26, 27]
    data = (2 ** np.concatenate([first, second, first]) - 1)[None, :]
    events = {(0, s) for i in range(3) for s in samples[f"s{i}"][-(1 if i == 2 else 3) :]}
    profiled = set(s for group in samples.values() for s in group)
    association = Association(
        data,
        inputs=inputs,
        mapping=mapping,
        selection=samples,
        genes=Genes([1], {1: "G"}, {}),
        events={"Point Mutation": set(), STRUCTURAL_VARIANT: events},
        profiled={"Point Mutation": profiled, STRUCTURAL_VARIANT: profiled},
        driver_mask=np.array([False]),
        cna_allowed=np.array([[False, False]]),
    )
    p1 = mannwhitneyu(first[-3:], first[:-3], method="asymptotic").pvalue
    p2 = mannwhitneyu(second[-3:], second[:-3], method="asymptotic").pvalue
    expected = min(2 * p1, p2) if p1 <= p2 else p1
    assert association.record(0, 0)[STRUCTURAL_VARIANT]["q"] == pytest.approx(expected)
    assert association.record(0, 2)[STRUCTURAL_VARIANT]["q"] is None
    assert association.record(0, 0)["Point Mutation"]["status"] == "not_testable"
