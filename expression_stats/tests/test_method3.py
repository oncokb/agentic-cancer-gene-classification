import numpy as np
import pytest
from scipy.stats import fisher_exact

from gene_expression_relevance import METHOD_VERSION, PARAMS
from gene_expression_relevance.stats import compute

CODES = [str(i) for i in range(32)]


def test_broad_demotion_and_subgroup_rescue():
    labels = np.repeat(CODES, 100)
    data = np.full((3, 3200), 31.0)
    data[0, :100] = 127  # broad baseline + lineage shift, no strictly higher P99 tail
    data[1, :12] = 2047  # broad baseline with a genuine subgroup
    data[2, :9] = 2047  # significant tail with too few cases
    result = compute(data, labels, CODES)
    assert METHOD_VERSION == "3"
    np.testing.assert_array_equal(result["broadly_expressed"], [True, True, True])
    assert result["lineage_blocked_by_broad"][0, 0]
    assert result["pass_mode"][0, 0] is None
    assert result["pass_mode"][1, 0] == "subgroup"
    assert not result["lineage_blocked_by_broad"][1, 0]
    assert result["subgroup_high_count"][1, 0] == 12
    assert result["subgroup_high_fraction"][1, 0] == pytest.approx(0.12)
    assert result["subgroup_high_fraction_ratio"][1, 0] == pytest.approx(0.12 * 3100)
    assert result["p95_log2_diff"][1, 0] == 6
    p = fisher_exact([[12, 88], [0, 3100]], alternative="greater").pvalue
    assert result["subgroup_q"][1, 0] == pytest.approx(32 * p)
    assert result["pass_mode"][2, 0] is None
    np.testing.assert_array_equal(result["stats_pass"], result["pass_mode"] != None)  # noqa: E711


def test_lineage_has_priority_when_both_arms_pass():
    labels = np.array(["0"] * 200 + [c for c in CODES[1:] for _ in range(100)])
    data = np.zeros((1, len(labels)))
    data[0, :200] = 31
    data[0, :20] = 2047
    result = compute(data, labels, CODES)
    assert not result["broadly_expressed"][0]
    assert result["pass_mode"][0, 0] == "lineage"
    assert result["subgroup_high_count"][0, 0] >= PARAMS["subgroup_min_count"]
    assert result["subgroup_q"][0, 0] < PARAMS["subgroup_fdr"]
    assert result["p95_log2_diff"][0, 0] >= PARAMS["subgroup_p95_log2_diff"]


@pytest.mark.parametrize(
    "types,detected,expected", [(29, 90, True), (28, 90, False), (29, 89, False)]
)
def test_broad_detection_requires_29_types_and_90_percent(types, detected, expected):
    data = np.zeros((1, 3200))
    for j in range(types):
        data[0, j * 100 : j * 100 + detected] = 10
    result = compute(data, np.repeat(CODES, 100), CODES)
    assert bool(result["broadly_expressed"][0]) == expected


def test_broad_requires_every_type_measured_and_type_balanced_cv():
    data = np.full((3, 320), 31.0)
    data[0, 0] = np.nan  # nine measurements in one cohort
    data[1, :10] = 1e6  # detected everywhere, very dissimilar typical levels
    result = compute(data, np.repeat(CODES, 10), CODES)
    np.testing.assert_array_equal(result["broadly_expressed"], [False, False, True])


def test_subgroup_fraction_gate_and_unmeasured_fields():
    data = np.full((1, 16000), 31.0)
    data[0, :12] = 2047  # 12/500 <3%, despite count and effect passing
    result = compute(data, np.repeat(CODES, 500), CODES)
    assert not result["stats_pass"].any()
    missing = compute([[np.nan, np.nan]], ["0", "0"], CODES)
    assert missing["pass_mode"][0, 0] is None
    for field in [
        "subgroup_high_count",
        "subgroup_high_fraction",
        "subgroup_high_fraction_ratio",
        "p95_log2_diff",
        "subgroup_q",
    ]:
        assert np.isnan(missing[field]).all()


@pytest.mark.parametrize("high,count", [(9, 0), (10, 0), (11, 12)])
def test_pooled_p99_is_floor_clamped_and_strict(high, count):
    data = np.zeros((1, 3200))
    data[0, :12] = high
    result = compute(data, np.repeat(CODES, 100), CODES)
    assert result["subgroup_high_count"][0, 0] == count
    assert not result["stats_pass"].any()
