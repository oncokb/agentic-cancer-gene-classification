# Tumor-purity R2 validation

Implementation is stacked on PR #176 (`b2baa1777e48bb253e3851aefe83629b28ddb22f`),
with no threshold retuning. Contract 1.4 is a demotion-only gate; see
[the contract](contract.md#contract-14-supplementary-tumor-purity-gate).

## Required checks

Commands from the repository root, except the two pipeline commands run inside
`expression_stats/` using Python 3.12 and `constraints.txt`:

| Command | File set | Result |
| --- | --- | --- |
| `.venv/bin/ruff check .` | `expression_stats/` | passed |
| `OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q tests` | `expression_stats/tests/` | 284 collected test cases passed |
| `uv run --frozen ruff check .` | repository configured lint scope | passed |
| `uv run --frozen pytest -q tests/test_expression_context*.py` | expression-context Python tests | 434 passed, 9 skipped (443 collected test cases) |
| `node tests/frontend/test_expression_context_page.js` | expression-context page frontend | 42 test functions passed |

Python counts are collected parameterized test cases, not test function counts.
The nine skips are environment-dependent MySQL/Redis integration checks. The
existing pytest-asyncio fixture-loop-scope deprecation remains. No typecheck
command is configured for these modules. The checkout has no tracked `uv.lock`;
a local copy of the existing environment lock enabled the required frozen
commands and is excluded from the commit.

Revert check: temporarily changed the R2 demotion return to `pass`, ran
`.venv/bin/python -m pytest -q tests/test_purity.py::test_finalize_global_family_and_only_tier_mutates`
inside `expression_stats/`, and confirmed exit 1: expected one demotion, observed
zero. Restored the gate and reran the full green suites above.

## Full-build comparison

Both builds run attached to foreground command sessions and are awaited to
completion. Pinned datahub commit:
`a8ea891da32add81fabaa8e744cf8cb72a8a0e66`. Both include alterations and use
`--skip-precheck`, matching the analysis input build; neither is a publishable
prechecked release. Publication controls and publish-time rechecks are unchanged
and covered by the pipeline tests.

Current build, from `expression_stats/`:

```sh
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m gene_expression_relevance build \
  --datahub-commit a8ea891da32add81fabaa8e744cf8cb72a8a0e66 \
  --out artifacts-purity --cache .cache --skip-precheck
```

Baseline: `git archive b2baa17 expression_stats` extracted inside this worktree,
then from the repository root:

```sh
OPENBLAS_NUM_THREADS=1 \
PYTHONPATH=expression_stats/artifacts-baseline-source/expression_stats \
expression_stats/.venv/bin/python -m gene_expression_relevance build \
  --datahub-commit a8ea891da32add81fabaa8e744cf8cb72a8a0e66 \
  --out expression_stats/artifacts-baseline \
  --cache expression_stats/.cache --skip-precheck
```

Release: `datahub-a8ea891da32a-m3-fa431b47c463` (contract 1.4).
Baseline rebuilt from b2baa17: `datahub-a8ea891da32a-m3-0a390aae8666`.
The build fingerprint matches the final pipeline source.

| Full-build measure | Result |
| --- | ---: |
| Genes / selected primary samples | 20,488 / 9,696 |
| Cohorts / pair records | 32 / 655,616 |
| v3 passes (unchanged) | 39,935 |
| High before / after purity | 16,407 / 16,073 |
| Demoted / candidate after purity | 334 / 23,862 |
| High-pair checks: pass / unassessed / not applicable / demoted | 13,464 / 215 / 2,394 / 334 |
| Purity full-build runtime | 497.71 seconds |
| Baseline full-build runtime | 418.99 seconds |
| Compressed `genes.jsonl.gz` | 119,982,535 bytes |
| All bundle assets | 120,861,989 bytes |

| Controls | v3 passes | High | Candidate |
| --- | ---: | ---: | ---: |
| Positive | 51/51 | 45 | 6 |
| Negative | 0/27 | 0 | 0 |

Alteration controls also pass: 13/13 positives, 0/66 negatives enriched.
The full-build publish-time `controls.recheck` returns no problems.

The demoted `(gene, tcga)` set equals all 334 rows of `demoted_R2.csv`
from the read-only R2 analysis exactly:
**0 extra, 0 missing**, so no discrepancy requires explanation.

For every gene and all 32 cohorts, compared the b2baa17 build against the purity
build: every pre-existing scientific field (including v3 calls/labels, all effect
sizes/tests, broad flags, precheck fields and complete alterations) has identical
serialized JSON bytes. The comparison removes the three additive `purity_*`
fields and restores the original tier solely for the 334 demotions. All **655,282
non-demoted tiers** match unchanged. Gene metadata also matches; release identity
and provenance necessarily change. No baseline scientific-field differences
were found. Build outputs, downloaded caches and local verification outputs are
excluded from the commit.

Review follow-ups add real-compute demotion/BH and shuffled-sample alignment
coverage, pin the CI object cache key to the purity hash, and warn once per bundle
about high pairs missing the required purity field. Pipeline source and its
fingerprint are unchanged from the full build above; no rebuild was needed.
