> Historical source-repository report; its Pages deployment plan is superseded
> by [contract.md](contract.md). No Pages deployment is used by this port.

# Real-data validation — 2026-11-05

Datahub commit: `a8ea891da32add81fabaa8e744cf8cb72a8a0e66`.
Python 3.12.14, macOS arm64. All 32 studies were downloaded and verified against
LFS SHA-256 pointers. Filtering retained 9,696 primary tumors, one per patient.
HGNC snapshot SHA-256:
`3944cdad7affce5bc2c7d63d7079d7e68762ed28ef414fc287cc0f57c4b0d78f`.

## Five-gene CLI run

```sh
/usr/bin/time -l .venv/bin/python -m gene_expression_relevance build \
  --datahub-commit a8ea891da32add81fabaa8e744cf8cb72a8a0e66 \
  --out site --genes ERBB2,ALB,KLK3,ESR1,TP53
```

31.86 seconds wall time, 744,128,512 bytes maximum resident set size (710 MiB).
Expression downloads and HGNC were cached; PubMed requests used the final
SQLite cache implementation with eight workers and the shared unkeyed limiter.
All 16 stats-passing pairs had live PubMed counts ≥1 and precheck=true.
These are enrichment/precheck candidates; the downstream literature gate is
outside this repository's scope.

| Gene | Stats + precheck passing cancer types |
| --- | --- |
| ERBB2 | None |
| ALB | CHOL, KIRC, LGG, LIHC, PAAD, PCPG |
| KLK3 | ACC, COADREAD, KICH, PRAD |
| ESR1 | BRCA, OV, PRAD, THCA, UCEC |
| TP53 | UCEC |

ALB→LIHC: n=366, median difference 18.042, Cliff's delta 0.998,
504 PubMed records, `tissue_of_origin_risk`.
KLK3→PRAD: n=493, median difference 18.507, Cliff's delta approximately 1,
1,621 PubMed records, `tissue_of_origin_risk`.
ERBB2→BRCA has median difference 0.944, so it fails the required ≥1 threshold
despite q≈3.11e−165 and Cliff's delta≈0.512. No thresholds were tuned to the
expected biological examples.

## Full-gene measurement

```sh
/usr/bin/time -l .venv/bin/python -m gene_expression_relevance build \
  --datahub-commit a8ea891da32add81fabaa8e744cf8cb72a8a0e66 \
  --out .cache/full-site --skip-precheck
```

20,488 resolved genes; 141.62 seconds wall time; 2,605,907,968 bytes maximum
resident set size (2.43 GiB). Downloads and HGNC were cached. PubMed was skipped.
65,055 pairs passed the statistics gate. Full-gene artifacts are local and ignored.
The full statistics and five-gene builds produced identical statistical values
for every comparison of the five selected genes.

A fresh full PubMed pass has a rate-limit floor of about 1.83 hours with a key
or 6.05 hours without one, **in addition to** statistics/download time. These
are extrapolated lower bounds from candidate count and the configured request
intervals; latency and retries can extend them. Eight concurrent workers hide
HTTP latency while sharing the global limiter. Persistent SQLite caching makes
subsequent runs reuse successful counts. The hosted workflow requires a key
for a full precheck to fit the [six-hour hosted job limit](https://docs.github.com/en/actions/reference/limits).
A full live PubMed pass was not attempted locally.

## Verification

- `.venv/bin/ruff check .`: passed, all repository Python files including `.github/check_build.py`.
- `.venv/bin/pytest`: passed, 46 collected test cases across 32 test functions.
  File set: `tests/test_contract.py`, `tests/test_fetch.py`, `tests/test_genes.py`,
  `tests/test_mapping.py`, `tests/test_precheck.py`, `tests/test_stats.py`, with
  the network-blocking fixture in `tests/conftest.py`.
- `.venv/bin/python -m compileall -q gene_expression_relevance .github/check_build.py`: passed.
- `node --check .cache/site-script.js`: passed (script extracted from packaged HTML).
- Five-gene real manifest, aliases and gene artifacts validate against the checked-in
  JSON Schemas. Full-build inspection checks all 20,488 gene files for 32 types,
  matching release IDs, filenames, alias targets and the five-gene statistical parity.

## Review points and deployment prerequisites

No blocking implementation questions remain. Method v1 explicitly documents
arithmetic-mean collapsing of duplicate Entrez rows and normalization of current
API cancerTypeIds into the contract's TCGA study labels. Review those conventions
when evolving the method or integrating another OncoTree-based consumer. The
lineage-marker heuristic is intentionally limited and informational.

The GitHub repository does not yet exist. No repository creation, push or PR was
attempted. After an org admin creates it, enable Pages through Actions, configure
`NCBI_API_KEY` for full prechecks, push the branch and open the PR. Hosted Actions
and Pages deployment cannot be exercised before that provisioning.
