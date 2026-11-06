# Relocation validation — 2026-11-06

The initial results below describe commit `4a456a1`. Current review-fix gates
are recorded in the final section.

Ported from read-only `gene-expression-relevance-wt-stats` at
`a7c7cc6d320f3d2c64d444e0a6d145dc2dfa6bc4`. Statistical implementation,
fetching, symbol resolution, precheck, provenance, pins and exact constraints
were compared byte-for-byte against the source. The builder only stops copying
the removed search page; method v2 is unchanged. The source checkout stayed clean.

All commands below run in the ACGC worktree unless stated otherwise.

## Gates

In `expression_stats/`, using its independent constrained Python 3.12 environment:

```sh
.venv/bin/ruff check .
.venv/bin/pytest -q
.venv/bin/pytest tests/test_isolation.py -q
```

Ruff passes. Pipeline tests: **108 collected cases, 108 passed** (57 test
functions, including parametrized functions). Isolation: **1 case passed**;
checks Python imports in both `expression_stats/` and `src/` using AST without
loading the application. Root pytest's unchanged `testpaths = ["tests"]`
collects **2,968 app cases**, excluding pipeline tests.

ACGC lint (root):

```sh
.venv/bin/ruff check .
node --check src/static/app.js
```

Both pass. Root ruff explicitly excludes `expression_stats/`; its separate
workflow runs the pipeline's stricter ruff configuration.

ACGC tests (root), with fresh disposable `mysql:8` and `redis:7-alpine`
containers on host ports 58670 and 58675:

```sh
.venv/bin/python -c "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('numpy','scipy','pandas'))"
env PATH="$PWD/.venv/bin:/usr/bin:/bin" \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=58670 MYSQL_USER=acgc MYSQL_PASSWORD=acgc \
  MYSQL_DATABASE=acgc_test REDIS_URL=redis://127.0.0.1:58675/0 \
  ACGC_ALLOW_DESTRUCTIVE_TESTS=1 .venv/bin/pytest tests/ -v -rA
```

**2,894 cases passed, 74 skipped**, 907 warnings, 72.20 seconds. No application
tests or application code were changed. The explicit PATH mirrors CI where
local agent CLIs are absent. An initial run with the developer PATH found an
unauthenticated Claude CLI and failed its real CLI integration test; that test
skips with the CI-like PATH. The disposable containers were stopped afterward.

Workflow syntax:

```sh
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 \
  .github/workflows/expression-stats-ci.yml \
  .github/workflows/expression-stats-release.yml
```

Passes. Checking the existing Docker Hub workflow additionally reports its
pre-existing obsolete action versions; only its requested job guard was changed.

Image validation:

```sh
docker build -t acgc-expression-stats-validation .
docker run --rm acgc-expression-stats-validation python -c "from pathlib import Path; import importlib.util; assert not Path('/app/expression_stats').exists(); assert all(importlib.util.find_spec(n) is None for n in ('numpy','scipy','pandas'))"
```

Passes. Application dependencies and Dockerfile are unchanged; `.dockerignore`
explicitly excludes the producer.

## Real five-gene build

In `expression_stats/`:

```sh
.venv/bin/python -m gene_expression_relevance build \
  --datahub-commit "$(cat datahub-commit.txt)" --out artifacts-five \
  --genes ERBB2,ALB,KLK3,ESR1,TP53
.venv/bin/python scripts/bundle.py artifacts-five bundle-five
```

Real pinned datahub input `a8ea891da32add81fabaa8e744cf8cb72a8a0e66`, verified
HGNC snapshot and copied content-addressed download/PubMed cache; 9,696 primary
samples, 31.34 seconds. Precheck enabled; all stats-passing pairs have real
PubMed counts and pass the precheck.

| Gene | Stats-passing cohorts (PubMed counts) |
| --- | --- |
| ERBB2 | none |
| ALB | CHOL (48), LIHC (472), PAAD (62) |
| KLK3 | KICH (5), PRAD (333) |
| ESR1 | BRCA (2075), OV (175), UCEC (147) |
| TP53 | UCEC (731) |

Five-gene assets: `genes.jsonl.gz` **17,666 bytes**, `manifest.json`
**9,515 bytes**, `aliases.json` **201 bytes**; **27,382 bytes total**.
Local generated files remain ignored and available under `bundle-five/`.

No GitHub release was created. Publication tests mock GitHub and exercise
upload failure, download corruption, existing-tag skips, review-only rejection,
draft-first publication, `make_latest=false`, and paginated discovery by
`published_at`. Live GitHub asset upload/undraft is intentionally untested.

## Full 20,488-gene size measurement

```sh
cd expression_stats
.venv/bin/python -m gene_expression_relevance build \
  --datahub-commit "$(cat datahub-commit.txt)" --out artifacts-full --skip-precheck
.venv/bin/python scripts/bundle.py artifacts-full bundle-full
```

Completed **20,488 genes**, 9,696 primary samples, in **370.17 seconds**.
PubMed precheck was deliberately skipped for this size measurement: the copied
cache held only 16 pairs, so full PubMed coverage was not run locally. This
bundle is explicitly **review-only and cannot be published**. The five-gene
build above ran with precheck enabled. Full build reproduces all five expected
statistical pass lists.

| Asset | Bytes |
| --- | ---: |
| `genes.jsonl.gz` | 66,546,261 |
| `manifest.json` | 9,386 |
| `aliases.json` | 763,302 |
| Total | **67,318,949** (~67.3 MB) |

Both local bundles were decompressed and every JSON line parsed; gene counts,
release IDs and compressed SHA-256 were verified against their manifests.
Generated full files remain ignored under `artifacts-full/` and `bundle-full/`.

## Cross-vendor review fixes — current gates

The root stdlib-only AST guard now runs in app CI; pipeline CI also triggers
on `src/**`. Packaging exports only `src` (`pyproject.toml` wheel packages and
`annotate` entry point; Docker runs `src.main:app`). Adding `src/` to sys.path
also exposes its immediate modules/packages, so the pipeline guard rejects
those bare names as well. The root guard scans app files regardless of how they
are imported and was exercised against forbidden static/dynamic imports and
with the producer directory absent.

Pipeline commands, from `expression_stats/`:

```sh
.venv/bin/ruff check .
.venv/bin/pytest -q
```

Ruff passes; **114 collected test cases passed** (61 test functions) in 5.65s.
Coverage includes draft cleanup and cleanup failure preserving the original
error, stale draft rejection, target commit, workflow token/job boundaries,
branch review builds, concurrency configuration, and root guard removal.

Root commands, with fresh disposable MySQL/Redis containers:

```sh
.venv/bin/ruff check .
.venv/bin/python -c "import importlib.util; assert all(importlib.util.find_spec(n) is None for n in ('numpy', 'scipy', 'pandas'))"
env PATH="$PWD/.venv/bin:/usr/bin:/bin" \
  MYSQL_HOST=127.0.0.1 MYSQL_PORT=49268 MYSQL_USER=acgc MYSQL_PASSWORD=acgc \
  MYSQL_DATABASE=acgc_test REDIS_URL=redis://127.0.0.1:49269/0 \
  ACGC_ALLOW_DESTRUCTIVE_TESTS=1 .venv/bin/pytest tests/ -v -rA
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.12 \
  .github/workflows/expression-stats-ci.yml \
  .github/workflows/expression-stats-release.yml
```

Root ruff and actionlint pass. **2,969 app cases collected; 2,895 passed,
74 skipped**, 907 warnings, 50.57 seconds. This adds exactly one root isolation
case. The environment contains no numpy/scipy/pandas. Source application code,
existing app tests, `pages.yml`, and the reader work in #167 remain untouched.

No releases were created. **First live publish remains a known risk.** On the
first authorized main publish, verify tag target equals building `GITHUB_SHA`,
all assets download and match manifest size/hash/count, the release is neither
draft nor prerelease, and the application release remains Latest. Confirm a
repeat does not create duplicates, branch dispatch only produces review
artifacts, failed uploads/verification clean up their own draft (or surface an
explicit stale-draft error if cleanup failed), and Docker Hub is not triggered.
