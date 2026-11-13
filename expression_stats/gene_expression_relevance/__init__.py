"""Tumor-type expression enrichment; not tumor versus normal."""

METHOD_VERSION = "3"
# Release-bundle contract minor version (schema_version stays "1"; changes are additive).
CONTRACT_VERSION = "1.2"
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
    precheck_min_pmids=1,
    precheck_cache_ttl_days=30,
)

# Recorded for consumers, derived from the single configurable expression floor.
PARAMS["detection_log2"] = PARAMS["expression_floor_log2"]

# Alteration-type statistics are detail only: they never feed stats_pass, pass_mode,
# broadly_expressed or any expression gate. Versioned and parameterized separately so
# the expression method (and its release_id "m3" token) is unchanged.
# "2": CNA restricted to pinned copy-number drivers with role-matched direction; Point
# Mutation eligibility widened with pinned curator-annotated genes.
ALTERATION_METHOD_VERSION = "2"
POINT_MUTATION = "Point Mutation"
STRUCTURAL_VARIANT = "Structural Variant"
COPY_NUMBER_ALTERATION = "Copy Number Alteration"
# Curator-sheet wording; also the order of alteration_types_enriched.
ALTERATION_CATEGORIES = (POINT_MUTATION, STRUCTURAL_VARIANT, COPY_NUMBER_ALTERATION)
ALTERATION_PARAMS = dict(
    fdr=0.05,
    min_profiled=20,
    mutation_driver_source="bailey2018+new_framework",
    # Copy Number Alteration is callable only for these genes; others are not_testable.
    cna_driver_source="bailey2018+new_framework+sanchezvega2018",
    # Oncogene-only: amplification only; TSG-only: deep deletion only; else both.
    cna_direction_by_role=True,
    hypermutator_max_nonsynonymous=500,
    missense_min_recurrence=10,
    inframe_min_recurrence=5,
    gates={
        POINT_MUTATION: dict(min_events=5, min_frequency=0.02, min_ratio=2.0),
        COPY_NUMBER_ALTERATION: dict(min_events=5, min_frequency=0.02, min_ratio=2.0),
        # Rare-event track (alterations report proposal): fusions are sparse and
        # assay-limited, so fewer events than min_events is insufficient_data.
        STRUCTURAL_VARIANT: dict(min_events=3, min_frequency=0.005, min_ratio=3.0),
    },
)
