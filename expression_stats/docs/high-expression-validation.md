# High-expression association validation (contract 1.6)

Implementation is stacked on reviewed PR #187 (`83a9348`) and retains the production
PM event rule. No thresholds were retuned. The new field is optional and detail only.

## Gates

| Working directory | Exact command | Result |
| --- | --- | --- |
| `expression_stats` | `.venv/bin/ruff check .` | passed |
| `expression_stats` | `.venv/bin/python -m pytest -q tests` | 322 test cases passed |
| repository root | `uv run --frozen ruff check .` | passed |
| repository root | `uv run --frozen pytest -q tests/test_expression_context*.py` | 475 test cases passed, 9 skipped |
| repository root | `node tests/frontend/test_expression_context_page.js` | 51 test cases passed |

Tests cover all association gates, constant expression and percentile ties, finite
profiled denominators, BH families excluding low-event cohorts, direction/driver
scope, build controls, publisher re-derivation/tampering, CSV injection protection,
with/without API/export/history agreement, and safe frontend rendering.

## Full local build and comparison

The local run calls the full production `build()` with all genes, alteration inputs,
purity and PubMed prechecks enabled. Saved LFS pointers from the validated analysis
verify all 32 expression objects. The run reuses the read-only TCGA cache through a
local symlink overlay; the source cache is unchanged. It overrides only remote input
lookup and cohort metadata retrieval with the saved input IDs and published mapping,
because remote metadata requests stalled. Every statistic is recomputed. The assembled raw expression matrix is reused on
rerun (20,488 rows, one column per selected tumor, float64 linear RSEM; original
matrix builder, mapping, gene order and sample selection). This avoids repeating
matrix assembly after releasing parsed alteration inputs to reduce memory use;
no expression statistics are copied from the published bundle.

All source PubMed caches were empty. The local cache was reconstructed from the
published bundle's 39,935 counts using its generation timestamp
`2026-11-14T17:09:29.407650+00:00` (conservative freshness). The production precheck
still applies its normal query match and staggered expiry checks. No PubMed counts
are fabricated or refreshed for this comparison.

Exact local invocation (the runner is retained locally):

```sh
cd expression_stats
.venv/bin/python /tmp/build_highexp_cached.py
```

Comparison baseline:
`expression-stats-datahub-a8ea891da32a-m3-13c84dbb1c63-pubmed-20261114`.
Release identity, contract version, control metadata and generation/provenance fields
necessarily change. The comparison removes only the new field from pair records and
compares compact JSON bytes for every existing pair field, then compares complete
gene documents allowing their new release ID.

The locally assembled raw matrix and the validated analysis's
`agcg-control-eval/purity/expression.bin` are byte-identical: both are 1,589,213,184
bytes (20,488 × 9,696 float64 values), SHA-256
`df62bb570857ce1133a38fb0d0998b9625cd5de6d37424373ff154b4e75b44e6`.


## MET versus the curator ranking

Association results are independent of purity and expression surfacing. NSCLC
matches **LUAD amp and SV** (also PM), while LUSC does not pass. STAD/ESCA and GBM
amplification pass. BRCA amplification is borderline: its top-decile Fisher
p=0.05216 narrowly misses the strict 0.05 gate despite delta=2.151 and MW q=0.01579.
KIRP amplification passes; KIRP PM has zero events under the unchanged production
mutation driver rule. This rule does not recover the curator's KIRP mutation
ranking; it was deliberately not widened or retuned. The single GBM SV is an
anecdote and never a call. No MET deep-deletion track is called.

| Cohort | Track | Status | Altered/profiled | Δ median log2 | MW q | k top10 | OR top10 | Fisher p top10 |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| LUAD | amp | associated_high_expression | 11/507 | 3.532 | 4.797e-06 | 9 | 48.64 | 2.516e-08 |
| LUAD | SV | associated_high_expression | 3/510 | 3.764 | 0.0275 | 2 | 18.69 | 0.02757 |
| LUAD | PM | associated_high_expression | 9/434 | 1.044 | 0.00655 | 4 | 7.7 | 0.00798 |
| LUSC | amp | not_associated | 7/484 | 0.6451 | 0.03113 | 2 | 3.66 | 0.1517 |
| LUSC | SV | not_testable | 0/481 | — | — | 0 | — | — |
| LUSC | PM | not_associated | 3/445 | -0.7008 | 0.3476 | 0 | 0 | 1 |
| STAD | amp | associated_high_expression | 12/410 | 2.707 | 6.28e-06 | 10 | 59.19 | 1.93e-09 |
| STAD | SV | not_testable | 0/409 | — | — | 0 | — | — |
| STAD | PM | not_testable | 0/331 | — | — | 0 | — | — |
| ESCA | amp | associated_high_expression | 6/181 | 2.854 | 0.01899 | 4 | 21.33 | 0.001168 |
| ESCA | SV | not_testable | 0/181 | — | — | 0 | — | — |
| ESCA | PM | not_testable | 0/178 | — | — | 0 | — | — |
| GBM | amp | associated_high_expression | 3/148 | 3.482 | 0.01708 | 2 | 20.31 | 0.02724 |
| GBM | SV | anecdotal | 1/152 | 4.166 | — | 1 | Infinity | 0.1053 |
| GBM | PM | not_testable | 0/149 | — | — | 0 | — | — |
| BRCA | amp | not_associated | 4/1068 | 2.151 | 0.01579 | 2 | 9.133 | 0.05216 |
| BRCA | SV | not_testable | 1/1064 | 1.484 | — | 0 | 0 | 1 |
| BRCA | PM | not_testable | 0/1048 | — | — | 0 | — | — |
| KIRP | amp | associated_high_expression | 5/283 | 1.98 | 0.003924 | 4 | 40.48 | 0.0004214 |
| KIRP | SV | not_testable | 1/276 | 0.2252 | — | 0 | 0 | 1 |
| KIRP | PM | not_testable | 0/274 | — | — | 0 | — | — |


## High-expression positive controls

| Gene | Cohort | Category | Altered/profiled | Δ median | MW q | k top10 | OR top10 | Fisher p |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| ERBB2 | BRCA | Copy Number Alteration (amp) | 122/1068 | 3.7621 | 4.2836e-65 | 99 | 504.68 | 1.6594e-106 |
| ERBB2 | STAD | Copy Number Alteration (amp) | 55/410 | 2.8636 | 7.8457e-29 | 38 | 262.27 | 1.0082e-36 |
| MDM2 | SARC | Copy Number Alteration (amp) | 47/251 | 4.5724 | 5.0815e-23 | 20 | 24.444 | 5.9061e-12 |
| CDK4 | SARC | Copy Number Alteration (amp) | 44/251 | 4.4087 | 4.4496e-23 | 24 | 123 | 2.3684e-19 |
| EGFR | GBM | Copy Number Alteration (amp) | 74/148 | 3.9176 | 1.0271e-18 | 14 | 17.033 | 0.0002706 |
| MET | LUAD | Copy Number Alteration (amp) | 11/507 | 3.5323 | 4.7972e-06 | 9 | 48.643 | 2.5158e-08 |
| MET | LUAD | Structural Variant | 3/510 | 3.7637 | 0.0275 | 2 | 18.694 | 0.027575 |
| ALK | LUAD | Structural Variant | 5/510 | 6.1884 | 0.00027203 | 5 | Infinity | 8.3323e-06 |
| ROS1 | LUAD | Structural Variant | 7/510 | 2.7995 | 2.0809e-05 | 6 | 61.067 | 4.9063e-06 |


## Full-build outcome and analysis agreement

Release ID: `datahub-a8ea891da32a-m3-fc8a5acec074`. The full build completed in
1,382.10 seconds with 20,488 genes ×32 cohorts = 655,616 pairs and 9,696 selected
primary tumors. All controls passed, with no missing results or failures:

| Panel | Required positives | Negative calls |
| --- | ---: | ---: |
| Existing expression | 51/51 | 0/27 |
| Existing frequency alterations | 13/13 | 0/65 (previously 66; MET moved) |
| New high-expression association | 9/9 | 0/384 |

The publisher's `controls.recheck()` also passed against the full generated gene
documents. The artifact's code fingerprint matches the final source code.

| Scope | Associated category calls | Additional calls vs frequency enrichment |
| --- | ---: | ---: |
| High-confidence pairs | 79 (17 amp, 60 SV, 2 PM) | +12 |
| All stats-pass pairs | 124 (40 amp, 80 SV, 4 PM) | +33 |
| All cohorts of genes with a high-confidence pair | 495 (222 amp, 263 SV, 10 PM) | +290 |
| All genes and cohorts | 943 (481 amp, 449 SV, 13 PM) | +603 |

No deep-deletion category is called. Calls here count gene × cohort × category,
not distinct gene/cohort pairs. The implementation has **zero call disagreements**
with the saved validated `pangene_matrices.npz` across all 20,488 ×32 ×4 tracks,
using the fixed rule and eligibility masks. The saved matrices themselves produce
124 stats-pass calls / +33 additions and 943 total calls; report.md's narrative
110 / +28 and 950 total are stale relative to those saved analysis results. No
threshold or event rule was changed to obtain these totals.

## Published-byte comparison: unmet numerical gate

**Exact byte equality versus the published release is NOT met by this local
ARM/macOS rebuild.** All 655,616 existing pair records were compared after removing
only `alteration_high_expression`. Every non-numeric/decision field and every
existing frequency-alteration field is identical. `stats_pass`, pass modes,
confidence tiers, strong arms, purity decisions, eligibility flags and PubMed
fields have zero changes. Existing numeric fields have differences bounded by
**1.7763568394002505e-15 absolute**; 20,264 gene documents contain at least one such
difference. This is a validation limitation, not a claim of byte equality.

| Existing numeric field | Differing pairs | Maximum absolute difference |
| --- | ---: | ---: |
| q_value | 366,346 | 1.1102230246251565e-15 |
| p_value | 382,579 | 3.3306690738754696e-16 |
| purity_q | 15,862 | 6.6613381477509392e-16 |
| p90_log2 | 188 | 1.7763568394002505e-15 |
| rest_median_log2 | 173 | 1.7763568394002505e-15 |
| p95_log2_diff | 180 | 1.7763568394002505e-15 |
| rest_p90_log2 | 193 | 1.7763568394002505e-15 |
| median_log2 | 207 | 1.7763568394002505e-15 |
| log2fc | 136 | 1.7763568394002505e-15 |
| log2fc_vs_median_of_type_medians | 182 | 1.7763568394002505e-15 |

For example, A1BG-AS1/ACC p changes from `2.9552106348249804e-06` to
`2.955210634824981e-06`. A standalone Linux ARM probe over 10 genes / 320 pairs
also differs from the published p/q bytes, supporting a platform/numerical
reproducibility explanation. The existing expression and purity computation
source files are unchanged by this PR. No numeric rounding, copying of published
statistics, threshold tuning or alteration of the existing method was introduced
to hide this mismatch. The exact with/without detail agreement test passes.

Stable manifest fields also match, except the candidate-miss diagnostic q values
show the same numerical drift. Intentional administrative differences are contract
1.6, release ID, code/build hashes, generation/expiry times, the controls panel hash,
the frequency negative total changing 66→65, and the added association metadata.

Operational deviations: remote metadata retrieval was replaced locally by saved
verified input pointers and the published cohort mapping; the assembled raw matrix
was cached for the final rerun; PubMed counts were reconstructed from the published
snapshot because the source caches were empty. `uv.lock` was generated locally to
run the requested frozen gates and is intentionally not committed.
