# Method 3 validation — 2026-11-07

Implements the **recommended branch-aware hybrid**, not the alternative
curator/strict-median rule. Inputs were read-only from
`/Users/foriskuang/Projects/agcg-control-eval`: `methods/followup_report.md`,
`controls-proposal.md`, `controls.yaml`, and the `followup_*` scripts. The
proposal's older P95-tail rule is superseded by the follow-up recommendation.
The repo's single authoritative contract remains [contract.md](contract.md);
no obsolete `docs/acgc-contract.md` duplicate was created.

The controls panel preserves 25 A positives, 10 B positives, 24 C negatives,
three regression negatives, and excluded ERBB2/ESCA with PMIDs/classes/outcomes.
It uses JSON-compatible YAML to keep loading stdlib-only. Original controls and
proposal SHA-256 are recorded in the panel. Reserves, exclusions and watchlist
entries from the evaluation are not added to the scored gate.

## Scientific validation

A separate read-only scan of the saved evaluation matrix recomputed all 116
curated/auxiliary pairs using the product `compute` implementation. Every
recommended final pass and broad flag exactly matched the saved evaluation.
The complete full-build parity/size results are recorded below.

The implementation uses population type-median CV<=0.5, >=90% detection in
>=29 types, >=10 measured samples in all 32 types; a broad-blocked unchanged
m2 lineage arm; and pooled-P99/Fisher subgroup with the recommended one-case
rest floor, >=10 cases, >=3%, >=3-fold ratio, P95 log2 shift>=1.5, and q<0.025.
The per-gene BH families remain 32 types. Existing wire fields are retained;
all new names and nullability are specified in the contract.

## Gates

From `expression_stats/`, its independent constrained Python 3.12 environment:

```sh
.venv/bin/ruff check .
.venv/bin/pytest -q
```

Lint passes; **149 collected test cases passed**. Coverage includes scientific
thresholds, broad demotion and subgroup rescue, lineage priority, missing-data
nulls, curated control fixtures, positive/negative/missing-control failures,
subset refusal, failure reports before PubMed, publisher refusal of unpassed
controls, panel fingerprint changes, and default/configurable PubMed rates.

Root environment and commands (the existing app dev environment is preserved
with `UV_NO_SYNC=1`; it contains no numpy, scipy or pandas):

```sh
UV_NO_SYNC=1 uv run ruff check
UV_NO_SYNC=1 uv run python -c "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('numpy','scipy','pandas'))"
env PATH="$PWD/.venv/bin:/usr/bin:/bin" UV_NO_SYNC=1 \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=63799 MYSQL_USER=acgc MYSQL_PASSWORD=acgc \
  MYSQL_DATABASE=acgc_test REDIS_URL=redis://127.0.0.1:63800/0 \
  ACGC_ALLOW_DESTRUCTIVE_TESTS=1 /opt/homebrew/bin/uv run pytest -q
```

Root ruff passes; **2,895 cases passed, 74 skipped**, 907 warnings, 140.51s
(2,969 collected app cases). Tests used fresh disposable MySQL/Redis and the
CI-like PATH without local LLM CLIs. Source application code/dependencies,
consumer tests/docs/removal guide, and `pages.yml` are unchanged.

Additional workflow validation from the repository root:

```sh
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 \
  .github/workflows/expression-stats-ci.yml \
  .github/workflows/expression-stats-release.yml
```

Both workflows pass. No GitHub release was created. First live publishing
remains untested; the prior PR verification checklist still applies, with a
fourth `controls-report.json` asset and manifest control summary now required.

## Eight-gene build with PubMed enabled

From `expression_stats/`, using the existing verified downloads:

```sh
OPENBLAS_NUM_THREADS=1 /usr/bin/time -l .venv/bin/python \
  -m gene_expression_relevance build \
  --datahub-commit "$(cat datahub-commit.txt)" --cache .cache \
  --out artifacts-m3-eight --genes ERBB2,KLK3,ALB,ESR1,TP53,GAPDH,B2M,MDM2
```

Eight genes, 9,696 primary samples, **63.82 seconds** in the builder (69.38s
wall including imports), maximum RSS **697,319,424 bytes**, peak memory
footprint **598,904,128 bytes**. PubMed ran for every passing pair, all with
counts>=1; local access was unkeyed at ~3/s. Valid existing cache entries were
reused. The subset's control status is `skipped_subset`, so it cannot publish.

| Gene | Broad | Passing cohorts (mode) |
| --- | --- | --- |
| ERBB2 | false | BRCA, STAD (subgroup) |
| KLK3 | false | KICH, PRAD (lineage) |
| ALB | false | CHOL, LIHC, PAAD (lineage) |
| ESR1 | false | BRCA, OV, UCEC (lineage) |
| TP53 | true | none |
| GAPDH | true | none |
| B2M | true | none |
| MDM2 | true | GBM, SARC (subgroup) |

TP53/UCEC is intentionally demoted by the recommended broad-baseline rule,
consistent with the evaluation's loss audit. Broad flags describe a baseline;
they do not imply a gene is biologically irrelevant. The eight-gene artifacts
remain ignored locally under `artifacts-m3-eight/`.

## Full real-data measurement

From `expression_stats/`:

```sh
OPENBLAS_NUM_THREADS=1 /usr/bin/time -l .venv/bin/python \
  -m gene_expression_relevance build \
  --datahub-commit "$(cat datahub-commit.txt)" --cache .cache \
  --out artifacts-m3-full --skip-precheck
.venv/bin/python scripts/bundle.py artifacts-m3-full bundle-m3-full
```

The pinned datahub snapshot is `a8ea891da32add81fabaa8e744cf8cb72a8a0e66`.
The existing local content-addressed cache contains the same verified TCGA
files used for the evaluation/preview matrix. Sample and gene selection are
unchanged. The full **statistical controls gate runs**; PubMed is deliberately
skipped for this size/runtime measurement. At ~40,000 calls and 3 requests/s,
full PubMed would take several hours. This bundle is review-only and must not
publish. The separate eight-gene build above exercises PubMed end to end.

Full result: **20,488 genes × 32 types**, 9,696 primary
samples, **39,935 passing pairs**: 35,592 lineage and
4,343 subgroup. **6,996 genes** are broadly expressed.
All **655,616** final pair calls and all broad flags match the saved
recommended evaluation exactly. The eight-gene calls match the separate subset
run. `lineage_blocked_by_broad` is true on 5,518 pairs, including
subgroup rescues; 5,008 formerly passing m2 pairs are demoted in the final OR.

| Scored class | Passing / total | Gate |
| --- | ---: | --- |
| A lineage positives | 25 / 25 | pass |
| B subgroup positives | 10 / 10 | pass |
| C negatives | 0 / 24 | pass |
| Regression negatives | 0 / 3 | pass |
| All positives | **35 / 35** | pass |
| All negatives | **0 / 27** | pass |
| ERBB2/ESCA | 0 / 1 | excluded from scoring |

Builder runtime **687.73s** (688.99s wall including imports),
maximum resident set size **2,456,928,256 bytes**
(~2.29 GiB), peak memory footprint **1,119,330,304 bytes**.
These are `/usr/bin/time -l` measurements on macOS and include the file-backed
matrix; compression/verification are outside the builder timer. Other validation
ran concurrently, so this is an observed operational measurement, not an
isolated performance benchmark. Raw output file bytes: **435,308,476**.

| Full bundle asset | Bytes |
| --- | ---: |
| `aliases.json` | 763,302 |
| `controls-report.json` | 14,107 |
| `manifest.json` | 10,064 |
| `genes.jsonl.gz` | 81,939,336 |
| Total | **82,726,809** (~82.73 MB) |

Eight-gene bundle: **59,234 bytes** across four assets.

Full review release identity: `datahub-a8ea891da32a-m3-d0af8a8884f9`.
Both bundles were independently decompressed and every line parsed. Compressed
SHA-256/size, gene counts, release IDs, pass-mode consistency and numeric diagnostic
bounds were verified. Reports and bundles remain ignored locally under
`artifacts-m3-full/`, `artifacts-m3-eight/`, `bundle-m3-full/`, `bundle-m3-eight/`.
No methodological deviation was made; the intentional full-PubMed skip and
existing contract path are documented above.
