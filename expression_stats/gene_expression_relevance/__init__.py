"""Tumor-type expression enrichment; not tumor versus normal."""

METHOD_VERSION = "2"
PARAMS = dict(
    fdr=0.05,
    log2fc=1.0,
    cliffs_delta=0.5,
    min_n=10,
    expression_floor_log2=3.4594316186372973,
    precheck_min_pmids=1,
    precheck_cache_ttl_days=30,
)

# Recorded for consumers, derived from the single configurable expression floor.
PARAMS["detection_log2"] = PARAMS["expression_floor_log2"]
