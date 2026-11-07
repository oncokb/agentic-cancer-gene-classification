import numpy as np
import pytest
from scipy.stats import mannwhitneyu

from gene_expression_relevance import PARAMS
from gene_expression_relevance.stats import bh, compute, passes, primary_indices


def test_bh_known_answer():
    np.testing.assert_allclose(
        bh([[0.01, 0.04, 0.03, 0.2]]), [[0.04, 0.0533333333, 0.0533333333, 0.2]]
    )
    np.testing.assert_allclose(bh([[0.01, np.nan, 1, 0]]), [[0.02, 1, 1, 0]])


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_rank_statistics_match_scipy_with_ties_and_missing(seed):
    rng = np.random.default_rng(seed)
    data = rng.integers(0, 8, size=(12, 33)).astype(float)
    data[0, :5] = np.nan
    labels = np.array(["A"] * 10 + ["B"] * 11 + ["C"] * 12)
    result = compute(data, labels, ["A", "B", "C"])
    for i in range(len(data)):
        for j, code in enumerate(["A", "B", "C"]):
            a = np.log2(data[i, labels == code] + 1)
            b = np.log2(data[i, labels != code] + 1)
            a, b = a[np.isfinite(a)], b[np.isfinite(b)]
            u, p = mannwhitneyu(a, b, alternative="two-sided", method="asymptotic")
            assert result["p_value"][i, j] == pytest.approx(p)
            assert result["auc"][i, j] == pytest.approx(u / (len(a) * len(b)))
            delta = np.sign(a[:, None] - b).mean()
            assert result["cliffs_delta"][i, j] == pytest.approx(delta)
            assert result["log2fc"][i, j] == pytest.approx(
                max(np.median(a), PARAMS["expression_floor_log2"])
                - max(np.median(b), PARAMS["expression_floor_log2"])
            )


def test_enrichment_known_answer_and_32_type_bh():
    labels = np.repeat([str(i) for i in range(32)], 10)
    data = np.zeros((2, 320))
    data[0, :10] = 31
    result = compute(data, labels, [str(i) for i in range(32)])
    assert result["stats_pass"][0, 0]
    assert result["stats_pass"].sum() == 1
    assert result["median_log2"][0, 0] == 5
    assert result["log2fc"][0, 0] == pytest.approx(5 - PARAMS["expression_floor_log2"])
    assert result["log2fc_vs_median_of_type_medians"][0, 0] == pytest.approx(
        5 - PARAMS["expression_floor_log2"]
    )
    assert result["cliffs_delta"][0, 0] == 1
    assert result["auc"][0, 0] == 1
    assert result["p_value"][1, 0] == 1
    np.testing.assert_allclose(result["q_value"], bh(result["p_value"]))


def test_median_of_type_medians_resists_large_cohort():
    labels = ["A"] * 10 + ["B"] * 100 + ["C"] * 10
    result = compute([[255] * 10 + [63] * 100 + [15] * 10], labels, ["A", "B", "C"])
    assert result["log2fc"][0, 0] == 2
    assert result["log2fc_vs_median_of_type_medians"][0, 0] == 3


@pytest.mark.parametrize(
    "field,bad",
    [
        ("q", 0.05),
        ("fc", 0.999),
        ("delta", 0.499),
        ("median", PARAMS["expression_floor_log2"] - 0.001),
        ("n", 9),
    ],
)
def test_thresholds(field, bad):
    values = dict(q=0.049, fc=1.0, delta=0.5, median=PARAMS["expression_floor_log2"], n=10)
    assert passes(**values)
    values[field] = bad
    assert not passes(**values)
    assert PARAMS["min_n"] == 10


def test_filter_dedupe_and_laml():
    samples = [
        "TCGA-AA-0001-01B",
        "TCGA-AA-0001-01A",
        "TCGA-AA-0002-11",
        "TCGA-AA-0003-02",
        "TCGA-AA-0004-03",
        "bad",
        "TCGA-AA-0005-01",
    ]
    seen = set()
    assert primary_indices(samples, "BRCA", seen) == [1, 6]
    assert primary_indices(samples, "BRCA", seen) == []
    assert primary_indices(samples, "LAML") == [4]


def test_unmeasured_gene_and_empty_type():
    result = compute([[np.nan, np.nan], [0, 0]], ["A", "A"], ["A", "B"])
    assert not result["stats_pass"].any()
    assert result["n"][0, 0] == 0
    assert np.isnan(result["p_value"][0, 0])
    assert np.isnan(result["q_value"][0, 0])
    assert np.isnan(result["auc"][1, 0])


@pytest.mark.parametrize("bad", [-1, np.inf])
def test_reject_invalid_expression(bad):
    with pytest.raises(ValueError):
        compute([[bad, 1]], ["A", "B"], ["A", "B"])


def test_batch_normalization_negative_values_inside_log_domain():
    result = compute([[-0.7, 0, 1, 2]], ["A", "A", "B", "B"], ["A", "B"])
    assert result["median_log2"][0, 0] == pytest.approx(np.log2(0.3) / 2)


@pytest.mark.parametrize(
    "tumor,rest,expected",
    [
        (7, 0, 0),
        (15, 0, 4 - PARAMS["expression_floor_log2"]),
        (31, 0, 5 - PARAMS["expression_floor_log2"]),
        (63, 31, 1),
        (3, 31, PARAMS["expression_floor_log2"] - 5),
    ],
)
def test_floor_clamps_both_comparators(tumor, rest, expected):
    result = compute([[tumor] * 10 + [rest] * 20], ["A"] * 10 + ["B"] * 20, ["A", "B"])
    assert result["log2fc"][0, 0] == pytest.approx(expected)
    assert result["log2fc_vs_median_of_type_medians"][0, 0] == pytest.approx(expected)
    assert result["stats_pass"][0, 0] == (expected >= 1)


def test_p90_is_informational_and_uses_linear_interpolation():
    tumor_log = [1] * 7 + [10, 11, 12]
    data = np.exp2([tumor_log + [2] * 10]) - 1
    result = compute(data, ["A"] * 10 + ["B"] * 10, ["A", "B"])
    assert result["p90_log2"][0, 0] == pytest.approx(11.1)
    assert result["rest_p90_log2"][0, 0] == 2
    assert not result["stats_pass"].any()


def test_detection_uses_single_floor_and_effective_minimum_is_rsem_21():
    assert PARAMS["detection_log2"] == PARAMS["expression_floor_log2"]
    custom = {**PARAMS, "expression_floor_log2": 5}
    assert not passes(q=0.001, fc=1, delta=0.5, median=4.5, n=10, params=custom)
    assert passes(q=0.001, fc=1, delta=0.5, median=5, n=10, params=custom)
    result = compute(
        [[20] * 10 + [0] * 10, [21] * 10 + [0] * 10], ["A"] * 10 + ["B"] * 10, ["A", "B"]
    )
    np.testing.assert_array_equal(result["stats_pass"][:, 0], [False, True])
