# OpenEvidence API Assessment: Cancer-Gene Literature Synthesis

**Prepared for:** OpenEvidence
**Context:** We are evaluating the OpenEvidence API (`/streaming/analysis` endpoint, `darwin` model) as a supplementary evidence source in an automated cancer-gene literature synthesis pipeline. This document reports our findings — benefits, costs, and one specific citation-grounding issue — from two controlled benchmark runs, computed directly from the raw API response data we captured.

**Data and methodology.** Two paired benchmark runs, each comparing a "disabled" arm (our pipeline without any OpenEvidence call) against an "enabled" arm (identical pipeline, with one OpenEvidence call added per gene), across the same 16 genes (`TP53, KRAS, EGFR, BRAF, BRCA1, ALK, ACACB, AIRE, ANKRD13A, CRACD, DENND2C, FAM117A, RFX7, RP1, TRARG1, CLCN3P1`):

- **Run A ("generic phrasing"):** open-ended question — *"What does the peer-reviewed evidence show about GENE's role in cancer? Summarize the key clinical and molecular evidence."*
- **Run B ("pointed phrasing"):** closed, classification-style question — *"Based on peer-reviewed evidence, is GENE an oncogene or tumor suppressor in cancer? State the classification and the strongest supporting evidence."* — plus a fusion-specific variant, *"Based on peer-reviewed evidence, is the EML4::ALK fusion oncogenic in cancer? State the classification and the strongest supporting evidence,"* run once against the `EML4::ALK` fusion pair.

All figures below were computed fresh from the raw JSON artifacts of these two runs (per-gene disabled/enabled comparison records, and the full captured `openevidence_analysis` object — `question`, `text`, and the deduplicated `citations` list — for every gene in both runs), not copied from any earlier internal write-up. Where we found a discrepancy between two earlier internal analyses of the same data, it's called out explicitly below rather than silently resolved.

---

## 1. How we're using the API today

OpenEvidence is called once per gene (or once per fusion pair, for fusion-aware questions) with a single closed, targeted natural-language question against the `/streaming/analysis` endpoint (`darwin` model). The accumulated streamed text and the deduplicated `citation_key`-indexed citation list from the response are attached to our synthesis prompt as a clearly labeled **"supplementary, unverified evidence"** block — never merged into our own verified-citation set. Concretely: OpenEvidence's citations are not cross-checked against PubMed and are never substituted for our own PubMed-sourced, abstract-verified citations. OpenEvidence is off by default in our system and is being evaluated as an optional, additive input alongside — not a replacement for — our own PubMed-based retrieval and citation-verification pipeline.

We recently narrowed the question phrasing from an open-ended literature summary ("what does the evidence show") to a closed classification question ("is this an oncogene or tumor suppressor," or "is this fusion oncogenic") to test whether a more targeted prompt would reduce latency and token/citation overhead. Findings from that comparison are below.

---

## 2. Concrete benefits observed

### 2.1 Coverage beyond PubMed: NCCN guideline citations

Because our own retrieval is PubMed-only, it structurally cannot surface non-journal sources such as clinical practice guidelines. OpenEvidence's citation set can. In the generic-phrasing run, OpenEvidence's `ALK` response (`citation_key: "15"`) cited:

> **Title:** "Non-Small Cell Lung Cancer"
> **Authors:** National Comprehensive Cancer Network
> **URL:** `https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf#page=53`

directly supporting the claim: *"The NCCN algorithm lists alectinib, brigatinib, ensartinib, and lorlatinib as preferred first-line options (all category 1), with ceritinib and crizotinib designated useful in certain circumstances (also category 1)."* This is not an isolated case — across the 16-gene generic-phrasing run, 10 of 16 genes' OpenEvidence responses included at least one NCCN guideline citation (`AIRE`, `ALK`, `BRAF`, `BRCA1`, `EGFR`, `KRAS`, `RFX7`, `RP1`, `TP53`, `TRARG1`). This is a genuine, verifiable capability gap our own PubMed-only pipeline cannot close on its own.

### 2.2 Fusion-specific question path: EML4::ALK qualitative case

We ran the fusion-aware question against the `EML4::ALK` fusion pair. OpenEvidence's response for `EML4` (paired with `ALK` in the same fusion-question call) stated the ALEX trial result as:

> *"ALEX (alectinib vs crizotinib, treatment-naive): median PFS 34.8 vs 10.9 months (HR 0.43, 95% CI 0.32–0.58); 5-year OS 62.5% vs 45.5%; final analysis median OS 81.1 vs 54.2 months (HR 0.78, 95% CI 0.56–1.08)."*

attributed to `citation_key: "13"` and `"15"`:

- `13`: "Updated Overall Survival and Final Progression-Free Survival Data for Patients With Treatment-Naive Advanced ALK-positive Non-Small-Cell Lung Cancer in the ALEX Study," Mok T, Camidge DR, Gadgeel SM, et al., *Annals of Oncology*, 2020 (DOI `10.1016/j.annonc.2020.04.478`) — the ALEX study's updated PFS / interim OS analysis.
- `15`: "Alectinib Versus Crizotinib in Previously Untreated ALK-positive Advanced Non-Small Cell Lung Cancer: Final Overall Survival Analysis of the Phase III ALEX Study," Peters S, Camidge R, Dziadziuszko R, et al., *Annals of Oncology*, 2025 (DOI `10.1016/j.annonc.2025.09.018`) — the ALEX study's final OS analysis.

  <sup>†</sup> Citation 15's `title` field as returned by the API is literally `"\zaAlectinib Versus Crizotinib..."` — a stray `\za` prefix (an apparent encoding artifact) ahead of the real title. We've cleaned it above for readability; flagging that normalization here rather than silently reproducing or silently hiding it.

Both are genuine ALEX trial publications — one the updated PFS/interim-OS analysis, the other the final OS analysis — and the 34.8 vs. 10.9 month PFS figure matches the trial's well-known result. This is a well-grounded, correctly attributed statistic — the kind of result we want from this integration. The fusion-specific question path also completed successfully end to end: 2 genes annotated, 1 fusion processed, no errors.

### 2.3 No regression in our own citation grounding under the pointed phrasing

Not a claim about OpenEvidence's own citations, but a relevant "no side effects" finding: a prior internal review had flagged a case where our own pipeline (independent of OpenEvidence) attached an incorrect PubMed citation to a BRCA1 risk claim. We re-checked `BRCA1` in the pointed-phrasing run: comparing the disabled-arm and enabled-arm citation sets for this gene shows the previously mismatched PMID (`41498327`) is no longer among the four retained citations in the enabled arm — it was replaced by `41359628` — and all four retained citations (`42323326`, `40202107`, `39265497`, `41359628`) now have a matching `supporting_quotes` entry whose text checks out near-verbatim against the underlying abstract (choline/FAM3C metastasis; promoter-methylation prognosis; GPX4-mediated ferroptosis resistance; K11-ubiquitination/PARPi sensitivity, respectively). This shows the pointed-question integration didn't destabilize our downstream citation pipeline for this gene — though as section 4 shows, it did not eliminate citation-grounding risk in general.

---

## 3. Concrete costs and tradeoffs

### 3.1 Latency

| | Generic phrasing | Pointed phrasing |
|---|---|---|
| Per-call OpenEvidence response time (n=16) — mean | 184.9 s | 133.0 s |
| — median | 191.8 s | 126.1 s |
| — min / max | 93.6 s / 255.9 s | 90.6 s / 198.6 s |
| Total wall-clock, OpenEvidence disabled (16 genes, concurrency=3) | 98.9 s | 98.9 s |
| Total wall-clock, OpenEvidence enabled (16 genes, concurrency=3) | 1,159.2 s | 861.0 s |
| **Wall-clock multiplier vs. not calling OpenEvidence** | **11.7×** | **8.7×** |

Pointed phrasing reduced mean per-call latency by ~28% (184.9s → 133.0s) and cut the wall-clock multiplier from 11.7× to 8.7×. That is a real, meaningful improvement — but the integration still multiplies total pipeline wall time by nearly an order of magnitude, and every one of the 32 calls across both runs took at least 90 seconds.

The single fusion-pair call (`EML4::ALK`) took 151.4 s of a 164.7 s total pipeline run for that fusion — 92% of wall time was the OpenEvidence call itself.

### 3.2 Token / prompt cost

We measured the size of the downstream synthesis prompt (our own LLM call that consumes OpenEvidence's output) with OpenEvidence disabled vs. enabled, summing `synthesis` call `total_input_tokens` (a run's declared input token count, inclusive of cache-write and cache-read tokens) across all 16 genes:

| | Generic phrasing | Pointed phrasing |
|---|---|---|
| Synthesis input tokens, disabled | 83,337 | 82,787 |
| Synthesis input tokens, enabled | 150,559 | 152,131 |
| **Increase** | **+80.7%** | **+83.8%** |

Narrowing the question did **not** reduce prompt cost — if anything it was marginally worse (83.8% vs. 80.7%). This is despite OpenEvidence's own response prose getting substantially shorter under pointed phrasing (mean 7,860 → 4,716 characters) and returning fewer citations per gene (mean 29.9 → 19.5). The prompt-token overhead is apparently not primarily driven by response length or citation count in a way that scales down with either.

*(Prose-length note: the 7,860 → 4,716 character figures above are measured after stripping an internal `REACTCOMPONENT!:!InlineGenerationStep!:!{...}` serialization prefix that is present verbatim at the start of every response's `text` field in our captured data — it looks like client-side rendering metadata for a "steps" UI, not part of the analysis prose itself. The `text` field as stored, without stripping that prefix, is longer: mean 9,045 → 5,609 characters. We're disclosing this so a reader diffing against the raw API response doesn't see an apparent discrepancy.)*

**Methodology-sensitivity caveat.** This is the figure where two earlier internal analyses of this same data disagreed by roughly 8–9 percentage points, and we want to be transparent about why rather than pick a number silently. The disagreement traces to whether `cache_creation_input_tokens` are included in "input tokens." Our primary number above (`total_input_tokens`, which includes cache-write tokens — the field our LLM provider bills against) shows **+80.7%** for generic phrasing. If instead you sum only the raw `input_tokens` field (excluding cache-write tokens, which is a legitimate methodology if you're trying to isolate genuinely "new" tokens per call rather than total billed input), the same generic-phrasing comparison shows **+89.3%** (78,093 → 147,816) — an 8.6-percentage-point gap driven by a small number of genes with non-trivial prompt-caching activity. For pointed phrasing the two methodologies are closer (82.0% vs. 83.8%, a 1.8-point gap) because less caching activity occurred in that run. We report `total_input_tokens` as primary since it reflects total billed input, but flag this sensitivity explicitly.

### 3.3 Citation yield

"Citation yield" here means our own verified PubMed citations retained in the final synthesis output (never OpenEvidence's own citations, which are kept separate and unverified) — i.e., whether having OpenEvidence's supplementary evidence available changes how many verified citations our own pipeline ends up retaining.

| | Generic phrasing | Pointed phrasing |
|---|---|---|
| Verified citations, disabled (sum, 16 genes) | 47 | 49 |
| Verified citations, enabled (sum, 16 genes) | 48 | 49 |
| **Net gain from enabling OpenEvidence** | **+1** | **+0** |

The net gain shrank from +1 to +0 between the two phrasings — enabling OpenEvidence produced essentially no improvement in our own verified-citation count in either case, and the pointed phrasing did not improve this. (Per-gene detail, generic phrasing: 12 of 16 genes unchanged, `AIRE`/`ANKRD13A`/`RFX7` +1 each, `DENND2C` −2.)

---

## 4. Citation-grounding case study: a resistance-mechanism attribution mismatch (pointed run, `ALK`)

This is the most important finding in this report. It is offered as a specific, reproducible product-quality observation for your team to inspect directly, not as a general accusation about response quality.

**Setup.** Pointed-phrasing run, gene `ALK`, question: *"Based on peer-reviewed evidence, is ALK an oncogene or tumor suppressor in cancer? State the classification and the strongest supporting evidence."*

**The claim, verbatim from the response `text`:**

> *"4. On-target resistance closes the causal loop. Relapse is driven overwhelmingly by secondary mutations within the ALK kinase domain itself — L1196M, G1269A, and the solvent-front G1202R (25–43% after second-generation TKIs), plus compound mutations after lorlatinib — and next-generation inhibitors designed against those specific residues restore response.[[1]][[12]][[13]][[14]]"*

Four citation keys are attached to this sentence. We focus on `citation_key: "13"`:

```
"citation_key": "13",
"title": "Third-generation EGFR and ALK inhibitors: mechanisms of resistance and management",
"authors": "Cooper AJ, Sequist LV, Lin JJ.",
"journal": "Nature Reviews. Clinical Oncology",
"date": "2022-08-01",
"doi": "10.1038/s41571-022-00639-9",
"url": "https://doi.org/10.1038/s41571-022-00639-9",
"source_texts": []
```

**The mismatch.** The claim asserts that ALK-TKI relapse is *"driven overwhelmingly"* by on-target (kinase-domain) secondary mutations — but this is contradicted on three independent grounds:

1. **Internally, by the response's own embedded statistic.** The parenthetical the response itself supplies — "25–43% after second-generation TKIs" — describes a minority, not an overwhelming majority, of resistance cases. A figure of 25–43% cannot simultaneously support the headline claim of "overwhelming" dominance; the sentence contradicts itself.
2. **Externally, against what citation 13 actually says — with real numbers, not just directional disagreement.** Cooper, Sequist & Lin (*Nat Rev Clin Oncol* 2022, PMID `35534623`) is the field's standard reference specifically on third-generation TKI resistance mechanisms, and its scope explicitly covers **both** on-target *and* off-target (bypass-pathway) resistance mechanisms for lorlatinib — which the OpenEvidence claim itself names in the same sentence ("plus compound mutations after lorlatinib"). Per that paper's own reported data, on-target ALK-kinase-domain mutations account for roughly **50–60% of resistance after second-generation TKIs** (alectinib, brigatinib, ceritinib) — but only about **25–30% of resistance after lorlatinib**, where the majority of cases are off-target: MET amplification (reported in roughly 22% of lorlatinib-relapse samples), RET rearrangements, RAS–MAPK pathway reactivation, EMT, and small-cell transformation. So for lorlatinib specifically — the drug the OpenEvidence claim explicitly names — the source it cites reports on-target mutations as a *minority* mechanism, the opposite of "driven overwhelmingly by secondary mutations within the ALK kinase domain itself."
3. **The response's own cited number doesn't match its own source, either.** Even setting aside the lorlatinib-specific figures, the response's "25–43% after second-generation TKIs" doesn't reconcile with Cooper et al.'s actual second-generation-TKI figure of ~50–60%. That's a sharper version of the same finding: not only does the *prose claim* misrepresent what citation 13 says, the *specific statistic attached to it* doesn't match that source's real reported number either — on a paper that, per point 2, was well within reach to check for the correct figure in the first place.

**What we could not verify.** `source_texts` — the field intended to carry the underlying source excerpt an OpenEvidence citation is grounded in (`reference.source_texts` in the streaming event, alongside `reference.reference_detail`) — was empty (`[]`) for this citation, and in fact for **every one of the 791 citations** we captured across both full benchmark runs (479 in the generic run, 312 in the pointed run). We could not use `source_texts` to check the claim against the exact underlying passage OpenEvidence intended to cite, because it was never populated in either run. This is itself worth flagging separately (see §5) — it means citation-key-to-claim grounding currently has to be checked by inspecting the cited work directly, rather than by comparing against a source excerpt the API provides inline.

**Why this matters and how it relates to our earlier finding.** This is a different failure mode from the one we flagged in our first benchmark of this integration, where a specific numeric statistic (a PFS/DFS figure) leaked from OpenEvidence's supplementary text into an unrelated citation in our *own* downstream synthesis. That specific pattern did not recur here. What we're reporting instead is narrower and lives entirely inside OpenEvidence's own response: a claim attached to a citation whose actual subject matter argues against the claim's own headline framing, in a response that is otherwise well-organized, correctly formatted, and (per §2) frequently well-grounded. We'd welcome the chance to walk through this specific example with your team — the underlying question of "how does one attribute a summary claim to the correct one of several inline citations for a topic with genuine scientific nuance" seems like exactly the kind of case that's actionable to trace through your generation pipeline.

---

## 5. Access and product constraints relevant to adoption

- **Provisioning.** Access requires org-provisioned API credentials via an Order Form; we found no published self-serve signup path, no published pricing, and no published rate limits. We are proceeding without confirmed pricing or rate-limit information.
- **PHI.** We have not tested this integration with any PHI, and we could not find published documentation of your PHI handling policy, BAA availability, or any PHI-specific restrictions on this API. **This is flagged as an open question for your team, not a confirmed constraint** — we'd appreciate clarification before considering any use case involving patient-identifiable data.
- **No PMID field on citations.** Every citation in both benchmark runs carried `citation_key` (an integer scoped to a single response, not a stable cross-response identifier), and — nested under `reference.reference_detail` — `title`, `authors_string`, `journal_name`/`journal_short_name`, `publication_date`, `doi`, and `url`. None carried a PMID field. In our data, only 38.4% (generic run) / 42.3% (pointed run) of citations happened to have a `pubmed.ncbi.nlm.nih.gov` URL from which a PMID could be parsed as a side effect; the remaining ~58–62% resolved to DOI-resolver links, NCCN guideline PDFs, or other non-PubMed URLs with no PMID recoverable at all. Any downstream consumer that needs to cross-reference OpenEvidence citations against a PubMed-based system (as we do) has to build and maintain its own DOI/title-based PMID-resolution layer; there is currently no reliable way to do this from the API response alone.
- **`source_texts` was empty in 100% of our sample.** As noted in §4, `reference.source_texts` — the field that would let a caller verify a citation against its underlying grounding excerpt without re-fetching the source — was empty for all 791 citations across both runs. We don't know whether this reflects how the field is populated for this endpoint/model in general, a characteristic of the questions we asked, or something specific to our account/integration; we'd like to understand whether this is expected.
- **Real observed latency.** Across 32 live calls (16 genes × 2 phrasings), per-call response time ranged 90.6–255.9 seconds, with several calls exceeding 200 seconds. Mean latency was 184.9s (generic phrasing) and 133.0s (pointed phrasing). This makes the endpoint unsuitable for any synchronous/interactive use case as currently observed, and requires generous timeouts (we use 60s as a floor, with actual waits regularly 2–4× that) in any batch/async integration.

---

## 6. Asks for the OpenEvidence team

1. **A native PMID field on citations** (in addition to, not instead of, DOI/`citation_key`), so downstream systems that key off PMID — which includes any PubMed-based literature pipeline like ours — don't need to build a separate resolution layer, and don't silently lose ~60% of citations to unresolvable URLs.
2. **Published self-serve pricing and rate limits**, even indicative ranges, so integration cost and scaling can be evaluated before an Order Form conversation.
3. **Guidance on typical/expected latency for the `/streaming/analysis` endpoint**, and whether a faster model tier exists for use cases (like ours) that don't need `darwin`'s full depth — our observed 90–256 second range makes latency the single largest integration cost we measured.
4. **Clarification on `reference.source_texts`** — whether it's expected to be populated for the endpoint/model/question types we're using, since it was empty in 100% of the citations we captured (791/791 across two full benchmark runs).
5. **Openness to reviewing the citation-grounding case study in §4** (`ALK`, pointed-phrasing run, `citation_key: "13"`, the on-target-resistance-mechanism claim) — we think this is a concrete, reproducible example your team could trace through your own generation and citation-attribution pipeline.

---

## 7. Bottom line

Latency improved meaningfully with pointed, closed-question phrasing — mean per-call response time dropped from 184.9s to 133.0s, and the wall-clock multiplier from calling OpenEvidence fell from 11.7× to 8.7× versus not using it at all. But neither token cost nor citation yield improved: synthesis prompt cost still rose ~81–84% regardless of phrasing, and the net gain in our own verified citations from enabling OpenEvidence dropped from a marginal +1 to +0. The citation-grounding problem we flagged in our original benchmark of this integration did not disappear with the pointed phrasing — it relocated to a different, still-real citation-accuracy issue (§4), specific to OpenEvidence's own response and citation attribution rather than the numeric-hallucination pattern we saw before. On the current evidence, this does not change our earlier recommendation: keep this integration off by default, opt-in only, and continue treating its output as clearly labeled, unverified supplementary evidence rather than a citation source we merge with our own.
