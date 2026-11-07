"""Tumor-type expression enrichment; not tumor versus normal."""

METHOD_VERSION = "3"
PARAMS = dict(
    fdr=0.05,
    log2fc=1.0,
    cliffs_delta=0.5,
    min_n=10,
    expression_floor_log2=3.4594316186372973,
    broad_sample_fraction=0.90,
    broad_min_types=29,
    broad_type_median_cv=0.50,
    broad_required_types=32,
    subgroup_percentile=99,
    subgroup_min_count=10,
    subgroup_min_fraction=0.03,
    subgroup_min_fraction_ratio=3.0,
    subgroup_p95_percentile=95,
    subgroup_p95_log2_diff=1.5,
    subgroup_fdr=0.025,
    precheck_requests_per_second=3.0,
    precheck_min_pmids=1,
    precheck_cache_ttl_days=30,
)

# Recorded for consumers, derived from the single configurable expression floor.
PARAMS["detection_log2"] = PARAMS["expression_floor_log2"]
