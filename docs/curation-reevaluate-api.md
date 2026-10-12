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
`tumor_type` is optional/null. Both PMID arrays default to `[]`; at least one
array must be nonempty. PMIDs must be positive numeric **strings**, unique
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
      "detail": "All supplied evidence PMIDs are included in curator order. Inclusion does not establish relevance; review this unpersisted suggestion."
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
`clinical_actionability` is **null** when no precedent PMIDs were supplied.
`quality_flags` can contain additional standard synthesis flags. Model names
reflect configured synthesis/escalation models; `suggestion_model` is null
when no supplemental call was needed. Every evidence PMID has exactly one
card and supporting quote, including papers the synthesis model did not cite.
Every quote is a substring of the respective abstract.

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
Completed jobs are evicted on subsequent POSTs after
`settings.annotation_job_ttl_seconds`. Polling must reach the same worker;
restarting the worker loses its jobs. No database persistence is involved.

Plan for roughly **10–60 seconds** for a typical small PMID set on the Haiku
fast path, with longer times for Opus escalation, upstream retries, or large
sets. This is an engineering estimate, not a measured live-LLM benchmark.
The path fetches selected papers and resolves gene context, then runs synthesis
and at most one batched supplemental Haiku call; it does no literature search
or paper selection. Missing fetch results receive an ID-existence check to
distinguish no-abstract from unknown papers.
