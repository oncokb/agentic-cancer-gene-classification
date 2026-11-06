# Method v2 smoke validation — 2026-11-05

This report validates the current pinned-dependency implementation, including
stable release IDs, cohort mapping identity and the dated HGNC archive.
The [ACGC contract](contract.md) remains the single source of truth.

## Fresh HGNC archive verification

Downloaded the archive afresh (without reusing cached bytes):

`https://storage.googleapis.com/public-download-files/hgnc/archive/archive/quarterly/tsv/hgnc_complete_set_2026-10-06.tsv`

- Downloaded bytes: 16,973,125.
- Verified SHA-256: `8bf6f686e640dbc864a346484626953621b0500d900d26cd085a35c2782d725d`.
- Verification time: `2026-11-05T21:46:09.928060+00:00`.
- The digest exactly matches `gene_expression_relevance/hgnc-pin.json`.
  The newly verified bytes were then used by the smoke build.

## Real five-gene build

```sh
pip install -c constraints.txt setuptools
pip install --no-build-isolation -c constraints.txt -e '.[dev]'
/usr/bin/time -l .venv/bin/python -m gene_expression_relevance build \
  --datahub-commit a8ea891da32add81fabaa8e744cf8cb72a8a0e66 \
  --out site --genes ERBB2,ALB,KLK3,ESR1,TP53
```

Python 3.12.14, macOS arm64. Exact dependency versions are committed in
`constraints.txt`, including numpy 2.5.4, scipy 1.18.1,
pandas 2.3.3 and setuptools 84.2.1.
All 32 studies were checked against pinned LFS object hashes. Expression
objects were reused from cache; statistics were recomputed over 9,696 retained
primary tumors. Fresh, TTL-valid SQLite PubMed counts were reused for all
nine passing pairs using `"GENE"[tiab] AND (<tumor query>)`.

**19.25 seconds wall time**, **728,662,016 bytes peak RSS** (694.9 MiB).

| Gene | Stats + precheck passing cohorts | Change from previous v2 smoke |
| --- | --- | --- |
| ERBB2 | None | None |
| ALB | CHOL, LIHC, PAAD | None |
| KLK3 | KICH, PRAD | None |
| ESR1 | BRCA, OV, UCEC | None |
| TP53 | UCEC | None |

All nine candidates passed the PubMed precheck. ALB→LIHC and KLK3→PRAD retain
the informational tissue-of-origin flag. No passing cohort changed after
switching to the dated HGNC archive. ERBB2 remains a documented limitation of
median-based enrichment; p90 fields remain informational. The downstream ACGC
literature gate was not run.

## Artifact provenance

- Release: `datahub-a8ea891da32a-m2-2ed8a257946f`.
- Code/dependency fingerprint: `058209c3b0c269b2eb8ed8b17fb9e08bdd2a66a84cdc9f6ef7b06efb659a2136`.
- Cohort mapping fingerprint: `e06f90a4285295645b813d1cc4229ecc24e9b1e92ed67ba5e6edbade01cced8b`.
- Build input fingerprint: `2ed8a257946fe28e20aa038f95f57616ed3a8a846b0de504963debe0e0413178`.

The code fingerprint matches this checkout's package sources, pyproject,
constraints and resolved numpy/scipy/pandas versions. The mapping hash matches
the published cohort list's study ID, cohort code, name and disease query,
sorted by study ID. Import dates do not affect identity. All five gene files
and the manifest validate against the current JSON Schemas and share the
release ID above. Recomputing all provenance hashes reproduces the manifest.

## Verification

- `.venv/bin/ruff check .`: passed for all repository Python files.
- `.venv/bin/pytest`: **98 collected cases passed across 52 test functions**.
  Files: `tests/test_contract.py`, `tests/test_fetch.py`, `tests/test_genes.py`,
  `tests/test_mapping.py`, `tests/test_precheck.py`, `tests/test_provenance.py`,
  `tests/test_stats.py`, `tests/test_workflow.py`. Socket connections are blocked
  by `tests/conftest.py`.
- `.venv/bin/pip check`: no broken requirements.
- Constrained editable installation succeeds with the generated site present;
  package discovery includes only `gene_expression_relevance`.
- Tests verify empty gene input becomes None and the workflow omits --genes;
  cohort field changes invalidate identity/early exit while import dates and
  ordering do not; TTL refresh can change a gene's precheck false→true without
  changing release_id. Existing statistics, schema, fetch and workflow tests
  also pass.

The current follow-up reran the five-gene build, not a full-gene build. The
prior full-gene timing is not presented as a measurement of this checkout.
Generated artifacts and downloaded data remain ignored by git. No repository
creation, push, PR or deployment was attempted.
