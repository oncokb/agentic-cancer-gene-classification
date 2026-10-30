# Using the ACGC API from scripts

This guide is for MSK staff who want to query ACGC (Agentic Cancer Gene
Classification) from a script or pipeline. It covers getting an API key,
asking ACGC about genes, and reading the results.

For the gene query API (`/v1/genes/...`), what you get back for each gene is
its classification and rationale: whether it's cancer-associated, its gene
class, a short rationale with supporting PubMed IDs, and a support score.
Gene-query responses also include a `view_url` link that opens the full report
in the ACGC web app (evidence cards, abstracts, quotes, clinical actionability)
for anyone who signs in. The annotate endpoints in section 5 return a richer
`AnnotationResult` and do not include `view_url`.

Base URL: `https://acgc.oncokb.org`

## 1. Get an API key

1. Sign in at <https://acgc.oncokb.org> with your MSK account.
2. Click **API keys** under your name in the sidebar, or go to
   <https://acgc.oncokb.org/api-keys>.
3. Create a key: give it a name you'll recognize later (for example
   `nightly triage script`) and pick an expiry. The default is 90 days; the
   maximum is 365.
4. **Copy the key now.** It starts with `acgc_` and is shown only once. ACGC
   stores only a hash of it, so a lost key can't be recovered. Revoke it and
   create a new one instead.

Keep the key in an environment variable or a secret store, never in git:

```bash
export ACGC_API_KEY=acgc_...
```

### Key rules

- A key acts as you. Anything it does is logged under your account.
- A key stops working when you revoke it, when it expires, or when your
  account is no longer allowed to sign in to ACGC.
- Each key can make **60 requests per minute** by default. Past that you get
  `429 Too Many Requests` with a `Retry-After` header.
- A key can't create, list or revoke keys. Manage keys on the API keys page
  while signed in.
- On the API keys page you can see each key's status and when it was last
  used, and revoke it. Admins can see and revoke everyone's keys.

## 2. Make your first request

Send the key in the `Authorization` header:

```bash
curl -s "https://acgc.oncokb.org/v1/genes/BRAF?tumor_type=Melanoma" \
  -H "Authorization: Bearer $ACGC_API_KEY"
```

To ask about several genes at once:

```bash
curl -s -X POST https://acgc.oncokb.org/v1/genes/query \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"genes": ["ALK", {"gene": "TP53", "tumor_type": "LUAD"}]}'
```

## 3. Endpoints

| Endpoint | Use it for |
| --- | --- |
| `GET /v1/genes/{symbol}` | One gene. Optional query parameters: `tumor_type`, `force_refresh=true`. |
| `POST /v1/genes/query` | Up to 50 genes in one request. Waits for all of them. |
| `POST /v1/genes/query/jobs` | The same request, run in the background. Use it for larger batches or when a request might take a while. |
| `GET /v1/genes/query/jobs/{job_id}` | Check on a background job. |

### Request body (`POST /v1/genes/query` and `/jobs`)

```json
{
  "genes": ["ALK", {"gene": "TP53", "tumor_type": "LUAD"}],
  "force_refresh": false
}
```

- **`genes`**: required, 1 to 50 entries. Each entry is either a gene symbol
  string or an object with `gene` and an optional `tumor_type`. You can mix
  the two forms.
- **`force_refresh`**: optional, default `false`. See
  [Speed and freshness](#6-speed-and-freshness).

Accepted gene identifiers for the gene query API:

- HGNC symbols such as `ALK`, `HLA-A` or `C1orf112`; Ensembl gene,
  transcript and protein IDs such as `ENSG00000141510.17`, `ENST00000269305`
  or `ENSP00000269305`; and RefSeq accessions such as `NM_000546.6` or
  `NP_000537`. Either kind also works as `TP53(ENST00000269305)`,
  `TP53(NM_000546.6)`, `ENST00000269305:c.215C>G` or `NM_000546.6:c.215C>G`.
  See [Gene identifiers](#gene-identifiers).
- Symbols: letters, digits, `-` and `.` only, starting and ending with a
  letter or digit.
- **Not fusions.** `EML4::ALK`, `ALK::` and similar are rejected with `422`.
  Use the full `/v1/annotate` endpoint for fusions (see the
  [README](../README.md#acgc-api-keys-for-scripts)).
- Repeated symbols are counted once, ignoring case. If you give the same gene
  more than once with different tumor types, the first one is used.

### Response

```json
{
  "run_id": "0b8f3c2e-5d1a-4c3e-9f57-2a6c1d9e4b10",
  "view_url": "https://acgc.oncokb.org/?run=0b8f3c2e-5d1a-4c3e-9f57-2a6c1d9e4b10",
  "results": [
    {
      "gene": "ALK",
      "tumor_type": null,
      "cancer_associated": true,
      "gene_class": "Receptor tyrosine kinase",
      "in_oncokb": true,
      "rationale": "ALK rearrangements are established oncogenic drivers ...",
      "gene_summary": "ALK encodes a receptor tyrosine kinase ...",
      "citation_pmids": ["17625570", "20979469"],
      "evidence_support_score": 0.92,
      "quality_flags": [],
      "cache_status": "reused",
      "cached_at": "2026-10-01T14:03:11+00:00",
      "error": null
    }
  ],
  "warnings": []
}
```

`GET /v1/genes/{symbol}` returns the same shape, with one entry in `results`.
`warnings` is the run-level list described under [annotate warnings](#post-v1annotate).

| Field | Meaning |
| --- | --- |
| `run_id` | ID of the saved run that holds these results. |
| `view_url` | Link to the full report in the ACGC web app. Whoever opens it signs in first if they aren't already, then lands on the report. |
| `warnings` | Run-level warnings, such as conflicting tumor types or fusion-like notation. Empty when there are none. |
| `gene` | The gene symbol ACGC classified. This can differ from what you sent, for example when an Ensembl ID or an old alias resolves to the current symbol. |
| `submitted_inputs` | What you sent, when it was rewritten to `gene` (for example an Ensembl transcript ID). Empty otherwise. |
| `warnings` | Caveats about how an input was read, for example an HGVS variant that was not interpreted. |
| `tumor_type` | The tumor type the classification actually used, or `null`. |
| `cancer_associated` | Whether ACGC classifies the gene as cancer-associated. |
| `gene_class` | Functional class, for example a kinase. |
| `in_oncokb` | Whether the gene is in OncoKB. |
| `rationale` | The reasoning behind the classification. |
| `gene_summary` | A short summary of what the gene does. |
| `citation_pmids` | PubMed IDs the rationale cites. These are checked against the papers ACGC actually retrieved. |
| `evidence_support_score` | 0 to 1: how well the answer is backed by the literature ACGC found. It is not a probability that the classification is biologically correct. |
| `quality_flags` | Codes for any quality concerns raised during classification. |
| `cache_status`, `cached_at` | Whether a saved result was reused or the gene was classified fresh, and when that result was produced. |
| `error` | Set only when this gene couldn't be classified. See [Errors](#7-errors). |

The gene query API deliberately leaves out abstracts, quotes, evidence cards
and OpenEvidence content. Open its `view_url` to see them. (The annotate
endpoints return full annotations, including evidence cards.)

### Gene identifiers

Every endpoint that takes genes or fusions (`/v1/genes/...`, `/v1/annotate`,
`/v1/annotate/jobs`, `/v1/annotate/gene`, `/v1/fusion-evidence/jobs`) accepts,
alone or as either fusion partner:

| Input | Read as |
| --- | --- |
| `TP53` | HGNC symbol (aliases and previous symbols resolve to the current symbol). |
| `ENSG00000141510`, `ENSG00000141510.17` | The Ensembl gene's symbol. |
| `ENST00000269305`, `ENST00000269305.9` | The symbol of the gene that owns the transcript. |
| `ENSP00000269305` | Protein → its transcript → that transcript's gene. |
| `TP53(ENST00000269305)` | `TP53`, after checking the transcript belongs to TP53. In a fusion, the transcript is used as that partner's breakpoint transcript. |
| `ENST00000269305:c.215C>G`, `ENSP00000269305:p.Arg72Pro` | The transcript's gene, with a warning: the variant itself is **not** interpreted. |
| `NM_000546`, `NM_000546.6`, `NR_003051`, `XM_...`, `XR_...` | The symbol of the gene NCBI lists for the RefSeq transcript. |
| `NP_000537`, `NP_000537.3`, `XP_...` | The symbol of the gene NCBI lists for the RefSeq protein. |
| `TP53(NM_000546.6)` | `TP53`, after checking the accession belongs to TP53. In a fusion, a transcript accession is used as that partner's breakpoint transcript, e.g. `EML4(NM_019063.5)::ALK(NM_004304.5)`. |
| `NM_000546.6:c.215C>G`, `NM_000546.6(TP53):c.215C>G`, `NP_000537.3:p.Arg72Pro` | The accession's gene, with a warning: the variant itself is **not** interpreted. |

So `ENST00000318522::ENST00000389048` and `NM_019063.5::NM_004304.5` are both
annotated as `EML4::ALK`, and fusion evidence searches PubMed for the symbols,
not the IDs. Only human stable IDs (`ENSG`/`ENST`/`ENSP` plus 11 digits,
optional `.version`) and human RefSeq transcript/protein accessions (`NM_`,
`NR_`, `XM_`, `XR_`, `NP_`, `XP_` plus digits, optional `.version`) are
accepted; other RefSeq types such as `NC_` (chromosomes) or `NG_` (genomic
regions) are rejected. RefSeq accessions are looked up in NCBI (Datasets gene
API); NCBI reports the gene for an accession regardless of version, so the
version is kept in your input but not checked. Neither kind is ever
fuzzy-matched to a gene.

Your original input stays in the result: `resolved_inputs` on each annotation
(`input`, `resolved`, `five_transcript`, `three_transcript`, `warnings`),
`submitted_inputs` in gene-query results, and `input` on fusion-evidence
results. Request-level `warnings` repeat any caveats and errors.

Some current human Ensembl genes (for example novel lncRNAs such as
`ENSG00000225718`) have neither an HGNC symbol nor an Ensembl name. Such a gene
is annotated under its unversioned Ensembl gene ID, with a warning like
"ENSG00000225718 is a current human lncRNA with no HGNC symbol or Ensembl name;
the Ensembl ID is used as its name." The ID is not an HGNC symbol, so fusion
evidence searches PubMed for the ID itself (e.g. `PTPRN2::ENSG00000225718`),
plus the other partner's aliases.

An identifier that can't be used gets its own error row and is not annotated
as any gene. That covers an unknown, non-human or malformed ID, an Ensembl or
NCBI lookup that failed (retry), and `GENE(ENST...)` / `GENE(NM_...)` where the
transcript belongs to a different gene. In the gene query API the last case
reads "The Ensembl ID belongs to a different gene than the one named." or "The
RefSeq accession belongs to a different gene than the one named."

For Ensembl IDs Ensembl's lookup doesn't know, the archive tells the two
permanent cases apart: "Ensembl ID ENSG... is retired (last seen in release
N)." (naming a replacement only when Ensembl suggests one) or "Ensembl did not
find ID ENSG...". Timeouts, network errors, HTTP 429, 5xx and unreadable
responses are retried with backoff (honouring `Retry-After`), up to 3 attempts
per request, and all Ensembl requests for one submission share a deadline of
about 15 seconds. Then the error reads "Ensembl lookup temporarily unavailable
(timeout); retry later." (or `(HTTP 429)`, `(HTTP 503)`, `(invalid response)`,
...). The same message, with `(inconsistent response)`, is used when Ensembl
contradicts itself, e.g. its lookup lacks an ID its archive calls current. Only
these are worth retrying. Successful lookups, and retired or not-found IDs the
archive has confirmed, are cached per ID; failures are not.

In a fusion, only the bad partner is rejected: the other partner is still
annotated on its own. For `EML4(ENST00000256078)::ALK` (a KRAS transcript),
ALK is annotated, and the error row and request `warnings` say "5′ partner of
'EML4(ENST00000256078)::ALK' rejected — ... belongs to KRAS, not EML4". Fix
the partner and resubmit to annotate the fusion as a pair.

Fusion evidence for a fusion with a rejected partner is not searched. The entry
has `retrieval_status: "failed"` and `literature_scope: null`, and its
`interpretation` gives the reason. The empty result does not mean there is no
literature.

## 4. Gene query background jobs

Start a gene-query job with the same body as `POST /v1/genes/query`:

```bash
curl -s -X POST https://acgc.oncokb.org/v1/genes/query/jobs \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"genes": ["ALK", "TP53", "BRAF"]}'
# {"job_id": "...", "status_url": "/v1/genes/query/jobs/..."}
```

Then check `status_url` every 10 to 30 seconds:

```bash
curl -s "https://acgc.oncokb.org/v1/genes/query/jobs/$JOB_ID" \
  -H "Authorization: Bearer $ACGC_API_KEY"
```

```json
{
  "job_id": "...",
  "status": "complete",
  "genes_completed": 3,
  "genes_total": 3,
  "run_id": "...",
  "view_url": "https://acgc.oncokb.org/?run=...",
  "results": [ ... ],
  "warnings": [],
  "error": null
}
```

- `status` is `queued`, `running`, `complete` or `failed`.
- `genes_completed` and `genes_total` show progress. `results` fills in as
  genes finish.
- `run_id` and `view_url` are set only once `status` is `complete`.
- `warnings` is an empty list (`[]`) until `status` is `complete`, then holds
  the same warnings as `POST /v1/genes/query` would return.
- If `status` is `failed`, `error` holds a short message. Start a new job to
  retry.

## 5. Annotate genes and fusions

`POST /v1/annotate` is the general annotation endpoint. It accepts genes and
fusions in the same request and returns one annotation per unique gene. Use it
when your input contains fusions; for genes only, the smaller
[gene query API](#3-endpoints) is usually enough.

```bash
curl -s -X POST https://acgc.oncokb.org/v1/annotate \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "fusions": [
          "ALK",
          {"fusion": "EML4::ALK", "tumor_type": "LUAD"}
        ],
        "mode": "core"
      }'
```

### `POST /v1/annotate`

| Field | Meaning |
| --- | --- |
| `fusions` | Required, at least one entry. Each entry is a gene or fusion string, or an object with `fusion` (aliases `gene`, `input` and `query` also work) and an optional `tumor_type`. |
| `include_fusion_evidence` | Optional, default `false`. Retrieves exact-pair evidence inline before saving and returning the run; also supported by `/v1/annotate/jobs`. |
| `mode` | `"full"` (default) or `"core"`. `"core"` prioritizes the cancer association, rationale, summary and citations. |
| `force_refresh` | Optional, default `false`. Bypasses saved gene annotations and fusion evidence and recomputes them. |
| `skip_literature_for_oncokb` | Optional, default `false`. Genes confirmed in OncoKB return a deterministic OncoKB-based annotation without PubMed retrieval or LLM synthesis. |

Each fusion is split into its partner genes, and every unique gene is annotated
once. `fusions_processed` counts submitted inputs; `genes_annotated` counts
unique genes. Each request to `/v1/annotate`, `/v1/annotate/jobs`, or
`/v1/fusion-evidence/jobs` accepts at most `ANNOTATE_MAX_INPUTS` entries
(default 50), before deduplication or filtering. Over-limit requests return
`422` with a string `detail`, for example
`{"detail": "At most 50 inputs may be annotated per request"}`.

`warnings` defaults to `[]`. When a gene has conflicting tumor types, it names
the gene, the type used, and the ignored types; annotation still uses one type
per gene. Comparisons ignore case and surrounding/repeated whitespace while
warnings preserve the original strings.

```json
{
  "run_id": "0b8f3c2e-5d1a-4c3e-9f57-2a6c1d9e4b10",
  "timestamp": "2026-10-26T14:03:11+00:00",
  "fusions_processed": 2,
  "genes_annotated": 2,
  "annotations": [
    {
      "gene": "ALK",
      "fusions": ["EML4::ALK"],
      "in_oncokb": true,
      "cancer_associated": true,
      "gene_class": "Receptor tyrosine kinase",
      "cancer_type_prevalence": "LUAD",
      "gene_summary": "ALK encodes a receptor tyrosine kinase ...",
      "citations": ["17625570", "20979469"],
      "evidence_support_score": 0.92,
      "cache_status": "reused",
      "cached_at": "2026-10-01T14:03:11+00:00",
      "error": null
    }
  ],
  "fusion_evidence": [],
  "warnings": [],
  "timings_ms": {"normalization": 120.4, "annotation": 4300.2, "total": 4420.6}
}
```

_The `annotations` array above is abbreviated: this request returns one entry
for `ALK` and one for `EML4`. The other entry is omitted here for brevity._

`fusion_evidence` is empty by default. Set `include_fusion_evidence: true`
to retrieve it before the annotation result is saved and returned. This also
adds `timings_ms.fusion_evidence`, included in `timings_ms.total`. Individual
fusion retrieval failures are logged and represented in the evidence result
without failing annotation. Alternatively, retrieve it separately with
[Fusion evidence jobs](#post-v1fusion-evidencejobs), optionally attaching it
to the saved run with `run_id`.

Saving the run is best-effort: if persistence fails, the response is still
returned with its `run_id`, but `GET /v1/annotate/{run_id}` may then return
`404` (`src/main.py:799-809`, `src/main.py:1557-1560`).

### `POST /v1/annotate/jobs`

Starts the same request in the background and returns a job ID. Use jobs for
larger batches, or whenever a synchronous request might take longer than the
production ingress timeout (about 300 seconds).

```bash
curl -s -X POST https://acgc.oncokb.org/v1/annotate/jobs \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"fusions": ["EML4::ALK", "ETV6::NTRK3"], "mode": "core"}'
```

```json
{
  "job_id": "6f2d8a1c-7b3e-4a9d-9c11-8f0e2d4b6a70",
  "status_url": "/v1/annotate/jobs/6f2d8a1c-7b3e-4a9d-9c11-8f0e2d4b6a70"
}
```

### `GET /v1/annotate/jobs/{job_id}`

Poll the `status_url` until `status` is `complete` or `failed`.

```json
{
  "job_id": "6f2d8a1c-7b3e-4a9d-9c11-8f0e2d4b6a70",
  "status": "complete",
  "phase": "complete",
  "fusions_processed": 2,
  "genes_completed": 4,
  "genes_total": 4,
  "annotations": [ ... ],
  "result": {
    "run_id": "0b8f3c2e-5d1a-4c3e-9f57-2a6c1d9e4b10",
    "timestamp": "2026-10-26T14:03:11+00:00",
    "fusions_processed": 2,
    "genes_annotated": 4,
    "annotations": [ ... ],
    "fusion_evidence": [],
    "warnings": [],
    "timings_ms": {"total": 8120.4}
  },
  "error": null,
  "timings_ms": {"total": 8120.4}
}
```

- `status` is `queued`, `running`, `complete` or `failed`.
- `phase` is `queued`, `annotation`, `fusion_evidence`, `complete`, or `failed`;
  it distinguishes optional inline evidence retrieval from gene annotation.
- `annotations` fills in as genes finish; `genes_completed` and `genes_total`
  show progress.
- On success, `result` is the same `AnnotationResult` that `POST /v1/annotate`
  returns. Save `result.run_id`; the fusion-evidence step below needs it. Run
  saving is best-effort, so a `run_id` whose save failed will return `404` from
  `GET /v1/annotate/{run_id}`.
- On failure, `error` holds a short message.
- Expiry is measured from job **creation**, not completion
  (`ANNOTATION_JOB_TTL_SECONDS`, default 3600). Eviction runs only when another
  annotation job is launched, and only removes jobs that are already `complete`
  or `failed`. A job that ran longer than an hour can therefore be evicted as
  soon as it finishes, on the next job launch; a poll after eviction returns
  `404`. Jobs do not survive a process restart.

### `POST /v1/fusion-evidence/jobs`

Runs exact fusion-pair PubMed evidence retrieval in the background as an
alternative to `include_fusion_evidence: true` on annotation requests.

```bash
curl -s -X POST https://acgc.oncokb.org/v1/fusion-evidence/jobs \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
        "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}],
        "run_id": "0b8f3c2e-5d1a-4c3e-9f57-2a6c1d9e4b10"
      }'
```

| Field | Meaning |
| --- | --- |
| `fusions` | Required, at least one entry in the same string-or-object form as `POST /v1/annotate`. Only entries with a fusion separator and two non-empty partners are processed; gene-only entries and entries such as `ALK::` are ignored. |
| `force_refresh` | Optional, default `false`. Bypasses and replaces the 7-day fusion-evidence cache. |
| `run_id` | Optional. When set, ACGC best-effort saves the completed evidence onto that existing run, so a later `GET /v1/annotate/{run_id}` may include it. |

The same `ANNOTATE_MAX_INPUTS` cap (default 50) applies before filtering or
deduplication; exceeding it returns `422` with a string `detail`.
Identical `(fusion, tumor_type)` entries in one request are de-duplicated.

```json
{
  "job_id": "c1a4e8f2-3b6d-4c8e-9a0f-5d7b1e2c9a44",
  "status_url": "/v1/fusion-evidence/jobs/c1a4e8f2-3b6d-4c8e-9a0f-5d7b1e2c9a44"
}
```

### `GET /v1/fusion-evidence/jobs/{job_id}`

```json
{
  "job_id": "c1a4e8f2-3b6d-4c8e-9a0f-5d7b1e2c9a44",
  "status": "complete",
  "fusions_completed": 1,
  "fusions_total": 1,
  "fusion_evidence": [
    {
      "fusion": "EML4::ALK",
      "tumor_type": "LUAD",
      "well_supported": false,
      "retrieved_count": 2,
      "pmids": ["17625570", "20979469"],
      "interpretation": "EML4::ALK has 2 non-retracted PubMed record(s) from exact fusion-pair searches, below the well-supported threshold. Papers mentioning LUAD in the title or abstract: 1 of 2.",
      "evidence_cards": [ ... ],
      "literature_scope": "requested_tumor",
      "requested_tumor_pmids": ["17625570"],
      "retrieval_status": "ok"
    }
  ],
  "error": null,
  "timings_ms": {"total": 6240.1}
}
```

- `status` is `queued`, `running`, `complete` or `failed`.
- `fusion_evidence` fills in as fusions finish; `fusions_completed` and
  `fusions_total` show progress.

Each `fusion_evidence` entry (the same shape is returned inline by
`include_fusion_evidence: true`):

| Field | Meaning |
| --- | --- |
| `fusion`, `tumor_type` | The input pair and the tumor type you sent, or `null`. |
| `pmids`, `retrieved_count` | Every non-retracted PMID whose title or abstract discusses the exact pair, in ranked order. Both a tumor-scoped and a tumor-agnostic search are run when `tumor_type` is set. |
| `evidence_cards` | Cards for the top 5 `pmids` only. Each card's `requested_tumor_terms` lists the requested tumor type or alias terms found in that paper's title or abstract. |
| `literature_scope` | Computed over all `pmids`, not just the cards. `requested_tumor`: at least one paper **mentions** the requested tumor type; this does not mean the fusion was shown in that tumor. `other_or_unspecified`: a tumor type was sent but no paper mentions it. `any_tumor`: no tumor type was sent and papers were found. `no_literature`: all searches succeeded and no paper was retained. `null` when retrieval failed, a partial result has no retained papers or its tumor-scoped search failed, the input is not a two-partner fusion, or the result predates this field. |
| `requested_tumor_pmids` | The `pmids` that mention the requested tumor type, in ranked order. These can include papers without a card. |
| `partial` | Boolean, default `false`. One exact-pair search failed; evidence from successful searches is retained and the interpretation describes the failure. Partial results are never cached. Scope is unknown (`null`) when no papers were retained or the tumor-scoped query failed; otherwise scope reflects retained papers. The UI labels partial lookups, groups unknown-scope partial results under Lookup failed, and suppresses the no-literature partner prompt. |
| `retrieval_status` | `ok` when at least one search succeeded (including partial results), or for non-pair inputs where no lookup applies; `failed` when retrieval raised an error. A failed entry is empty because the lookup failed, not because no literature exists; retry it. |

Tumor mentions are matched deterministically on word boundaries against the
same expanded terms as the tumor-scoped query (for example `LUAD` also matches
"lung adenocarcinoma"). The case of `tumor_type` does not matter. A hyphenated
neighbour ends a match, so `breast` does not match "breast-feeding"; this covers
Unicode dashes (en and em dashes, non-breaking, soft and full-width hyphens,
minus signs) as well as `-`, and these dashes in `tumor_type` itself are treated
as `-`. A preceding "non" also ends a match, so `SCLC` does not match
"non-small cell lung cancer", "non small cell lung cancer" or a line-broken
"non- small cell lung cancer".
`ALL` also matches "B-ALL" and "T-ALL".
Abbreviations match case-sensitively as uppercase tokens; an unlisted
`tumor_type` of up to 5 letters or digits (such as `dsrct`) is treated as an
abbreviation unless it is a common word such as `lung`. Two-letter
abbreviations such as `MM` or `os` count only in titles, and "uterine tumor
resembling ovarian sex cord tumor" does not count as an ovarian tumor mention.
- **The job status response is the authoritative source for the evidence.** If
  `run_id` was supplied, the job marks itself `complete` first and then
  best-effort attaches the evidence to that run. A missing run is silently
  skipped; other persistence errors are only logged. The run may therefore lag
  behind the job, and `GET /v1/annotate/{run_id}` is not guaranteed to include
  the evidence (`src/main.py:1728-1734`, `src/main.py:812-826`).
- Fusion-evidence job records have no age-based eviction, but they are
  process-local and are lost on restart.

### `GET /v1/annotate/{run_id}`

Returns the saved `AnnotationResult` for a run without recomputing it. This is
how you read back inline fusion evidence or evidence attached by a successful
`/v1/fusion-evidence/jobs` request.

```bash
curl -s "https://acgc.oncokb.org/v1/annotate/$RUN_ID" \
  -H "Authorization: Bearer $ACGC_API_KEY"
```

The response has the same shape as `POST /v1/annotate`; it does **not** include
a `view_url`. Build the report link yourself from the run ID:

```text
https://acgc.oncokb.org/?run=<run_id>
```

That is the same link format the app uses (`_run_view_url` in `src/main.py`).
`fusion_evidence` is populated if the annotation request set
`include_fusion_evidence: true` or a fusion-evidence job successfully attached
it to this run; otherwise it is `[]`. An unknown `run_id` returns `404`.

### `POST /v1/runs/{run_id}/rerun`

Start a **new** annotation job with a saved run's original request: the same
inputs (tumor types, exon, genomic and transcript fields), `mode`,
`include_fusion_evidence`, `skip_literature_for_oncokb` and `local_backend`.
It behaves exactly like `POST /v1/annotate/jobs`, including the input cap and
API-key rate limits. The original run is never modified, so links to it keep
working.

The body is optional. `force_refresh` (default `false`) chooses between
reusing cached results and recomputing them; the original run's own
`force_refresh` is not reused.

```bash
curl -s -X POST "https://acgc.oncokb.org/v1/runs/$RUN_ID/rerun" \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"force_refresh": false}'
```

```json
{
  "job_id": "6f2d8a1c-7b3e-4a9d-9c11-8f0e2d4b6a70",
  "status_url": "/v1/annotate/jobs/6f2d8a1c-7b3e-4a9d-9c11-8f0e2d4b6a70",
  "rerun_of": "example-run-id",
  "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD", "five_exon": 13, "three_exon": 20}],
  "mode": "full"
}
```

Poll `status_url` as for `POST /v1/annotate/jobs`; the completed job's
`result.run_id` is the new run. `fusions` and `mode` are the inputs the new run
was started with, e.g. for a follow-up `POST /v1/fusion-evidence/jobs`.

Any signed-in, active user (or API key) may rerun any run they can open. The
new run belongs to the caller and is saved with `source` `"rerun"` and
`rerun_of` (the original `run_id`) in its stored request. Runs saved by
`POST /v1/annotate/gene` are rerun as a one-input annotate request. Errors:
`401` unauthenticated, `403` account pending or disabled, `404` unknown run,
`422` if the run's stored request can't be rerun (for example an older run
saved without its inputs) or exceeds the input cap, and `503` if run storage
isn't configured.

### `POST /v1/annotate/gene`

Annotate one gene and return its full `GeneAnnotation` fields, plus the saved
run's `run_id` and a `view_url`. The request body takes `gene` and an optional
`tumor_type`:

```bash
curl -s -X POST https://acgc.oncokb.org/v1/annotate/gene \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"gene": "ALK", "tumor_type": "LUAD"}'
```

The response also includes the run-level `warnings` list, with the same shape
and meaning as `POST /v1/annotate`; it is `[]` when there are none. Because
this endpoint takes one input, its warnings are normally about notation (for
example `EWSR1-FLI1`), not conflicting tumor types.

### Batch workflow

For a large gene/fusion list, this sequence avoids request timeouts and gives
you exact-pair fusion evidence:

1. Split the inputs into chunks of at most `ANNOTATE_MAX_INPUTS` (default 50).
   Larger requests return `422` with a string `detail`; synchronous requests
   can also exceed the production ingress timeout (about 300 seconds).
2. For each chunk, `POST /v1/annotate/jobs` with the chunk's `fusions`. Set
   `include_fusion_evidence: true` to retrieve evidence in the same job.
3. Poll `GET /v1/annotate/jobs/{job_id}` every 15 to 30 seconds until `status`
   is `complete` or `failed`. On `429`, wait for `Retry-After` seconds and
   retry the same poll.
4. From the completed job, keep `result.annotations` and `result.run_id`. If
   inline evidence was requested, read `result.fusion_evidence` and skip steps 5–7.
5. Otherwise, if the chunk contains fusions, `POST /v1/fusion-evidence/jobs` with those
   fusion entries and `run_id` set to `result.run_id`. Gene-only entries can be
   left in the list; they are ignored.
6. Poll `GET /v1/fusion-evidence/jobs/{job_id}` every 15 to 30 seconds, again
   honoring `Retry-After` on `429`, until `status` is `complete` or `failed`.
7. Read the evidence from the fusion job's `fusion_evidence`. That response is
   authoritative. If you also supplied `run_id`, you can fetch the run with
   `GET /v1/annotate/{run_id}` and construct its report link as
   `https://acgc.oncokb.org/?run=<run_id>`; attachment is best-effort, so the
   run may not have the evidence yet (or at all).

Status polls count toward the **60 requests per minute** per-key limit, so keep
polling intervals at 15 seconds or longer when you have several jobs running.

### Known limitations

- `POST /v1/annotate` (and its jobs) returns `fusion_evidence: []` by default.
  Set `include_fusion_evidence: true` or run `POST /v1/fusion-evidence/jobs`
  separately for exact fusion-pair evidence.
- Only one tumor type is used per gene per request: the first non-empty
  `tumor_type` in submission order among all inputs that resolve to that gene,
  whatever their spelling (`TP53`, an alias, `ENSG...`, `ENST...`, `ENSP...`
  or `TP53(ENST...)`). Ignored conflicting types are named in the
  response-level `warnings`. To annotate a gene in more than one tumor type,
  send one request per tumor type.
- Fusion separators are `::`, `--` and `/` only. A single hyphen is **not** a
  separator: `EWSR1-FLI1` is treated as one gene symbol, not a fusion. Write
  `EWSR1::FLI1`.
- Job state is held in memory in the process that created it. Restarting the
  app loses all annotation and fusion-evidence jobs, even ones that were
  running.
- Annotation-job expiry is measured from **creation**, not completion. Eviction
  runs when another annotation job is launched and removes completed or failed
  jobs older than the TTL (default one hour), so a job that runs longer than an
  hour can be evicted as soon as it finishes.
- Fusion-evidence jobs only process entries that contain a fusion separator and
  have two non-empty partners. Gene-only entries and malformed entries such as
  `ALK::` are silently skipped.
- Exact-pair evidence is cached for 7 days. Set `force_refresh: true` on an
  inline annotation request or fusion-evidence job to bypass and replace it.

## 6. Speed and freshness

- ACGC saves each gene's result and reuses it while it's still fresh, so asking
  about a gene someone has already looked at is usually quick. Each result says
  whether it was reused (`cache_status`) and when it was produced
  (`cached_at`).
- Gene annotations are cached by `(gene, tumor_type)` and reused for up to 180
  days. After that ACGC checks PubMed for newer PMIDs and keeps reusing the
  stored annotation if none are found, so 180 days is not a hard maximum age
  (`src/config.py:132-140`, `src/pipeline/orchestrator.py:198-242`).
- A gene that hasn't been classified recently is classified from scratch. That
  involves PubMed searches and AI model calls, and typically takes 10 to 40
  seconds per gene.
- Exact fusion-pair evidence is cached separately for 7 days.
- `force_refresh: true` (or `?force_refresh=true` on the `GET`) ignores saved
  gene results and classifies every gene again. It's slower and costs more, so
  use it only when you need a fresh answer. It also bypasses and replaces the
  fusion-evidence cache when inline evidence is requested. For a separate
  fusion-evidence job, set `force_refresh: true` on that job request too.
  Refreshing the fusion-evidence result still reuses the underlying PubMed
  search and fetch caches (default TTL: 24 hours) to limit NCBI load.
- For more than a handful of uncached genes, use a background job rather than
  holding one request open.

## 7. Errors

### Whole-request errors

Rows that mention gene symbols, fusions, the `GENE_QUERY_MAX_GENES` 50-gene cap or `view_url` describe
the **gene query API** (`/v1/genes/...`). `POST /v1/annotate` accepts fusions,
has the separate `ANNOTATE_MAX_INPUTS` cap (default 50), and does not return
`view_url`. The `401`, `403`, `429` and
`503` cases apply to all endpoints.

| Status | Meaning | What to do |
| --- | --- | --- |
| `401` | No key, or the key is wrong, revoked or expired, or its owner is no longer allowed to use ACGC. | Check `ACGC_API_KEY`, or create a new key. |
| `403` | Your account is waiting for administrator approval, or a key was used to manage keys. | Contact the ACGC team, or manage keys on the API keys page. |
| `404` | Gene query API, `GET /v1/genes/{symbol}`: the gene doesn't exist (HGNC or Ensembl confirmed no match). Annotate, `GET /v1/annotate/{run_id}`: the run doesn't exist. | Check the symbol or run ID. |
| `422` | Gene query API: a bad gene symbol, a fusion, more than 50 genes, or invalid JSON. Annotation and fusion-evidence requests: an empty `fusions` list, invalid JSON, or more than `ANNOTATE_MAX_INPUTS` entries (default 50; over-limit responses have a string `detail`). | Fix the request. The response says which field is wrong, or states the input limit. |
| `429` | You exceeded your key's rate limit. | Wait for the number of seconds in `Retry-After`, then retry. |
| `500` | Gene query API: ACGC couldn't save the run, so no `view_url` is returned. Annotate: an unexpected pipeline error (run saving is separate and best-effort). | Retry later. |
| `503` | A gene lookup service (HGNC or Ensembl) was temporarily unavailable, or ACGC can't verify keys right now. | Retry after a short wait. |

### Per-gene errors in batch requests

In `POST /v1/genes/query` and in jobs, a problem with one gene doesn't fail
the whole request. That gene's entry has `error` set instead:

| `error` | Meaning |
| --- | --- |
| `Gene symbol could not be resolved.` | The gene doesn't exist. Check the symbol. |
| `Gene symbol lookup is temporarily unavailable; please retry.` | A lookup service was down. Retry that gene. |

For the gene query API, the run is still saved, and `view_url` still works for
the genes that succeeded.

## 8. Python example

```python
import os
import time

import requests

BASE = "https://acgc.oncokb.org"
HEADERS = {"Authorization": f"Bearer {os.environ['ACGC_API_KEY']}"}


def query_genes(genes):
    """Classify genes with a background job and return the finished job."""
    resp = requests.post(
        f"{BASE}/v1/genes/query/jobs", json={"genes": genes}, headers=HEADERS, timeout=30
    )
    resp.raise_for_status()
    status_url = BASE + resp.json()["status_url"]

    while True:
        resp = requests.get(status_url, headers=HEADERS, timeout=30)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", "10")))
            continue
        resp.raise_for_status()
        job = resp.json()
        if job["status"] in ("complete", "failed"):
            return job
        time.sleep(15)


job = query_genes(["ALK", {"gene": "TP53", "tumor_type": "LUAD"}, "BRAF"])
if job["status"] == "failed":
    raise SystemExit(f"Job failed: {job['error']}")

print("Full report:", job["view_url"])
for r in job["results"]:
    if r["error"]:
        print(f"{r['gene']}: ERROR {r['error']}")
    else:
        print(
            f"{r['gene']}: cancer_associated={r['cancer_associated']} "
            f"support={r['evidence_support_score']:.2f} PMIDs={r['citation_pmids']}"
        )
```

## 9. More detail

- Every endpoint and field is also listed in the interactive API reference at
  <https://acgc.oncokb.org/docs>.
- Operator details, such as configuration settings, rate-limit storage and
  logging, are in the README sections
  [ACGC API Keys (for scripts)](../README.md#acgc-api-keys-for-scripts) and
  [Gene query API](../README.md#gene-query-api).
- Questions or problems: contact the ACGC team.

## GET /v1/runs

List your saved runs, newest first. Requires a signed-in session or an API key;
API keys list runs belonging to the key owner. With authentication disabled for
local development, the owner is `local@internal`.

Query parameters: `limit` (default `20`, range `1`–`100`) and optional opaque
`cursor` (use the preceding response's `next_cursor` unchanged).

```json
{
  "runs": [
    {
      "run_id": "example-run-id",
      "created_at": "2026-10-26T12:00:00+00:00",
      "source": "session",
      "input_summary": "ALK, TP53",
      "input_count": 2,
      "genes_annotated": 2,
      "view_url": "/?run=example-run-id"
    }
  ],
  "next_cursor": null
}
```

`source` is `session`, `api_key`, `local` or `rerun` (started with
`POST /v1/runs/{run_id}/rerun`). `source`, `input_summary`, `input_count`, and `genes_annotated` may be null.
`next_cursor` is null on the last page, including an empty list. Responses contain
metadata only, never request or result blobs. Invalid limits or cursors return
`422`, whose `detail` may be a string or FastAPI's validation-error list.
Unauthenticated requests return `401`. Run storage that isn't configured returns
`503`; a database outage returns `500`, like `GET /v1/annotate/{run_id}`.
`created_at` is an ISO-8601 UTC timestamp and may include microseconds.
The `run_id` in `view_url` is URL-encoded.

## GET /v1/runs/{run_id}/inputs

Return the fusion inputs a saved run was submitted with, so a client can retry
individual genes without reconstructing tumor types or breakpoints from the
annotations. Requires a signed-in session or API key; only the run's owner or an
administrator may read them.

```json
{
  "run_id": "example-run-id",
  "inputs": [
    {
      "fusion": "EML4::ALK",
      "tumor_type": "LUAD",
      "five_exon": null,
      "three_exon": null,
      "five_genomic": null,
      "three_genomic": null,
      "five_transcript": "ENST00000318522",
      "three_transcript": null
    }
  ]
}
```

Inputs come back as submitted; a run saved by `POST /v1/annotate/gene` returns
its gene as a single input. Unknown runs return `404`, non-owners return `403`,
unauthenticated requests return `401`, and run storage that isn't configured
returns `503`.

## POST /v1/runs/{run_id}/retry

Re-annotate failed or no-result genes on an existing saved run and merge the
results into that run in place. A retry works on genes, not on the original
submissions: a fusion's partners are retried one gene at a time, and fusion
evidence has its own retry (`POST /v1/fusion-evidence/jobs` with `run_id`).
Only the run's owner or an administrator may retry (an API key acts as its
owner; a run with no recorded owner is admin-only).

```bash
curl -s -X POST https://acgc.oncokb.org/v1/runs/example-run-id/retry \
  -H "Authorization: Bearer $ACGC_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "kind": "errors",
    "inputs": [{"fusion": "ALK", "tumor_type": "LUAD"}]
  }'
```

```json
{
  "job_id": "c1a4e8f2-3b6d-4c8e-9a0f-5d7b1e2c9a44",
  "status_url": "/v1/annotate/jobs/c1a4e8f2-3b6d-4c8e-9a0f-5d7b1e2c9a44"
}
```

| Field | Description |
|---|---|
| `kind` | Required. `errors` retries genes whose annotation failed for a transient reason; `no_result` re-checks genes on the No result tab that have no error. |
| `inputs` | Required, 1 to `ANNOTATE_MAX_INPUTS` entries. Plain genes (structured inputs or strings, as in `POST /v1/annotate/jobs`), each naming one row by its `gene` or by a spelling it was resolved from (an alias, Ensembl ID or RefSeq accession in its `resolved_inputs`), with the tumor type it was annotated under. |
| `force_refresh` | Optional. Defaults to `true` for `no_result` (a cached result would just repeat) and `false` for `errors`. |

Every input must be a plain gene whose row on this run is currently eligible
for `kind`. If any input is a fusion, carries breakpoint fields, was rejected
permanently (an invalid accession, an unresolvable symbol, or a transcript that
belongs to a different gene), matches no eligible row, or matches more than one
eligible row, the whole request returns `422` naming each problem, and nothing
is processed. Each accepted input is tied to the row it matched before the job
starts, and the result is written to that row whatever spelling it comes back
under. Inputs are
otherwise validated like a fresh annotate request, and the retry reuses the
saved run's `mode`, `local_backend`, and `skip_literature_for_oncokb`.

The job runs in the background and is polled through the returned `status_url`
with `GET /v1/annotate/jobs/{job_id}`. When the job is `complete`,
`result.run_id` is the original run id and its `annotations` contain the
retried genes as the pipeline returned them.

A result is written to a row only when its gene is the row's gene. If an input
now resolves to a different gene (for example an alias that has since moved),
the row is left unchanged, no row is added for the other gene, and the job
still completes with the other genes applied. Each such input is listed in the
job's `retry_outcomes`:

```json
"retry_outcomes": [
  {
    "input": "CD246",
    "row_gene": "ALK",
    "resolved_gene": "LTK",
    "message": "CD246 now resolves to LTK, not ALK; ALK was not updated"
  }
]
```

A row whose input never resolved to a gene (its `gene` is the raw input,
because the symbol lookup failed) has no gene yet, so its first successful
resolution to a non-empty gene is written. A result with an empty or blank
`gene` is never written to any row; its input is listed in `retry_outcomes`
with `resolved_gene: ""` and a message such as
`"ENSG00000157764 did not resolve to a gene; not updated"`.

Results are written to the stored run only when the whole job completes, in the
same locked transaction other run updates (such as fusion-evidence retries)
use, so concurrent updates don't overwrite each other; a `failed` job leaves the
run unchanged. Only the rows of the submitted genes that are still eligible are
written, and only with a result at least as good (an error < no result < a
grounded annotation); no other gene's row is ever changed, and results for genes
that weren't submitted are discarded. An updated row keeps its earlier
`resolved_inputs` alongside the retry's. Existing warnings are kept and new
warnings about the updated genes are added. `genes_annotated` is recomputed.

Unknown runs return `404`, non-owners return `403`, unauthenticated requests
return `401`, and run storage that isn't configured returns `503`.
