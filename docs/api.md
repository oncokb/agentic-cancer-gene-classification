# Using the ACGC API from scripts

This guide is for MSK staff who want to query ACGC (Agentic Cancer Gene
Classification) from a script or pipeline. It covers getting an API key,
asking ACGC about genes, and reading the results.

What you get back for each gene is its classification and rationale: whether
it's cancer-associated, its gene class, a short rationale with supporting
PubMed IDs, and a support score. Every response also includes a `view_url`
link that opens the full report in the ACGC web app (evidence cards,
abstracts, quotes, clinical actionability) for anyone who signs in.

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
  [Speed and freshness](#5-speed-and-freshness).

Accepted gene identifiers:

- HGNC symbols such as `ALK`, `HLA-A` or `C1orf112`, and Ensembl gene IDs such
  as `ENSG00000141510` or `ENSG00000141510.17`.
- Letters, digits, `-` and `.` only, starting and ending with a letter or
  digit.
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
  ]
}
```

`GET /v1/genes/{symbol}` returns the same shape, with one entry in `results`.

| Field | Meaning |
| --- | --- |
| `run_id` | ID of the saved run that holds these results. |
| `view_url` | Link to the full report in the ACGC web app. Whoever opens it signs in first if they aren't already, then lands on the report. |
| `gene` | The gene symbol ACGC classified. This can differ from what you sent, for example when an Ensembl ID or an old alias resolves to the current symbol. |
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
| `error` | Set only when this gene couldn't be classified. See [Errors](#6-errors). |

The API deliberately leaves out abstracts, quotes, evidence cards and
OpenEvidence content. Open `view_url` to see them.

## 4. Background jobs

Start a job with the same body as `POST /v1/genes/query`:

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
  "error": null
}
```

- `status` is `queued`, `running`, `complete` or `failed`.
- `genes_completed` and `genes_total` show progress. `results` fills in as
  genes finish.
- `run_id` and `view_url` are set only once `status` is `complete`.
- If `status` is `failed`, `error` holds a short message. Start a new job to
  retry.

## 5. Speed and freshness

- ACGC saves each gene's result and reuses it while it's still fresh, so asking
  about a gene someone has already looked at is usually quick. Each result says
  whether it was reused (`cache_status`) and when it was produced
  (`cached_at`).
- A gene that hasn't been classified recently is classified from scratch. That
  involves PubMed searches and AI model calls, and typically takes 10 to 40
  seconds per gene.
- `force_refresh: true` (or `?force_refresh=true` on the `GET`) ignores saved
  results and classifies every gene again. It's slower and costs more, so use
  it only when you need a fresh answer.
- For more than a handful of uncached genes, use a background job rather than
  holding one request open.

## 6. Errors

### Whole-request errors

| Status | Meaning | What to do |
| --- | --- | --- |
| `401` | No key, or the key is wrong, revoked or expired, or its owner is no longer allowed to use ACGC. | Check `ACGC_API_KEY`, or create a new key. |
| `403` | Your account is waiting for administrator approval, or a key was used to manage keys. | Contact the ACGC team, or manage keys on the API keys page. |
| `404` | `GET /v1/genes/{symbol}` only: the gene doesn't exist (HGNC or Ensembl confirmed no match). | Check the symbol. |
| `422` | The request is malformed: a bad gene symbol, a fusion, more than 50 genes, or invalid JSON. | Fix the request. The response says which field is wrong. |
| `429` | You exceeded your key's rate limit. | Wait for the number of seconds in `Retry-After`, then retry. |
| `500` | ACGC couldn't save the run. No `view_url` is returned, because it wouldn't open. | Retry later. |
| `503` | A gene lookup service (HGNC or Ensembl) was temporarily unavailable, or ACGC can't verify keys right now. | Retry after a short wait. |

### Per-gene errors in batch requests

In `POST /v1/genes/query` and in jobs, a problem with one gene doesn't fail
the whole request. That gene's entry has `error` set instead:

| `error` | Meaning |
| --- | --- |
| `Gene symbol could not be resolved.` | The gene doesn't exist. Check the symbol. |
| `Gene symbol lookup is temporarily unavailable; please retry.` | A lookup service was down. Retry that gene. |

The run is still saved, and `view_url` still works for the genes that
succeeded.

## 7. Python example

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

## 8. More detail

- Every endpoint and field is also listed in the interactive API reference at
  <https://acgc.oncokb.org/docs>.
- Operator details, such as configuration settings, rate-limit storage and
  logging, are in the README sections
  [ACGC API Keys (for scripts)](../README.md#acgc-api-keys-for-scripts) and
  [Gene query API](../README.md#gene-query-api).
- Questions or problems: contact the ACGC team.
