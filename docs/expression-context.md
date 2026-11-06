# Expression context (optional, removable module)

Status: design v2 — 2026-11-06. Statistics are produced in this repository
(`expression_stats/`) and delivered as GitHub release bundles. Supports bundle
`schema_version` "1", `method_version` "2".

## Goal

Given a gene, surface only the (gene, cancer type) pairs where:

1. TCGA PanCancer Atlas RNA-seq expression is **statistically significantly
   elevated in that cancer type relative to the other TCGA tumor types**
   (tumor-only data: this is *tumor-type enrichment*, NOT tumor-vs-normal), and
2. the literature characterizes the gene in that tumor type.

Each surfaced pair shows the statistics and the supporting citations side by
side, in the API and on the "Expression stats" page.

## Architecture

```
 ┌──────────── expression_stats/ (producer, this repo) ────────────┐
 │ GitHub Action (cron weekly + manual)                             │
 │  1. download the 32 *_tcga_pan_can_atlas_2018 RSEM files from    │
 │     cBioPortal datahub (pinned commit)                           │
 │  2. stats gate for every gene (one-vs-rest)                      │
 │  3. PubMed-count pre-check for stats-passing pairs               │
 │  4. publish a GitHub *release bundle* on this repo:              │
 │     tag expression-stats-<release_id>-pubmed-<YYYYMMDD>          │
 │     assets manifest.json, genes.jsonl.gz, aliases.json           │
 └──────────────────────────────┬───────────────────────────────────┘
                                │ GitHub Releases API (discovery) +
                                │ asset download, verified sha256/size
                                ▼
 ┌──────────── ACGC: src/expression_context/ (consumer) ────────────┐
 │ off unless EXPRESSION_CONTEXT_ENABLED                             │
 │  - bundle refresher: newest bundle → module MySQL tables,         │
 │    atomic switch of the active bundle                             │
 │  - API + "Expression stats" page read the active bundle           │
 │  - literature gate on demand via LiteratureGate adapter           │
 │    (v1: ACGC pipeline; later: LRAS /lras/v1/retrieve)             │
 │  - stores joined verdicts in its own MySQL table                  │
 └───────────────────────────────────────────────────────────────────┘
```

No new instances and no Pages site: the producer is a GitHub Action whose
output is release assets on this (public) repository; the consumer is a module
on the existing ArgoCD-deployed ACGC, off by default.

## Removability requirements (hard)

ACGC must be able to drop this feature by deleting one package and two lines.

- All code lives in `src/expression_context/` (router, page + its static
  files, GitHub release client, bundle loader/refresher, stores, service,
  literature-gate adapter, settings, models). Nothing else in `src/` imports
  from it except ONE registration call in `src/main.py`; the core UI
  (`src/static/`) has no reference to it.
- Own settings class with env prefix `EXPRESSION_CONTEXT_` — do **not** add
  fields to `src/config.py`.
- `EXPRESSION_CONTEXT_ENABLED` defaults to `false`. When false: no routes, no
  page and no nav entry, no tables created, no outbound calls (GitHub
  included), no extra startup work.
- Own tables, created by the module only when enabled (see "Tables"). No
  foreign keys to, and no new columns on, existing tables (`runs`,
  `gene_annotations`, …). Reads ACGC's cache/pipeline only through public
  functions, never by editing their schema.
- No new heavy dependencies in ACGC (no numpy/scipy/pandas): all statistics are
  computed in `expression_stats/`.
- Dependency on the producer is release-asset-only against the versioned
  bundle contract (no shared Python imports in either direction).
- Literature access goes through a `LiteratureGate` protocol so the ACGC
  pipeline implementation can be swapped for LRAS without touching the router.
- Guard tests: (a) an import-boundary test fails if any module outside
  `src/expression_context/` (other than the single registration line in
  `src/main.py`) imports it, or if the module imports a private ACGC name;
  (b) with the flag off the app boots, the API and page 404, the main page has
  no nav entry, no table is created and nothing calls out.
- `src/expression_context/REMOVAL.md` lists the exact removal steps.

## Release bundle transport

The bundle format is defined in **one place**:
[`expression_stats/docs/contract.md`](../expression_stats/docs/contract.md)
("Release bundle contract v1" and "Release transport and atomicity"). This
document does not restate it; if anything here disagrees with that file, that
file wins and this module should be updated.

Supported by this module:

| | Value |
|---|---|
| `schema_version` | `"1"` (any other value is rejected) |
| `method_version` | `"2"` |

How ACGC consumes it:

1. **Discover.** List the releases of `EXPRESSION_CONTEXT_RELEASES_REPO`
   (default `oncokb/agentic-cancer-gene-classification`) and keep non-draft,
   non-prerelease releases whose tag starts with `expression-stats-`. GitHub
   orders the list by creation, so **every page** (100 releases each, at most
   `EXPRESSION_CONTEXT_MAX_RELEASE_PAGES`) is read and the newest by
   `published_at` wins. If the list continues past the page cap, discovery is
   *incomplete*: nothing is activated, the active bundle keeps serving, and the
   refresh reports `outcome:discovery_incomplete`.
   `EXPRESSION_CONTEXT_PINNED_BUNDLE_TAG` instead loads exactly that tag and
   never advances (rollback/pinning).
2. **Download and verify.** All three assets are required. `manifest.json`
   (schema_version, bundle fields, `bundle_tag` = release tag =
   `expression-stats-<release_id>-pubmed-<YYYYMMDD of pubmed_snapshot_date>`)
   and `aliases.json` (a JSON object of string alias → string symbol; any other
   shape rejects the bundle) are validated first. Then `genes.jsonl.gz` is
   streamed once: SHA-256 and byte count over the compressed bytes, inflated
   incrementally (bounded memory), every line validated as a gene document of
   the same `release_id`, inserted in batches. The bundle becomes `ready` only
   if `size_bytes`, `sha256` and `gene_count` all match and the aliases were
   imported; otherwise it is marked failed, its rows are deleted and the
   active bundle keeps serving.
3. **Switch.** One transaction swaps the single-row pointer, fenced on still
   holding the loader lease. The previous bundle is kept. A bundle that leaves
   {active, previous} gets `retired_at` from the **database clock at the
   swap** (a re-activated bundle has it cleared), and pruning deletes it only
   once it has been retired for `EXPRESSION_CONTEXT_BUNDLE_PRUNE_GRACE`
   (default 1 h, at least the pointer cache time + 5 minutes), again on the
   database clock. Pruning reads the *current* pointers inside its own fenced
   transaction, never a snapshot, so it can't delete a bundle that just became
   active again. Pods with a cached pointer and in-flight requests keep
   finding retired bundles during the grace period. A reader whose bundle has vanished anyway re-reads
   the active bundle and retries; it never answers 404 because of pruning.
4. **Serve.** Requests read the active bundle from MySQL (no outbound call on
   the request path). No bundle yet → HTTP 503 with `Retry-After`. A failed
   refresh logs a warning, emits `expression_context.bundle.refresh` with
   `outcome:<…>`, records the error, and keeps serving the last good bundle.

Coordination and rate limits:

- The refresh check is due every `EXPRESSION_CONTEXT_REFRESH_INTERVAL`
  (default 6 h; seconds or ISO 8601 such as `PT6H`; minimum 5 minutes). The
  "last checked" time is shared in MySQL, so the whole cluster makes about one
  discovery per interval however many pods run (each pod only wakes every
  `min(REFRESH_ERROR_RETRY, REFRESH_INTERVAL / 12)` to ask whether one is due).
- **One authoritative loader lock**: a lease row in
  `expression_context_state` (owner token + expiry), taken and renewed by
  compare-and-set on the *database* clock and held for the whole load. Every
  loader must hold it; Redis plays no part in loading, so a pod whose Redis is
  unreachable can't start a second import. **Every bundle write is fenced:**
  row inserts, the per-tag reset, failure cleanup, status updates, activation
  and pruning each carry the lease token and run in one transaction that
  first locks the state row (`SELECT … FOR UPDATE`) and checks the lease on
  the database clock. Acquiring or renewing the lease updates that same row,
  so ownership can't change between the check and the write; a stale loader's
  writes are refused (`LoaderLockNotHeld`) and touch nothing. A failed or
  erroring renewal also aborts the load early. A crashed holder's lease simply
  expires (`EXPRESSION_CONTEXT_BUNDLE_LOCK_TTL_SECONDS`, 120 s).
- Only discovery uses the GitHub API (60 requests/h per IP unauthenticated;
  `EXPRESSION_CONTEXT_GITHUB_TOKEN` is optional and only raises that limit).
  Asset downloads go to `github.com` release URLs, which are not API-metered,
  and never carry the token. A 403/429 is not retried: the cluster waits until
  GitHub's `Retry-After`/`X-RateLimit-Reset`. Other GitHub errors retry after
  `EXPRESSION_CONTEXT_REFRESH_ERROR_RETRY` (30 minutes); a bundle that violates
  the contract is not downloaded again before a full interval.
- **Budget enforced at startup:** the worst case `3600 / min(REFRESH_INTERVAL,
  REFRESH_ERROR_RETRY) × MAX_RELEASE_PAGES × HTTP_RETRY_ATTEMPTS` must be at
  most 30 requests/hour without a token (1000/hour with one); otherwise the
  settings are rejected with an error naming the numbers. The defaults are
  exactly 30/hour (2 × 5 × 3). The refresher never checks earlier than those
  cadences (no early-check slack), so the bound is real.

`release_id` (`datahub-<12hex>-m<method>-<build_sha12>`) changes only when the
producer's inputs change; a PubMed-count refresh keeps it and publishes a new
`bundle_tag`. ACGC treats both as opaque strings.

## Consumer semantics

What ACGC relies on from the contract:

- **Manifest:** `schema_version`, `release_id`, the bundle fields above, and
  per cancer type `oncotree_code` (a TCGA PanCancer cohort code, e.g.
  `COADREAD`, `GBM`, `LGG`, `PCPG`, `SARC`), `study_id`, `name`,
  `tumor_type_query` (passed to the literature gate as the tumor type).
  `method_version`, `params` (e.g. `expression_floor_log2`, `detection_log2`)
  and `source` are informational and shown as provenance.
- **Gene documents** (lines of `genes.jsonl.gz`): `schema_version`,
  `release_id` (must equal the manifest's), `gene.hugo_symbol`, and per cancer
  type `oncotree_code`, `n`, `n_rest`, `stats_pass`, `literature_precheck_pass`,
  `flags`. The statistics (`median_log2`, `rest_median_log2`, `p90_log2`,
  `rest_p90_log2`, `log2fc`, `log2fc_vs_median_of_type_medians`,
  `cliffs_delta`, `auc`, `p_value`, `q_value`) and `pubmed_count` are displayed
  and stored, not re-gated.
- **Aliases:** `aliases.json` maps an alias to its canonical symbol; used only
  when the input is not itself a canonical symbol in the bundle.
- **Nulls are tolerated:** every statistic and `pubmed_count` may be `null`
  (undefined, e.g. `n == 0`); nulls are stored and returned as `null`, never as
  zero, and sort last.
- **Gating:** a pair goes to the literature gate iff `stats_pass` is true AND
  `literature_precheck_pass` is not `false`. `null` (pre-check not run) is sent
  to the literature gate, which is authoritative; only an explicit `false`
  excludes, with reason `precheck`. `stats_pass` false excludes with reason
  `stats`. Pre-check values come from the active bundle, so a PubMed refresh
  that flips a pair to pass takes effect on the next job.
- **Expired pre-checks:** if the manifest's `source.precheck_expires_at` is in
  the past when a job runs or the page is viewed, every
  `literature_precheck_pass` in the bundle is treated as unavailable (like
  `null`): it never excludes a pair, the literature gate decides, and the page
  shows "Pre-check expired". The raw value is still stored and displayed.
- **Unknown fields are ignored; an unknown `schema_version` is rejected** (the
  bundle is not loaded; the previous one keeps serving).
- **Verdict reuse:** literature verdicts are stored and reused per
  `(hugo_symbol, oncotree_code, release_id, literature_gate)` — keyed on
  `release_id`, not `bundle_tag` — so a new `release_id` re-evaluates and a
  PubMed refresh alone does not.

## ACGC API and page (when enabled)

- `POST /v1/expression-context/jobs` `{ "gene": "ERBB2" }` → 202
  `{job_id, status_url}`. Runs the literature gate for each pair with
  `stats_pass` true and `literature_precheck_pass` not `false` (`null` passes).
  Reuses stored verdicts for the same (gene, oncotree_code, release_id,
  literature_gate).
- `GET /v1/expression-context/jobs/{job_id}` → status + result when done.
- `GET /v1/expression-context/stats/{gene}` → the active bundle's statistics for
  every cancer type of the gene, each with its call (`surfaced`, `excluded`
  with a reason, or `literature_pending`) and any stored literature verdict,
  plus the bundle provenance.
- `GET /v1/expression-context/bundle` → active bundle provenance and the
  cluster's refresh state (last check, back-off, last error, pinned tag).
- `GET /v1/expression-context/genes/{gene}` → latest stored result.
  `surfaced` pairs by default; `?include_excluded=true` adds excluded pairs with
  a reason (`stats`, `precheck`, `literature`).
- `GET /v1/expression-context/genes?limit&cursor` → history of genes run.
- `GET /expression-context` → the "Expression stats" page (signed-out browsers
  go through `/login`): enter a gene, see per-cancer-type statistics with the
  pass/fail call, the PubMed pre-check (pass / fail / not run / expired),
  flags (incl. tissue-of-origin), the literature verdict and
  citations for passing pairs, the bundle/release provenance, and start the
  literature check. Its files live in `src/expression_context/static/`; a
  "Expression stats" tab is added to the main page only while enabled.
- All API routes are behind `require_auth`.

Literature verdict (v1, `literature_gate = "acgc_pipeline_v1"`): run the
existing pipeline with `tumor_type = tumor_type_query`; supported iff
`insufficient_evidence` is false AND ≥1 cited PMID. Stored with PMIDs and
rationale.

## Tables

All created by the module, only when enabled; dropped on removal.

- `expression_context_results` — literature verdicts joined with the stats:
  `hugo_symbol, oncotree_code, release_id, literature_gate, surfaced (bool),
  exclusion_reason, stats_json, evidence_json, computed_at, requested_by`;
  unique `(hugo_symbol, oncotree_code, release_id, literature_gate)`.
- `expression_context_bundles` — one row per bundle loaded or attempted:
  `bundle_tag` (PK), `release_id`, `schema_version`, `method_version`,
  `pubmed_snapshot_date`, `published_at`, `release_url`, `gene_count`,
  `sha256`, `size_bytes`, `manifest_json`, `status` (`loading|ready|failed`),
  `loaded_rows`, `error`, `load_started_at`, `finished_at`, `retired_at`
  (set when it left {active, previous}; deleted after the grace period).
- `expression_context_stats` — `(bundle_tag, hugo_symbol)` PK,
  `entrez_gene_id`, `doc_zlib` (the gene's JSON line, zlib-compressed: a
  32-cancer-type document is ~16 KB of JSON and ~4.5 KB compressed, so
  ~90 MB per ~20.5k-gene bundle; the active and previous bundles are kept,
  plus a loading one and recently retired ones for up to the grace period).
- `expression_context_aliases` — `(bundle_tag, alias)` PK, `hugo_symbol`.
- `expression_context_state` — one row (`id = 1`): `active_bundle_tag`,
  `previous_bundle_tag`, `activated_at`, `last_checked_at`,
  `next_check_after`, `last_error`, `last_error_at`, and the single-loader
  lease `loader_owner`, `loader_expires_at`.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `EXPRESSION_CONTEXT_ENABLED` | `false` | Turns the whole module on. |
| `EXPRESSION_CONTEXT_RELEASES_REPO` | `oncokb/agentic-cancer-gene-classification` | Where bundles are published. |
| `EXPRESSION_CONTEXT_PINNED_BUNDLE_TAG` | unset | Load exactly this tag; never advance. |
| `EXPRESSION_CONTEXT_GITHUB_TOKEN` | unset | Optional; raises the API rate limit only. |
| `EXPRESSION_CONTEXT_REFRESH_INTERVAL` | `PT6H` | How often the cluster checks for a newer bundle. |
| `EXPRESSION_CONTEXT_REFRESH_ERROR_RETRY` | `PT30M` | Back-off after a failed GitHub call; re-check cadence while nothing is loaded. |
| `EXPRESSION_CONTEXT_MAX_RELEASE_PAGES` | `5` | Release-list pages read per discovery (counts toward the API budget). |
| `EXPRESSION_CONTEXT_BUNDLE_PRUNE_GRACE` | `PT1H` | How long a retired bundle stays readable before deletion. |
| `EXPRESSION_CONTEXT_REDIS_URL` | ACGC's Redis | Redis for the module's leases. |

Further tuning knobs (timeouts, size limits, batch size, lease timings, pool
size) are documented in `src/expression_context/settings.py`.

## Later

- Swap `LiteratureGate` to LRAS when `/lras/v1/retrieve` ships.
- Normal-tissue comparison (TCGA adjacent / GTEx) as a separate track.
