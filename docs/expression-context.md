# Expression context (optional, removable module)

Status: design v1 — 2026-11-05 (stats method_version "2"; artifact schema_version "1")

## Goal

Given a gene, surface only the (gene, cancer type) pairs where:

1. TCGA PanCancer Atlas RNA-seq expression is **statistically significantly
   elevated in that cancer type relative to the other TCGA tumor types**
   (tumor-only data: this is *tumor-type enrichment*, NOT tumor-vs-normal), and
2. the literature characterizes the gene in that tumor type.

Each surfaced pair shows the statistics and the supporting citations side by side.

## Architecture (hybrid)

```
 ┌────────────── stats repo (separate, static) ──────────────┐
 │ GitHub Action (cron weekly + manual)                       │
 │  1. download 32 *_tcga_pan_can_atlas_2018                  │
 │     data_mrna_seq_v2_rsem.txt from cBioPortal datahub      │
 │     (pinned commit)                                        │
 │  2. stats gate for every gene (one-vs-rest, see below)     │
 │  3. cheap PubMed-count pre-check for stats-passing pairs   │
 │  4. publish versioned JSON to GitHub Pages                 │
 └───────────────────────┬────────────────────────────────────┘
                         │  HTTPS GET, versioned JSON contract
                         ▼
 ┌────────────── ACGC: src/expression_context/ ──────────────┐
 │ opt-in router (flag EXPRESSION_CONTEXT_ENABLED)            │
 │  - reads precomputed hits for the gene                     │
 │  - literature gate on demand via LiteratureGate adapter    │
 │    (v1: ACGC pipeline; later: LRAS /lras/v1/retrieve)      │
 │  - stores joined verdicts in its own MySQL table           │
 └────────────────────────────────────────────────────────────┘
```

No new instances: the stats side is GitHub Actions + Pages; the ACGC side is a
route on the existing deployment, off by default.

## Removability requirements (hard)

ACGC must be able to drop this feature by deleting one package and two lines.

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
  computed in the stats repo.
- Dependency on the stats repo is HTTP-only against the versioned contract
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

## Statistics (stats repo; method_version "2")

Per gene, per TCGA PanCancer cohort (32; COAD+READ combined as COADREAD).
`oncotree_code` values are these TCGA PanCancer **cohort codes** (e.g. `BRCA`,
`COADREAD`, `GBM`, `LGG`, `PCPG`, `SARC`), not necessarily OncoTree leaf codes.

- Input: `data_mrna_seq_v2_rsem.txt` (batch-normalized RSEM, linear scale).
- Samples: primary tumors only (TCGA sample type `01`; `03` for LAML);
  one sample per patient.
- Transform: `log2(x + 1)`.
- Expression floor: `F = log2(11)` (RSEM 10), so near-zero expression can't
  produce large fold changes.
- Test: Mann-Whitney U, cancer type vs all other tumors pooled; BH FDR across
  the 32 cancer types for that gene.
- Effect sizes (floor-clamped):
  `log2fc = max(median(T), F) - max(median(rest), F)`; the same floor applies to
  `log2fc_vs_median_of_type_medians` (guards against large cohorts dominating
  the pooled rest). Also Cliff's delta, AUC, and informational 90th
  percentiles `p90_log2` / `rest_p90_log2`.
- `stats_pass` = q < 0.05 AND log2fc >= 1 AND cliffs_delta >= 0.5 AND
  median(T) >= F (detection) AND n >= 10.
- Flags (informational): `tissue_of_origin_risk`, `small_n`.
- Literature pre-check (stats-passing pairs only): PubMed count for
  `"GENE"[tiab] AND (<tumor query>)`; `literature_precheck_pass` = count >= 1.
  Requires only an NCBI API key secret; no LLM secrets in CI.

All thresholds are parameters recorded in the manifest.

## Artifact contract v1 (stats repo → ACGC)

Base URL configured in ACGC as `EXPRESSION_CONTEXT_ARTIFACT_BASE_URL`.

`GET {base}/v1/manifest.json`

```json
{
  "schema_version": "1",
  "release_id": "2026-11-05-datahub-<shortsha>-m2-<buildhash>",
  "method_version": "2",
  "params": {"fdr": 0.05, "log2fc": 1.0, "cliffs_delta": 0.5,
             "min_n": 10, "floor_log2": 3.4594316186372973,
             "detection_log2": 3.4594316186372973, "precheck_min_pmids": 1},
  "source": {"datahub_commit": "<sha>", "generated_at": "<iso8601>"},
  "cancer_types": [
    {"study_id": "brca_tcga_pan_can_atlas_2018", "oncotree_code": "BRCA",
     "name": "Breast Invasive Carcinoma", "tumor_type_query": "breast cancer",
     "import_date": "2026-06-05 15:34:03"}
  ]
}
```

`GET {base}/v1/genes/{HUGO_SYMBOL}.json` (upper-case canonical symbol; 404 if
gene absent)

```json
{
  "schema_version": "1",
  "release_id": "…",
  "gene": {"hugo_symbol": "ERBB2", "entrez_gene_id": 2064},
  "cancer_types": [
    {"oncotree_code": "BRCA", "n": 1082, "n_rest": 8989,
     "median_log2": 0.0, "rest_median_log2": 0.0,
     "p90_log2": 0.0, "rest_p90_log2": 0.0,
     "log2fc": 0.0, "log2fc_vs_median_of_type_medians": 0.0,
     "cliffs_delta": 0.0, "auc": 0.0, "p_value": 0.0, "q_value": 0.0,
     "stats_pass": true,
     "pubmed_count": 1234, "literature_precheck_pass": true,
     "flags": []}
  ]
}
```

`release_id` identifies the datahub commit, the method version and a build
hash of the producer (so a code-only rebuild is a new release).

Nullable per-type fields (consumers must accept `null`):

| Field(s) | `null` when |
|---|---|
| `median_log2`, `rest_median_log2`, `p90_log2`, `rest_p90_log2`, `log2fc`, `log2fc_vs_median_of_type_medians`, `cliffs_delta`, `auc`, `p_value`, `q_value` | statistic undefined (e.g. `n == 0`, or the test can't be run) |
| `pubmed_count`, `literature_precheck_pass` | pre-check not run (`stats_pass` false, or a skip mode) |

`oncotree_code`, `n`, `n_rest`, `stats_pass` and `flags` are always present.
A pair goes to ACGC's literature gate iff `stats_pass` is true AND
`literature_precheck_pass` is not `false` (`null` = pre-check not run; the
literature gate itself is authoritative). It is excluded with reason
`precheck` only when `literature_precheck_pass` is explicitly `false`.

`GET {base}/v1/aliases.json` → `{"HER2": "ERBB2", …}` (unambiguous aliases only).

Consumers must ignore unknown fields and reject unknown `schema_version`.

## ACGC API (when enabled)

- `POST /v1/expression-context/jobs` `{ "gene": "ERBB2" }` → 202
  `{job_id, status_url}`. Runs the literature gate for each pair with
  `stats_pass` true and `literature_precheck_pass` not `false` (`null` passes). Reuses stored verdicts for the same
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
- Optional live browser recompute on the static site.
