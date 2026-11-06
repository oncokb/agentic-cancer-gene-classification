# Expression context (optional, removable module)

Status: artifact v1, statistical method v2 — 2026-11-05

## Goal

Given a gene, surface only the (gene, cancer type) pairs where:

1. TCGA PanCancer Atlas RNA-seq expression is **statistically significantly
   elevated in that cancer type relative to the other TCGA tumor types**
   (tumor-only data: this is *tumor-type enrichment*, NOT tumor-vs-normal), and
2. the literature characterizes the gene in that tumor type.

Each surfaced pair shows the statistics and the supporting citations side by side.

## Architecture (hybrid)

```
 ┌────────────── expression_stats/ (isolated producer) ──────────────┐
 │ GitHub Action (cron weekly + manual)                       │
 │  1. download 32 *_tcga_pan_can_atlas_2018                  │
 │     data_mrna_seq_v2_rsem.txt from cBioPortal datahub      │
 │     (pinned commit)                                        │
 │  2. stats gate for every gene (one-vs-rest, see below)     │
 │  3. cheap PubMed-count pre-check for stats-passing pairs   │
 │  4. publish verified GitHub release bundles                 │
 └───────────────────────┬────────────────────────────────────┘
                         │  GitHub release download → local MySQL import
                         ▼
 ┌────────────── ACGC: src/expression_context/ ──────────────┐
 │ opt-in router (flag EXPRESSION_CONTEXT_ENABLED)            │
 │  - reads precomputed hits for the gene                     │
 │  - literature gate on demand via LiteratureGate adapter    │
 │    (v1: ACGC pipeline; later: LRAS /lras/v1/retrieve)      │
 │  - stores joined verdicts in its own MySQL table           │
 └────────────────────────────────────────────────────────────┘
```

No new instances: the stats side is GitHub Actions + release assets; the ACGC side is a
route on the existing deployment, off by default.

## Removability requirements (hard)

The consumer module remains independently removable. Producer removal is
documented in [../REMOVAL.md](../REMOVAL.md).

- All code lives in `src/expression_context/` (router, artifact client, service,
  literature-gate adapter, store, settings, models). Nothing else in `src/`
  imports from it except ONE registration call in `src/main.py`.
- Own settings class with env prefix `EXPRESSION_CONTEXT_` — do **not** add
  fields to `src/config.py`.
- `EXPRESSION_CONTEXT_ENABLED` defaults to `false`. When false: router not
  mounted, table not created, no outbound calls, no extra startup work.
- Own table `expression_context_results`, created by the module only when
  enabled. No foreign keys to, and no new columns on, existing tables
  (`runs`, `gene_annotations`, …). Reads ACGC's cache/pipeline only through
  public functions, never by editing their schema.
- No new heavy dependencies in ACGC (no numpy/scipy/pandas): all statistics are
  computed in expression_stats/.
- Dependency on the producer is release-asset-only against the versioned contract
  below (no submodule, no shared package).
- Literature access goes through a `LiteratureGate` protocol so the ACGC
  pipeline implementation can be swapped for LRAS without touching the router.
- Guard tests: (a) an import-boundary test fails if any module outside
  `src/expression_context/` (other than the single registration line in
  `src/main.py`) imports it; (b) with the flag off the app boots, the routes 404,
  and the table is not created.
- `src/expression_context/REMOVAL.md` lists the exact removal steps
  (delete package, delete registration line + import, delete tests,
  `DROP TABLE expression_context_results`, remove env from k8s manifest).

## Statistics (expression_stats/; method_version "2")

Per gene, per TCGA PanCancer study (32; COAD+READ combined as COADREAD):

- Input: `data_mrna_seq_v2_rsem.txt` (batch-normalized RSEM, linear scale).
- Samples: primary tumors only (TCGA sample type `01`; `03` for LAML);
  one sample per patient.
- Transform: `log2(x + 1)`.
- Test: Mann-Whitney U, cancer type vs all other tumors pooled; BH FDR across
  the 32 cancer types for that gene.
- Floor: `F = expression_floor_log2 = log2(11) = 3.4594316186372973`
  (linear RSEM 10). `log2fc = max(median(T), F) - max(median(rest), F)`.
- `log2fc_vs_median_of_type_medians = max(median(T), F) -
  max(median(other type medians), F)`. The floor changes the reported fold
  changes, not the raw medians, ranks, Cliff's delta or AUC.
- Effect sizes also include Cliff's delta and AUC.
- `stats_pass` = q < 0.05 AND log2fc >= 1 AND cliffs_delta >= 0.5 AND
  median(T) >= F (`detection_log2`) AND n >= 10. All comparisons must be defined.
  `detection_log2` is derived from the single `expression_floor_log2` parameter,
  not independently configurable. With both medians clamped and log2fc >= 1,
  the effective minimum median is F+1, corresponding to RSEM >= 21. The explicit
  median(T) >= F detection check is retained for clarity but is implied.
- `p90_log2` and `rest_p90_log2` report the 90th percentile of transformed
  measured samples, using linear interpolation. These fields are informational
  and NEVER gate v2. Median-based v2 can miss subset-amplified genes such as
  ERBB2; upper-tail enrichment is reserved for a later method.
- Missing samples are omitted, not zero-filled. Unavailable comparisons count
  as p=1 internally for BH across all 32 types, but undefined p/q serialize as
  null. A constant measured gene has a defined p=1.
- Flags (informational): `tissue_of_origin_risk`, `small_n`.
- Literature pre-check (stats-passing pairs only): PubMed count for
  `"GENE"[tiab] AND (<tumor_type_query>)`; `literature_precheck_pass` = count >= 1.
  Counts expire after `precheck_cache_ttl_days = 30` days and are invalidated
  when the query changes. Requires only an NCBI API key secret; no LLM secrets.

All thresholds are parameters recorded in the manifest.

## Release bundle contract v1 (expression_stats/ → ACGC)

The producer and reader share this single authoritative contract. Reader implementation
is maintained separately (PR #167). No shared Python imports are permitted.

`manifest.json` release asset:

```json
{
  "schema_version": "1",
  "release_id": "datahub-<12hex>-m2-<build_sha12>",
  "bundle_tag": "expression-stats-datahub-<12hex>-m2-<build_sha12>-pubmed-<YYYYMMDD>",
  "pubmed_snapshot_date": "<YYYY-MM-DD>",
  "gene_count": 20488,
  "sha256": "<sha256 of compressed genes.jsonl.gz>",
  "size_bytes": 12345678,
  "method_version": "2",
  "params": {"fdr": 0.05, "log2fc": 1.0, "cliffs_delta": 0.5,
             "min_n": 10, "expression_floor_log2": 3.4594316186372973,
             "detection_log2": 3.4594316186372973, "precheck_min_pmids": 1,
             "precheck_cache_ttl_days": 30},
  "source": {"datahub_commit": "<sha>", "generated_at": "<iso8601>",
             "hgnc_sha256": "<sha256>", "code_sha256": "<sha256>",
             "overrides_sha256": "<sha256>", "build_sha256": "<sha256>",
             "cohort_mapping_sha256": "<sha256>"},
  "build": {"genes": null, "skip_precheck": false},
  "cancer_types": [
    {"study_id": "brca_tcga_pan_can_atlas_2018", "oncotree_code": "BRCA",
     "name": "Breast Invasive Carcinoma", "tumor_type_query": "breast cancer",
     "import_date": "2026-06-05 15:34:03"}
  ]
}
```

`genes.jsonl.gz`: gzip-compressed JSON Lines, one existing per-gene document
per line, unchanged schema; symbols are upper-case canonical symbols.

```json
{
  "schema_version": "1",
  "release_id": "datahub-<12hex>-m2-<build_sha12>",
  "gene": {"hugo_symbol": "ERBB2", "entrez_gene_id": 2064},
  "cancer_types": [
    {"oncotree_code": "BRCA", "n": 1082, "n_rest": 8989,
     "median_log2": 0.0, "rest_median_log2": 0.0,
     "p90_log2": 0.0, "rest_p90_log2": 0.0,
     "log2fc": 0.0, "log2fc_vs_median_of_type_medians": 0.0,
     "cliffs_delta": 0.0, "auc": 0.0, "p_value": 0.0, "q_value": 0.0,
     "stats_pass": false,
     "pubmed_count": null, "literature_precheck_pass": null,
     "flags": []}
  ]
}
```

`aliases.json` release asset → `{"HER2": "ERBB2", …}` (unambiguous aliases only).

`oncotree_code` contains **TCGA PanCancer cohort codes**, including COADREAD,
GBM, LGG, PCPG and SARC. These preserve the 32 study partitions and are not
necessarily current OncoTree leaf codes. Consumers must use the manifest's
mapping and disease phrase rather than reinterpret codes as leaf identifiers.

The following per-type numeric fields are nullable when n == 0, the relevant
sample group has no measurements, or the statistic is undefined:
`median_log2`, `rest_median_log2`, `log2fc`,
`log2fc_vs_median_of_type_medians`, `cliffs_delta`, `auc`, `p_value`, `q_value`,
`p90_log2`, `rest_p90_log2`. Group-specific medians/percentiles can remain
measured when the opposite group is empty. Undefined p/q are null; BH still
includes that comparison as p=1 internally. `n` and `n_rest` remain integers
and `stats_pass` remains boolean (false for an undefined comparison).
`pubmed_count` and `literature_precheck_pass` are null when the precheck was not
run for that pair, including stats_pass=false and `--skip-precheck`.
ACGC integration must accept these numeric and precheck fields as Optional;
no nullable numeric value should be treated as a measured zero.

**Consumer semantics:** `literature_precheck_pass: null` means the precheck was
not run; a stats-passing pair must still be passed to the consumer's literature
gate. Only an explicit `literature_precheck_pass: false` excludes a pair at the
precheck stage. Thus eligibility is `stats_pass && (literature_precheck_pass is
not false)`. `stats_pass: false` still excludes the pair regardless of precheck.
This file is the **single source of truth** for the producer/consumer contract;
consumer repositories should link here rather than duplicate these semantics.

Release IDs have the exact form `datahub-<12hex>-m2-<build_sha12>`. They are
input identities, not build dates. The suffix hashes method parameters,
override contents, HGNC snapshot hash, code fingerprint, gene subset,
precheck mode, and the cohort mapping fingerprint. The latter hashes the sorted
list of (`study_id`, `oncotree_code`, `name`, `tumor_type_query`), excluding
`import_date`, and is recorded as `source.cohort_mapping_sha256`. Neither the build date, precheck freshness nor fetch timestamps
participate. A forced rebuild due to PubMed TTL expiry with identical inputs
keeps the same release_id, allowing ACGC to reuse its literature verdicts.
`source.generated_at` and `source.precheck_expires_at` record freshness separately.
The code fingerprint hashes package source, override JSONs, pin,
`pyproject.toml`, `constraints.txt`, and the resolved numpy/scipy/pandas versions.
Build and CI dependencies use the committed exact constraints. CI installs
dependencies before computing that fingerprint and deciding early exit.
The HGNC URL points to a dated quarterly archive snapshot, with SHA-256 verified
before use. Early exit checks these fingerprints, parameters and options as
well as datahub commit/method. Before early-exiting a matching release, the
workflow also compares the current API cohort mapping fingerprint.
When prechecks run, `source.precheck_expires_at` records the earliest expiry
of the counts used in that release. Expired counts trigger rebuilding even if
the artifact was generated more recently. Subset and skip-precheck workflow
builds always produce downloadable review artifacts and must never be published as releases. Scheduled/full precheck builds
require NCBI_API_KEY. The PubMed cache is saved with an always-run step, including
after build failure or timeout.

Consumers must ignore unknown fields and reject unknown `schema_version`.


### Release transport and atomicity

- Tag: `expression-stats-<release_id>-pubmed-<YYYYMMDD>`.
- Name: `Expression stats <release_id> (PubMed <YYYY-MM-DD>)`.
- `pubmed_snapshot_date` is the UTC date the completed build began; counts may
  come from the existing 30-day TTL cache. It is a bundle snapshot date, not a
  claim that every PubMed count was fetched on that date. Earliest count expiry
  remains `source.precheck_expires_at`. Readers requiring PubMed freshness must
  check `source.precheck_expires_at`, not the tag date or snapshot date.
- Releases are not prereleases and are created and undrafted with
  `make_latest=false`, preserving the application's Latest release.
- Assets: `manifest.json`, `genes.jsonl.gz`, and existing `aliases.json` with
  its existing name and bytes (and any future existing index JSON unchanged).
  Manifest retains all existing fields and adds `bundle_tag` (the exact tag),
  `pubmed_snapshot_date` (`YYYY-MM-DD`), `gene_count` (number of JSON lines),
  `sha256` (lower-case SHA-256 hex of compressed `genes.jsonl.gz`), and
  `size_bytes` (compressed asset byte length). Consumers verify size/hash before import.
- Publish atomically: create draft, upload all assets, download each and verify
  byte size and SHA-256, then undraft. Upload/verification failures best-effort
  delete only the draft created by that invocation, then re-raise the failure.
  If cleanup fails or the job is interrupted, a matching existing draft fails
  with `stale draft <id>; delete it`; an existing published tag skips successfully.
  Release creation sets `target_commitish` to the building `GITHUB_SHA`.
  Partial gene or skipped-precheck builds are
  review artifacts only and never published. Missing NCBI_API_KEY fails fast
  before dependency installation for any requested precheck build.
- Discovery: paginate the repository's releases, filter non-draft tags beginning
  `expression-stats-`, select newest by `published_at`, then download assets.
  Refreshing PubMed leaves the date-free `release_id` unchanged but yields a new
  dated tag, so readers detect refreshes while reusing literature verdicts.
- Full fresh matching manifests may be reused until PubMed expiry; this checks
  input fingerprints and current cohort mapping. Existing matching tags are
  also skipped immediately before upload if published; stale drafts fail loudly.
  Generated artifacts are not committed.
- The build job has only `contents: read`; checkout never persists credentials.
  A read token is scoped to the release-discovery step, never pip or data builds.
  Bundles cross to a separate publisher via Actions upload/download-artifact.
  Only that job has `contents: write`, and its GH_TOKEN is scoped to publication.
  Publishing requires `github.ref == 'refs/heads/main'`; branch dispatches always
  build review artifacts, even when a matching published build is fresh.
- Workflow concurrency is keyed by event name and ref, so queued manual runs
  cannot replace pending cron runs. Same-event/ref pending runs may coalesce
  under GitHub's concurrency policy. A shared `expression-stats-publish` job
  concurrency group serializes all main-branch publications, preventing tag
  races across schedule and dispatch. Running jobs are never cancelled.
  Since only schedule/main and dispatch/main reach publication, each with one
  active workflow run, at most one publisher waits behind the active publisher.

## ACGC API (when enabled)

- `POST /v1/expression-context/jobs` `{ "gene": "ERBB2" }` → 202
  `{job_id, status_url}`. Runs the literature gate for each pair with
  `stats_pass && (literature_precheck_pass is not false)`. Reuses stored verdicts
  for the same
  (gene, oncotree_code, release_id, literature_gate).
- `GET /v1/expression-context/jobs/{job_id}` → status + result when done.
- `GET /v1/expression-context/genes/{gene}` → latest stored result.
  `surfaced` pairs by default; `?include_excluded=true` adds excluded pairs with
  a reason (`stats`, `precheck`, `literature`).
- `GET /v1/expression-context/genes?limit&cursor` → history of genes run.
- All behind `require_auth`.

Literature verdict (v1, `literature_gate = "acgc_pipeline_v1"`): run the
existing pipeline with `tumor_type = tumor_type_query`; supported iff
`insufficient_evidence` is false AND ≥1 cited PMID. Stored with PMIDs and
rationale.

## Table `expression_context_results`

`hugo_symbol, oncotree_code, release_id, literature_gate, surfaced (bool),
exclusion_reason, stats_json, evidence_json, computed_at, requested_by`;
unique `(hugo_symbol, oncotree_code, release_id, literature_gate)`.

## Later

- Swap `LiteratureGate` to LRAS when `/lras/v1/retrieve` ships.
- Normal-tissue comparison (TCGA adjacent / GTEx) as a separate track.
