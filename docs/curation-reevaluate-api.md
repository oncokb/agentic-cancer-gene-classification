# Curator re-evaluation API (PR 2)

This API returns suggestions for editing. It never saves a run or a gene annotation.
Both routes currently use `require_auth`; the marked `_curator_dependency` must be
swapped to `require_curator` when the curator-overrides PR merges.

## Create and poll

`POST /v1/curation/reevaluate/jobs` returns HTTP 200 immediately:

```json
{
  "subject_type": "gene",
  "subject_key": "ALK",
  "tumor_type": "LUAD",
  "evidence_pmids": ["123", "456"],
  "precedent_pmids": ["789"]
}
```

For fusion drafts, use `"subject_type": "fusion"` and
`"subject_key": "EML4::ALK"` (also accepts `--` or `/` notation).
Fusion input partners are uppercased for lookup through HGNC/Ensembl, then
returned with canonical spelling/case preserved (for example, `C11orf95::RELA`). `tumor_type` is optional/null. Both PMID arrays default to `[]`; at least one
array must be nonempty. Precedent-only requests skip synthesis and return
`insufficient_evidence: true`, empty `citations`/`model_citations`/cards/quotes,
and a null synthesis model with a suggested precedent block.

`CURATION_REEVALUATE_MAX_PMIDS` defaults to **50**: each array and the combined
unique PMID count must fit this cap, or POST returns **422** (standard FastAPI
validation detail). Both copies of an ID shared across arrays count once toward
the combined limit. Per-list and combined limits read the live settings on
each validation; they are not frozen when the module is imported.
`CURATION_REEVALUATE_MAX_INFLIGHT_JOBS` defaults to **2**:
queued/running jobs count toward the normalized owner email's limit, or POST
returns **429**:

```json
{"detail": "Too many in-flight re-evaluation jobs"}
```

Admission and counting occur under one lock. Finished jobs release their slot.
GET is restricted to the job owner; another user's GET returns **404** with
`{"detail": "Job not found"}`. Admins may read any job. Owners are stored internally
and excluded from JSON. Start/success/failure actions are logged against the owner.
Terminal status is stored before auditing; audit failures are logged and do not
change job results or retain an in-flight slot.

PMIDs must be positive numeric **strings**, unique
within each array. An ID can occur in both arrays. Order is preserved in each
output block. Invalid request shape returns HTTP 422; valid-shaped requests
with unknown/retracted/no-abstract papers create a job that fails validation.

```json
{
  "job_id": "5c08d0ca-5648-4cf4-97cc-b3477f14887d",
  "status_url": "/v1/curation/reevaluate/jobs/5c08d0ca-5648-4cf4-97cc-b3477f14887d"
}
```

`GET {status_url}` returns HTTP 200 with exactly these envelope fields.
Poll until `succeeded` or `failed`; `queued` and `running` are intermediate:

```json
{"status": "queued", "error": null, "invalid_pmids": null, "draft": null}
```

```json
{"status": "running", "error": null, "invalid_pmids": null, "draft": null}
```

Validation failure (a paper can appear in both retracted and no_abstract):

```json
{
  "status": "failed",
  "error": "Curator PMIDs failed validation",
  "invalid_pmids": {"unknown": ["123"], "retracted": ["456"], "no_abstract": ["789"]},
  "draft": null
}
```

Missing IDs are checked with an **uncached**, retraction-inclusive PubMed
esearch. If that check confirms they do not exist, the job returns
`invalid_pmids.unknown`, including when every supplied ID is unknown. If efetch
returned no records but esearch finds supplied IDs, the job instead fails with
`invalid_pmids: null` and `error: "NCBI returned no verifiable PMIDs; please retry."`.
This avoids classifying a cached empty fetch after an XML parse error as invalid
literature. Network exceptions remain generic job failures.

An actual unresolved subject returns
`"Could not resolve subject: <input>. Check the gene symbol and retry."`
(or `"Check the fusion partner symbols and retry."` for a fusion).
A reported HGNC/Ensembl lookup outage instead returns
`"Gene lookup is temporarily unavailable; please retry."` with `invalid_pmids: null`.

Other failures return `"error": "Re-evaluation failed; please retry."` with
`invalid_pmids` and `draft` null. Cancellation returns `"error": "Job cancelled"`.
Unknown/expired IDs return HTTP 404 `{"detail": "Job not found"}`. Standard
application authentication errors apply to both endpoints.

## Successful draft

```json
{
  "status": "succeeded",
  "error": null,
  "invalid_pmids": null,
  "draft": {
    "gene": "ALK",
    "cancer_association_rationale": "Suggested rationale from synthesis.",
    "citations": ["123", "456"],
    "model_citations": ["123"],
    "evidence_cards": [
      {
        "pmid": "123", "title": "Paper title", "journal": "Journal",
        "publication_types": [], "is_preprint": false, "evidence_type": "clinical",
        "selected_reason": "Suggested description for curator editing.",
        "quote": "Exact sentence from abstract.", "abstract": "Exact sentence from abstract. More text."
      },
      {
        "pmid": "456", "title": "Second paper", "journal": "",
        "publication_types": [], "is_preprint": false, "evidence_type": "other",
        "selected_reason": "Suggested description for curator editing.",
        "quote": "Second abstract sentence.", "abstract": "Second abstract sentence."
      }
    ],
    "supporting_quotes": [
      {"pmid": "123", "quote": "Exact sentence from abstract."},
      {"pmid": "456", "quote": "Second abstract sentence."}
    ],
    "clinical_actionability": {
      "confidence_score": 1.0, "confidence_level": "high",
      "summary": "Suggested summary of curator-selected precedents.",
      "confidence_explanation": "Curator-supplied PMIDs: threshold bypassed. Confidence denotes curator selection, not a calibrated evidence assessment.",
      "pmids": ["789"], "score_components": [],
      "evidence": [{
        "pmid": "789", "title": "Precedent paper", "evidence_type": "clinical",
        "therapies": ["imatinib"], "domains": ["kinase"], "matched_terms": [],
        "quote": "Verbatim precedent quote.", "abstract": "Verbatim precedent quote."
      }]
    },
    "quality_flags": [{
      "code": "curator_pinned_draft", "label": "Curator-selected draft", "severity": "info",
      "detail": "All supplied evidence PMIDs are included in curator order. Inclusion does not establish relevance; review this unpersisted suggestion. Evidence support is computed from model_citations, not the pinned citation list."
    }],
    "model_info": {
      "synthesis_model": "claude-haiku-4-5-20251001",
      "synthesis_escalated": false,
      "suggestion_model": "claude-haiku-4-5-20251001"
    },
    "generated_at": "2026-10-12T18:00:00+00:00"
  }
}
```

Complete serialized gene and fusion request/response JSON examples (using illustrative mocked literature) are in [curation-reevaluate-examples.json](curation-reevaluate-examples.json).

The example above shows the UI-editable fields. A gene draft also contains all ordinary
`GeneAnnotation` fields (classification, evidence-support metadata, etc.).
`model_citations` preserves the synthesis model's verified citation list, which
may be smaller than the pinned `citations`. Evidence-support scores and standard
quality flags use **model_citations**, not the pinned list. The pinned-draft flag
explains this difference.

The ordinary deterministic `ClinicalActionability` assessment produced by the
annotation builder is **replaced** by the curator precedent suggestion.
`clinical_actionability` is **null** when no precedent PMIDs were supplied,
even if the ordinary assessment would have produced a block. Precedent confidence
marks curator selection rather than an independent assessment.
`quality_flags` can contain additional standard synthesis flags. Model names
reflect configured synthesis/escalation models. `synthesis_model` is null for
precedent-only requests; `suggestion_model` identifies the supplemental model
attempted for every valid draft. Every evidence PMID has exactly one
card and supporting quote, including papers the synthesis model did not cite.
Every quote is a substring of the respective abstract. Candidates are tried
in order (synthesis, then supplemental suggestion) and must be 20–400 characters;
Escaped model quotes are HTML-unescaped and checked against the original abstract.
Otherwise the verbatim first sentence is used, bounded to 400 characters at a
word boundary (a single word longer than 400 characters uses the hard prefix limit). Selected reasons are strings capped
at 600 characters; invalid LLM evidence types fall back to the deterministic
record classifier. Precedent summaries are capped at 2,000 characters. Abstracts
in the supplemental prompt are escaped and fenced as untrusted data.

Fusion drafts have the same common fields and annotation metadata, replace
`gene` with `fusion`, and add the existing fusion-evidence fields:

```json
{
  "fusion": "EML4::ALK", "tumor_type": "LUAD", "well_supported": true,
  "retrieved_count": 2, "pmids": ["123", "456"],
  "interpretation": "Suggested rationale from synthesis."
}
```

Each fusion card additionally includes `fusion`, `matched_via_alias: false`,
and `alias_matches: []`. No exact-pair filter or card cap is applied to curator
PMIDs. Fusion precedent papers populate the same ClinicalActionability block;
partner precedent is kept separate from the exact-fusion evidence array.

## Operations and timing

Jobs use the same process-local memory/asyncio pattern as annotation jobs.
The in-flight limit and ownership store are process-local. Completed jobs are evicted on subsequent POSTs after
`settings.annotation_job_ttl_seconds`. Polling must reach the same worker;
restarting the worker loses its jobs. No database persistence is involved.

Plan for roughly **10–60 seconds** for a typical small PMID set on the Haiku
fast path, with longer times for Opus escalation, upstream retries, or large
sets. This is an engineering estimate, not a measured live-LLM benchmark.
The path fetches selected papers and resolves gene context, then runs synthesis
and one batched supplemental Haiku call; it does no literature search
or paper selection. Missing fetch results receive an ID-existence check to
distinguish no-abstract from unknown papers.
