# Expression context (optional, removable module)

Status: artifact v1 (additive fields), statistical method v3 — 2026-11-07

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

## Statistics (expression_stats/; method_version "3")

Use the recommended branch-aware hybrid from the follow-up evaluation:
`(v2_lineage_pass AND NOT broadly_expressed) OR subgroup_pass`. This is an
empirical screening rule, not tumor-versus-normal inference. The curated panel
was inspected during method development, not a blinded external validation.

Inputs and sample selection are unchanged: batch-normalized linear RSEM from
32 pinned TCGA PanCancer Atlas studies (COAD+READ combined as COADREAD), primary
samples (`01`, or `03` for LAML), deterministic one-per-patient selection and
Entrez aggregation. Transform finite measurements as `x = log2(RSEM + 1)`;
NaNs are omitted and measured zeros remain measurements. Let
`F = expression_floor_log2 = log2(11) = 3.4594316186372973` (RSEM 10).
Percentiles use NumPy's linear interpolation in log space, then clamp to F.

### Broad baseline and lineage arm

`broadly_expressed` is a non-null boolean on the per-gene document. Set it true
only if every one of the 32 types has at least 10 measured samples, at least
29 types have >=90% of their measured samples at RSEM >=10 (`x >= F`), and the
population CV (`ddof=0`) of `2**median(x_type) - 1` across the 32 types is <=0.5.
Type medians have equal weights, irrespective of cohort size. An undefined CV
or insufficient measurements makes the flag false. It describes a baseline,
not a curated housekeeping-gene list, and may also mark true amplified genes.

The lineage arm retains the m2 test unchanged: two-sided asymptotic
Mann–Whitney U with tie/continuity corrections and per-gene BH across all 32
types; q<0.05, clamped median log2FC>=1, Cliff's delta>=0.5,
median(type)>=F, n>=10. A broadly expressed gene cannot pass this arm.
`log2fc=max(median(type),F)-max(median(rest),F)`;
`log2fc_vs_median_of_type_medians` uses the median of the other type medians
and clamps both operands to F. Raw medians, ranks, AUC and delta are unchanged.
`detection_log2` remains derived from the same floor; the FC gate implies an
effective type median >=F+1. Existing p/q fields continue describing this
lineage test even when a subgroup call passes it by a different test.

### Subgroup arm

For each gene, form the label-invariant threshold
`T=max(Q99(x_all_measured_TCGA), F)`. In each type and its rest, count finite
samples strictly greater than T as k and k_rest. Require **all**:

- n>=10 and n_rest>=10;
- k>=10 and k/n>=0.03;
- `(k/n)/(max(k_rest,1)/n_rest)>=3` (a conservative one-case rest floor);
- `max(Q95(x_type),F)-max(Q95(x_rest),F)>=1.5` log2 units (~2.83-fold);
- one-sided Fisher p=`hypergeom.sf(k-1,n+n_rest,k+k_rest,n)`, with BH across
  all 32 types for this gene, q<0.025.

Broadly expressed genes **may** pass the subgroup arm. All thresholds are
recorded in manifest params. The two arms have separate testing families;
these are per-gene screening q-values, not a claimed genome-wide FDR or a
joint 0.05 FDR for the OR rule.

### Wire fields and nullability

All previous fields remain. `stats_pass` equals `pass_mode != null`:

| New field | Type / meaning |
| --- | --- |
| document `broadly_expressed` | bool, never null; definition above |
| `pass_mode` | `"lineage"`, `"subgroup"`, or null; lineage wins if both arms pass |
| `lineage_blocked_by_broad` | bool, never null; original m2 lineage rule passed and gene is broad, even if subgroup rescues it |
| `subgroup_high_count` | int or null; k, null if type has no measured samples |
| `subgroup_high_fraction` | float or null; k/n, null if n=0 |
| `subgroup_high_fraction_ratio` | float or null; rest-floor ratio above, null if n=0 or n_rest=0 |
| `p95_log2_diff` | float or null; clamped P95 difference, null if either group has no measurements |
| `subgroup_q` | float or null; subgroup BH q, null if either group has no measurements |

A small but measured group may have defined diagnostics/q while still failing
min_n. Undefined tests enter their respective 32-type BH family as p=1,
then serialize as null. A constant measured gene has defined lineage p=1
and subgroup q=1. Group-specific measured summaries remain defined when the
opposite group is empty. Existing nullable fields retain their semantics.
`p90_log2`/`rest_p90_log2` remain informational. `tissue_of_origin_risk` and
`small_n` remain informational flags. Do not interpret a null as measured zero.

### Curated controls and PubMed

`expression_stats/controls.yaml` is JSON-compatible YAML, read with stdlib
JSON, with original source SHA-256, PMIDs, class and expected outcomes. It
contains 25 lineage positives, 10 subgroup positives, 24 negatives and three
regression negatives. ERBB2/ESCA is reported but excluded from scoring.
The panel is included in the code fingerprint so changing it changes identity.
Control IDs and (gene, tcga) pairs must both be unique; evaluation and publication
reject duplicates. Publisher positive/negative totals are derived from this panel,
not hard-coded, while requiring all positives pass, zero negative passes and
zero missing scored pairs.

Every build writes `v1/controls-report.json`. Full builds must pass all 35
positives and pass none of the 27 negatives; missing scored genes/types fail
closed, including missing negatives. The gate runs before PubMed. Failure
raises, preserves the report and manifest under `<out>-failed/`, retains any
previous complete output, and cannot reach publication. The workflow uploads
failed control artifacts for review. Subset builds report `skipped_subset`,
do not enforce the gate, and remain unpublishable. The manifest's `controls`
summary records status, panel SHA-256, counts, missing count and failures.
Publication independently requires the complete passing report for the current
panel, 35/35 positives and 0/27 negatives, in addition to full precheck coverage.
`controls-report.json` is a separate release asset with its existing bytes.

PubMed runs only for final stats-passing pairs, using
`"GENE"[tiab] AND (<tumor_type_query>)`. Precheck passes for count>=1. Cached
counts invalidate when queries change. Expiry is deterministically spread by
SHA-256 of (gene, type): `fetched_at + max_ttl * (1 - 0.5 * hash_fraction)`,
giving 15–30 days for the 30-day maximum TTL. Existing fetched timestamps are
reused; no count ever lives longer than the maximum. The default
rate is **3 requests/s even with an API key**, reserving capacity for ACGC.
Configure `EXPRESSION_STATS_PUBMED_REQUESTS_PER_SECOND` (or the Python client
`requests_per_second` argument); it must be finite and >0, capped at 3 without
a key or 10 with a key. Retries share the same limiter. The effective rate is recorded as the unhashed
`source.precheck_requests_per_second`, never in method `params`.
`runtime-settings.json` stores operational defaults outside the source fingerprint;
changing its rate or the environment override leaves `release_id` unchanged.
Workflow prechecks still require NCBI_API_KEY.

The precheck has a default 300-minute phase budget, configurable with
`EXPRESSION_STATS_PRECHECK_TIME_BUDGET_SECONDS` or `runtime-settings.json`.
It is also capped at 315 minutes from builder start, leaving at least 15 minutes
before the workflow's 330-minute step timeout for thread shutdown and cache
saving. `source.precheck_time_budget_seconds` and `source.build_time_budget_seconds`
record these unhashed settings. Socket timeouts, retries, rate-limiter waits and
`Retry-After` sleeps respect the remaining budget. Pending tasks are cancelled;
in-flight workers stop at the deadline. Successful counts commit individually.
Budget exhaustion raises an error, preserves prior complete output and writes
`<out>-incomplete/{manifest.json,controls-report.json,precheck-report.json}`.
Its manifest marks `source.precheck_incomplete=true` and has no completed
`precheck_expires_at`; publication refuses it. The always-run PubMed cache save
still runs, and the workflow uploads the incomplete report for review. Re-run
the workflow to reuse committed counts; statistics are rebuilt, but successful
queries are not repeated while fresh. No partial gene bundle is published.

## Release bundle contract v1 (expression_stats/ → ACGC)

The producer and reader share this single authoritative contract. Reader implementation
is maintained separately (PR #167). No shared Python imports are permitted.

`manifest.json` release asset:

```json
{
  "schema_version": "1",
  "release_id": "datahub-<12hex>-m3-<build_sha12>",
  "bundle_tag": "expression-stats-datahub-<12hex>-m3-<build_sha12>-pubmed-<YYYYMMDD>",
  "pubmed_snapshot_date": "<YYYY-MM-DD>",
  "gene_count": 20488,
  "sha256": "<sha256 of compressed genes.jsonl.gz>",
  "size_bytes": 12345678,
  "method_version": "3",
  "params": {
    "fdr": 0.05,
    "log2fc": 1.0,
    "cliffs_delta": 0.5,
    "min_n": 10,
    "expression_floor_log2": 3.4594316186372973,
    "broad_sample_fraction": 0.9,
    "broad_min_types": 29,
    "broad_type_median_cv": 0.5,
    "broad_required_types": 32,
    "subgroup_percentile": 99,
    "subgroup_min_count": 10,
    "subgroup_min_fraction": 0.03,
    "subgroup_min_fraction_ratio": 3.0,
    "subgroup_p95_percentile": 95,
    "subgroup_p95_log2_diff": 1.5,
    "subgroup_fdr": 0.025,
    "precheck_min_pmids": 1,
    "precheck_cache_ttl_days": 30,
    "detection_log2": 3.4594316186372973
  },
  "source": {
    "datahub_commit": "<sha>",
    "generated_at": "<iso8601>",
    "precheck_requests_per_second": 3.0,
    "precheck_time_budget_seconds": 18000,
    "build_time_budget_seconds": 18900,
    "hgnc_sha256": "<sha256>",
    "code_sha256": "<sha256>",
    "overrides_sha256": "<sha256>",
    "build_sha256": "<sha256>",
    "cohort_mapping_sha256": "<sha256>"
  },
  "build": {
    "genes": null,
    "skip_precheck": false
  },
  "cancer_types": [
    {
      "study_id": "brca_tcga_pan_can_atlas_2018",
      "oncotree_code": "BRCA",
      "name": "Breast Invasive Carcinoma",
      "tumor_type_query": "breast cancer",
      "import_date": "2026-06-05 15:34:03"
    }
  ],
  "controls": {
    "status": "passed",
    "controls_sha256": "<sha256 of controls.yaml>",
    "positive_passed": 35,
    "positive_total": 35,
    "negative_passed": 0,
    "negative_total": 27,
    "missing": 0,
    "failures": []
  }
}
```

`genes.jsonl.gz`: gzip-compressed JSON Lines, one existing per-gene document
per line, unchanged schema; symbols are upper-case canonical symbols.

```json
{
  "schema_version": "1",
  "release_id": "datahub-<12hex>-m3-<build_sha12>",
  "gene": {"hugo_symbol": "ERBB2", "entrez_gene_id": 2064},
  "broadly_expressed": false,
  "cancer_types": [
    {"oncotree_code": "BRCA", "n": 1082, "n_rest": 8989,
     "median_log2": 0.0, "rest_median_log2": 0.0,
     "p90_log2": 0.0, "rest_p90_log2": 0.0,
     "log2fc": 0.0, "log2fc_vs_median_of_type_medians": 0.0,
     "cliffs_delta": 0.0, "auc": 0.0, "p_value": 0.0, "q_value": 0.0,
     "stats_pass": false, "pass_mode": null, "lineage_blocked_by_broad": false,
     "subgroup_high_count": 0, "subgroup_high_fraction": 0.0,
     "subgroup_high_fraction_ratio": 0.0, "p95_log2_diff": 0.0, "subgroup_q": 1.0,
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

Release IDs have the exact form `datahub-<12hex>-m3-<build_sha12>`. They are
input identities, not build dates. The suffix hashes method parameters,
override contents, HGNC snapshot hash, code fingerprint (including the curated controls panel), gene subset,
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
Operational `runtime-settings.json` is excluded from this fingerprint.
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
- Assets: `manifest.json`, `genes.jsonl.gz`, `controls-report.json`, and existing `aliases.json` with
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
  under GitHub's concurrency policy. Builds with prechecks share the
  `expression-stats-pubmed` concurrency group with `cancel-in-progress: false`,
  so cron and manual builds (including branch dispatches) cannot simultaneously
  use the shared NCBI_API_KEY. This serializes the entire build, including cache
  restore/save for prechecked builds. Skipped prechecks instead use a unique
  per-run group, cannot displace a pending cron precheck, and never restore/save
  the PubMed cache (so they cannot shadow newer resumable counts). GitHub retains at most one pending job
  per shared group; a newer pending job can replace an older one, while running
  jobs are never cancelled. The event/ref workflow groups remain separate so
  manual runs cannot replace pending cron runs at the workflow queue level.
  A shared `expression-stats-publish` job
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
