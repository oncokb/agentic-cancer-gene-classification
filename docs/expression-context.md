# Expression context (optional, removable module)

Status: design v1 — 2026-11-05 (supports artifact schema_version "1", method_version "2"; contract owned by the producer repo)

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
 │  2. stats gate for every gene (one-vs-rest, producer doc)  │
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
- Dependency on the stats repo is HTTP-only against the producer's versioned
  contract (see "Artifact contract"; no submodule, no shared package).
- Literature access goes through a `LiteratureGate` protocol so the ACGC
  pipeline implementation can be swapped for LRAS without touching the router.
- Guard tests: (a) an import-boundary test fails if any module outside
  `src/expression_context/` (other than the single registration line in
  `src/main.py`) imports it; (b) with the flag off the app boots, the routes 404,
  and the table is not created.
- `src/expression_context/REMOVAL.md` lists the exact removal steps
  (delete package, delete registration line + import, delete tests,
  `DROP TABLE expression_context_results`, remove env from k8s manifest).

## Artifact contract (owned by the producer)

The statistics method and the artifact contract are defined in **one place**,
the producer repo:
[`oncokb/gene-expression-relevance` → `docs/acgc-contract.md`](https://github.com/oncokb/gene-expression-relevance/blob/main/docs/acgc-contract.md).
This document does not restate them; if anything here disagrees with that file,
that file wins and this module should be updated.

Supported by this module:

| | Value |
|---|---|
| `schema_version` | `"1"` (any other value is rejected) |
| `method_version` | `"2"` |

Endpoints read (under `EXPRESSION_CONTEXT_ARTIFACT_BASE_URL`):
`v1/manifest.json`, `v1/genes/{HUGO_SYMBOL}.json` (404 = gene absent) and
`v1/aliases.json`.

`release_id` has the form `datahub-<12hex>-m<method>-<build_sha12>` and changes
only when the producer's inputs change (datahub commit, method, build
fingerprint); it does not change on a PubMed-count refresh. ACGC treats it as an
opaque string: it is a cache and verdict-reuse key, never parsed.

## Consumer semantics

What ACGC relies on from the contract:

- **Manifest:** `schema_version`, `release_id`, and per cancer type
  `oncotree_code` (a TCGA PanCancer cohort code, e.g. `COADREAD`, `GBM`, `LGG`,
  `PCPG`, `SARC`), `study_id`, `name`, `tumor_type_query` (passed to the
  literature gate as the tumor type). `method_version`, `params` (e.g.
  `expression_floor_log2`, `detection_log2`) and `source` are informational.
- **Gene file:** `schema_version`, `release_id` (must equal the manifest's),
  `gene.hugo_symbol`, and per cancer type `oncotree_code`, `n`, `n_rest`,
  `stats_pass`, `literature_precheck_pass`, `flags`. The statistics
  (`median_log2`, `rest_median_log2`, `p90_log2`, `rest_p90_log2`, `log2fc`,
  `log2fc_vs_median_of_type_medians`, `cliffs_delta`, `auc`, `p_value`,
  `q_value`) and `pubmed_count` are displayed and stored, not re-gated.
- **Aliases:** `aliases.json` maps an alias to its canonical symbol; used only
  when the canonical gene file is absent.
- **Nulls are tolerated:** every statistic and `pubmed_count` may be `null`
  (undefined, e.g. `n == 0`); nulls are stored and returned as `null`, never as
  zero, and sort last.
- **Gating:** a pair goes to the literature gate iff `stats_pass` is true AND
  `literature_precheck_pass` is not `false`. `null` (pre-check not run) is sent
  to the literature gate, which is authoritative; only an explicit `false`
  excludes, with reason `precheck`. `stats_pass` false excludes with reason
  `stats`.
- **Unknown fields are ignored; an unknown `schema_version` is rejected**
  (HTTP 502 from the ACGC endpoints).
- **Verdict reuse:** literature verdicts are stored and reused per
  `(hugo_symbol, oncotree_code, release_id, literature_gate)`, so a new
  `release_id` re-evaluates and a PubMed-count refresh alone does not.

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
