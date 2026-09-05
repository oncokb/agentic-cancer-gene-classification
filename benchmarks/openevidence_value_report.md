# OpenEvidence live value benchmark

**Verdict: no demonstrated quality gain large enough to justify blanket enablement in this sample.**

## Results

All 32 annotations completed without errors. All 16 enabled genes received nonempty live OpenEvidence analyses. The runs began at 2026-09-05T01:16:20.055512+00:00 (disabled) and 2026-09-05T01:18:08.575904+00:00 (enabled), on September 4 local time.

- Final verified citations (summed per gene): **47 → 48**. Supplementary OpenEvidence references: **479**, counted separately.
- Synthesis input tokens: **83,337 → 150,559**; output: **14,573 → 15,655**.
- All pipeline LLM input tokens: **123,685 → 193,562**; output: **16,535 → 17,647**.
- Whole-run wall time: **98.9 → 1159.2 seconds** (11.7×).
- Disabled per-gene latency: mean **16.5s**, median **15.6s**, range **10.3–31.6s**.
- Enabled per-gene latency: mean **199.8s**, median **209.1s**, range **107.9–275.2s**.
- Reference recall over 7 genes / 18 reference PMIDs: **7/18 (38.9%) → 8/18 (44.4%)** micro recall; macro recall **42.9% → 47.6%**.
- Cancer-association agreement: **14/16**. Exact summary wording changed for **16/16** genes.
- Retrieved PMID pools matched for **13/16** genes; selected PMID sets matched for **7/16**. These differences precede the OpenEvidence call and cannot be caused by its content.

### Citation and classification results

All arrows mean disabled → enabled. Recall is unavailable (—) where no nonempty holdout reference exists.

| Gene | Retrieval tier | Verified citations | Reference recall | OE references | Cancer-associated |
|---|---:|---:|---:|---:|---|
| ACACB | 1 → 1 | 4 → 4 | 0.0% → 0.0% | 29 | yes → yes |
| AIRE | 1 → 1 | 0 → 1 | 0.0% → 0.0% | 40 | no → yes |
| ALK | 1 → 1 | 4 → 4 | — → — | 36 | yes → yes |
| ANKRD13A | 1 → 1 | 3 → 4 | 0.0% → 33.3% | 7 | yes → yes |
| BRAF | 1 → 1 | 4 → 4 | — → — | 64 | yes → yes |
| BRCA1 | 1 → 1 | 4 → 4 | — → — | 46 | yes → yes |
| CLCN3P1 | 2 → 2 | 0 → 0 | — → — | 13 | no → no |
| CRACD | 1 → 1 | 4 → 4 | 100.0% → 100.0% | 9 | yes → yes |
| DENND2C | 2 → 2 | 2 → 0 | — → — | 19 | no → no |
| EGFR | 1 → 1 | 4 → 4 | — → — | 51 | yes → yes |
| FAM117A | 1 → 1 | 4 → 4 | 100.0% → 100.0% | 7 | yes → yes |
| KRAS | 1 → 1 | 4 → 4 | — → — | 53 | yes → yes |
| RFX7 | 1 → 1 | 3 → 4 | 0.0% → 0.0% | 12 | yes → yes |
| RP1 | 1 → 1 | 0 → 0 | — → — | 21 | no → no |
| TP53 | 1 → 1 | 4 → 4 | — → — | 51 | yes → yes |
| TRARG1 | 1 → 1 | 3 → 3 | 100.0% → 100.0% | 21 | yes → no |

### Token and latency costs

Each token cell is **input / output**, with cache reads/writes included in input. “All LLM” includes synthesis, selection, and agentic retrieval, but excludes OpenEvidence’s undisclosed internal tokens.

| Gene | Synthesis off | Synthesis on | All LLM off | All LLM on | Gene seconds off → on | OE seconds |
|---|---:|---:|---:|---:|---:|---:|
| ACACB | 4,223 / 989 | 9,168 / 1,061 | 6,635 / 1,044 | 11,580 / 1,126 | 14.7 → 211.9 | 195.9 |
| AIRE | 2,659 / 451 | 7,792 / 594 | 5,089 / 491 | 10,222 / 634 | 19.7 → 265.3 | 254.2 |
| ALK | 5,561 / 985 | 11,277 / 1,079 | 8,108 / 1,060 | 13,824 / 1,154 | 15.6 → 244.0 | 228.5 |
| ANKRD13A | 4,242 / 878 | 6,687 / 1,016 | 6,626 / 933 | 9,071 / 1,071 | 11.6 → 142.7 | 130.7 |
| BRAF | 6,190 / 1,006 | 12,200 / 964 | 8,645 / 1,081 | 14,655 / 1,039 | 18.0 → 237.1 | 222.8 |
| BRCA1 | 6,092 / 948 | 11,009 / 1,216 | 8,646 / 1,023 | 13,563 / 1,286 | 15.5 → 209.9 | 192.7 |
| CLCN3P1 | 2,393 / 164 | 4,225 / 366 | 7,563 / 727 | 9,405 / 926 | 13.9 → 107.9 | 93.6 |
| CRACD | 4,856 / 968 | 7,827 / 1,233 | 7,378 / 1,038 | 10,349 / 1,303 | 10.3 → 208.2 | 191.0 |
| DENND2C | 8,245 / 1,391 | 4,905 / 439 | 10,924 / 1,970 | 10,187 / 1,041 | 31.6 → 123.2 | 110.3 |
| EGFR | 6,916 / 1,205 | 12,802 / 1,235 | 9,526 / 1,280 | 15,412 / 1,310 | 14.1 → 256.1 | 239.0 |
| FAM117A | 4,339 / 943 | 6,641 / 983 | 4,339 / 943 | 6,641 / 983 | 11.6 → 157.8 | 145.6 |
| KRAS | 6,156 / 1,017 | 12,856 / 1,007 | 8,734 / 1,092 | 15,434 / 1,082 | 15.0 → 265.9 | 249.7 |
| RFX7 | 4,251 / 878 | 7,397 / 1,075 | 6,762 / 938 | 9,908 / 1,140 | 21.0 → 145.5 | 131.2 |
| RP1 | 7,263 / 725 | 15,898 / 918 | 9,777 / 765 | 18,412 / 958 | 16.5 → 179.8 | 165.1 |
| TP53 | 6,279 / 1,141 | 13,248 / 1,447 | 8,735 / 1,216 | 15,746 / 1,522 | 18.1 → 275.2 | 255.9 |
| TRARG1 | 3,672 / 884 | 6,627 / 1,022 | 6,198 / 934 | 9,153 / 1,072 | 16.5 → 166.4 | 151.6 |

Raw usage breakdown (ordinary input / cache creation / cache read): synthesis **78,093 / 2,622 / 2,622 → 147,816 / 2,743 / 0**.

## Did the changes add value?

This is a qualitative review of the paired outputs against their retrieved
abstracts and selected paper sets, not a blinded expert assessment or a formal
accuracy score. Full before/after summaries, rationales, gene-class labels,
pathway text, citation additions/removals, and Boolean changes are preserved in
[comparison.json](results/openevidence_live_20260904/comparison.json).

| Gene | Assessment of the enabled annotation relative to baseline |
|---|---|
| ACACB | Same four citations and association; more metabolic detail and more cautious clinical wording. Plausible prose improvement, no measured recall gain; selected papers differed. |
| AIRE | Cancer-association flip, false → true. Broader APECED-associated cancer susceptibility is potentially useful and agrees with the existing positive holdout label, but new thymoma details lack support in the selected abstract, and the enabled text overstates what that abstract establishes about causation. Mixed value. |
| ALK | Same four citations and selected papers; adds trial outcome numbers beside a PMID whose abstract does not contain those outcomes. A clear grounding concern rather than demonstrated quality gain. |
| ANKRD13A | Adds one holdout PMID (40975168) and retains the cancer association. The summary largely restates the same mechanisms, and the new PMID is not discussed in the summary. Small reference-recall gain with identical selected papers; ordinary synthesis variation remains an alternative explanation. |
| BRAF | Swaps one final citation and shifts tumor-frequency/prognosis detail. No classification gain or reference gold available; selection also differed. No demonstrated net improvement. |
| BRCA1 | Adds clinical risk percentages and treatment context; unchanged cancer association. The risk percentages are attached to a mechanistic ELF3 paper whose retrieved abstract does not report them. Grounding concern; selection also differed. |
| CLCN3P1 | Still negative, zero direct PubMed evidence, zero verified citations. Clearer distinction from the parent CLCN3 gene is useful caution, but 13 supplementary references did not establish a pseudogene-specific cancer association. |
| CRACD | Shifts from context-dependent roles to a more uniform tumor-suppressor account, omitting the opposing NSCLC proliferation finding (33006362), even though that paper was selected in both arms. Potential loss of nuance; selection differences limit attribution. |
| DENND2C | Still negative; two background/non-cancer citations disappear. The enabled retrieval/selection was already empty before OpenEvidence, so this cannot be credited to OpenEvidence citation filtering. Paralog-specific caution is useful but no direct cancer evidence was gained. |
| EGFR | Same citation count, different PMIDs; broader clinical context and outcome numbers replace some molecular detail. Selection differed; no demonstrated classification/recall benefit, and new clinical claims need claim-level sourcing. |
| FAM117A | Same four citations and perfect recall against its three holdout PMIDs. Summary emphasis changes but no measured evidence improvement. |
| KRAS | Same citation count; more prevalence/treatment framing. Broad wording about inhibitor classes in clinical use needs stronger support than the cited preclinical compound study (40780213). No classification benefit; selection differed. |
| RFX7 | One more citation, but no holdout-recall gain; retains the same tumor-suppressor association and adds detail about mouse lymphoma models. Possible enrichment, not a demonstrated accuracy gain. |
| RP1 | Both arms correctly distinguish the HGNC gene from the same-named oncolytic therapy and return no cancer association or citations. OpenEvidence's 21 supplementary references yield no measurable improvement. |
| TP53 | Same four final citations and cancer association. Added biological/clinical detail changes emphasis; no measured reference or classification improvement. |
| TRARG1 | Positive → negative despite identical selected papers and final citations. The enabled summary acknowledges functional HCC evidence but applies a stronger “validated cancer gene” standard. This conflicts with the holdout's positive label and looks like overcorrection for a broad cancer-association task. |

Two changes deserve particular attention:

- **New claims can acquire an unrelated verified PMID.** In ALK, the enabled
  summary attaches 7-year lorlatinib PFS of 55% and 3-year adjuvant alectinib DFS
  of 88.7% to [PMID 34175504](https://pubmed.ncbi.nlm.nih.gov/34175504/), a 2021
  review whose selected abstract discusses fusion variants, not those outcome
  numbers. Those numbers occur in the saved OpenEvidence text. This is evidence
  of supplementary content entering a final claim without adequate grounding
  in its attached PMID; it is not a finding that the numbers themselves are
  necessarily false. BRCA1 similarly attaches 72% breast/44% ovarian lifetime
  risks to [PMID 41498327](https://pubmed.ncbi.nlm.nih.gov/41498327/), whose
  abstract concerns ELF3, replication stress, and luminal progenitors rather
  than reporting those risk estimates. These examples show why a retained
  “verified” PMID does not establish claim-level correctness.
- **A changed classification is not automatically better.** AIRE's selected
  [abstract](https://pubmed.ncbi.nlm.nih.gov/30510552/) already links APECED to
  oral SCC, but explicitly leaves the causal role of chronic infection
  unresolved; the enabled summary speaks more definitively and adds a thymoma
  statistic without a matching selected source. Conversely, TRARG1's selected
  [HCC study](https://pubmed.ncbi.nlm.nih.gov/31157375/) reports differential
  expression, prognosis associations, and opposing malignant phenotypes after
  miR-484/TUSC5 manipulation. The enabled negative classification appears to
  demand validation beyond the pipeline's broad association question. Across
  the ten genes with non-null existing Boolean holdout labels, both arms match
  **9/10**: the AIRE gain is offset by the TRARG1 loss. This is descriptive
  agreement with imperfect legacy labels, not a validated accuracy estimate.

`insufficient_evidence` also switches from false to true for DENND2C, consistent
with its empty enabled retrieval pool. OncoKB membership does not change.

The two actual Tier-2 genes received **32 supplementary references** combined,
without a positive cancer-association change or any new verified citations.
This small subgroup provides no evidence for a Tier-2-specific recall benefit.

## Verdict

**This sample does not justify blanket OpenEvidence enablement for annotation
quality.** It costs **80.7% more synthesis input tokens**, **7.4% more synthesis
output tokens**, and **11.7× whole-run wall time**, for **one net extra verified
citation** and **one extra matched reference PMID**. The latter is the
ANKRD13A improvement with identical selected papers, although one paired run
cannot distinguish an OpenEvidence effect from ordinary synthesis variation. The 479 supplementary reference entries are not 479 independently
validated additions: they are deduplicated by the integration's per-analysis
citation keys, can overlap between genes, and may concern related genes or
context rather than the target gene.

OpenEvidence sometimes adds useful context, but the two classification flips
cancel against the existing labels, and spot checks reveal claim-to-citation
mismatches and overcorrection. The cost is therefore not justified by the
**demonstrated** quality gain in this sample. That is a provisional result,
not proof that OpenEvidence cannot help: there is one run per arm, only two
Tier-2 examples, incomplete/fusion-context reference labels, stochastic
selection and synthesis, a four-citation output cap, and no blinded expert
review. Thirteen retrieval pools and seven selected paper sets matched across
arms; the rest introduce additional uncertainty. Neither confidence intervals
nor a causal effect estimate are warranted here.

Dollar ROI cannot be calculated from these artifacts: OpenEvidence usage
charges/internal tokens are unavailable, and LLM cache creation/read tokens
have different billing weights. The tables provide observed token and latency
costs, not an invented dollar estimate. A useful next experiment would hold the
retrieved/selected papers fixed, repeat both synthesis conditions, and have an
expert adjudicate claim-level support—especially for truly sparse genes. For
now, this evidence supports keeping the integration opt-in rather than paying
its latency on every annotation.

## Experiment

We submitted the same 16 standalone genes to `run_pipeline(mode="full",
force_refresh=True)` twice, once with OpenEvidence disabled, then enabled.
The six established cancer genes were TP53, KRAS, EGFR, BRAF, BRCA1, and ALK.
The other ten were drawn from the repository's existing holdout: ACACB, AIRE,
ANKRD13A, CRACD, DENND2C, FAM117A, RFX7, RP1, TRARG1, and CLCN3P1. These
cover less-characterized associations, sparse evidence, and ambiguous symbols.
Selection preceded the runs; no genes were removed based on results.
The baseline actually used Tier 2 for CLCN3P1 and DENND2C and Tier 1 for the
other 14 genes. This is a convenience sample, not a prevalence-weighted sample
of clinical inputs. The Tier-2 subgroup is particularly small.

Both arms used the configured live Anthropic SDK, the normal selection and
synthesis escalation policy, three concurrent genes, and two concurrent LLM
calls. The fast/selection/retrieval model was `claude-haiku-4-5-20251001`, with
`claude-opus-4-7` for synthesis escalation. OpenEvidence used `darwin`.
The benchmark raised its read timeout from the configured 60 seconds to 900
seconds to allow slow responses to finish. This experiment therefore evaluates
returned OpenEvidence evidence with a generous timeout, not the response yield
of the default timeout. The `.env` was neither printed nor modified.

The benchmark executable adds observation wrappers around real pipeline
functions; it does not substitute API responses. It uses an initially empty
in-memory reference cache for each arm, avoiding shared Redis state and
cross-arm cache warming. OpenEvidence bypasses caching altogether. There is no
final annotation cache. Other than this cache isolation and the OpenEvidence
flag/timeout, the pipeline's retrieval, selection, synthesis, and validation
logic is unchanged. SDK-side prompt caching still applies and is measured.

## How to read the measurements

- **Verified citations:** unique PMIDs in the final annotation after the
  existing pipeline's checks. This is not an independent expert validation of
  the claims. OpenEvidence's supplementary references are counted separately;
  the integration deliberately does not add them to the verified PMID pool.
- **Reference recall:** overlap with nonempty PMID lists in the existing
  `benchmarks/data/holdout.jsonl`, divided by that reference list's size.
  Missing/empty lists mean unavailable recall, not zero or perfect recall.
  These labels concern fusion-context annotations, whereas this experiment
  uses standalone genes. They are a limited reference set, not exhaustive
  ground truth. No new gold labels or expert clinical adjudication were added.
- **Synthesis input/output:** provider-reported tokens across every synthesis
  call, including escalations. Input includes ordinary input, cache creation,
  and cache read tokens. Their billing rates differ; counts are not dollars.
  Raw per-call usage also includes selection and Tier-2 retrieval, reported
  separately as total pipeline LLM usage. OpenEvidence's internal model tokens
  and its monetary charges are not exposed by this integration.
- **Latency:** each annotation's `timings_ms.total`, from after normalization
  and admission to a gene concurrency slot through annotation construction.
  It includes waits for the shared LLM semaphore, but excludes the gene queue
  and initial normalization. Whole-run wall time is measured separately.
- **Change versus value:** Boolean cancer association and insufficient-evidence
  changes are distinguished from wording, gene-class labels, and reference
  changes. A changed summary or larger bibliography alone does not prove an
  improvement. Independent selection and synthesis are stochastic: two runs
  cannot isolate every OpenEvidence effect from ordinary run-to-run variation.

## Artifacts

- [Disabled arm](results/openevidence_live_20260904/disabled.json)
- [Enabled arm](results/openevidence_live_20260904/enabled.json)
- [Per-gene comparison](results/openevidence_live_20260904/comparison.json)
- [Runtime versions and additional settings](results/openevidence_live_20260904/environment.json)

## Reproduce

With credentials already configured locally:

```sh
uv run python -m benchmarks.run_openevidence_benchmark --arm disabled --output benchmarks/results/NEW_RUN
uv run python -m benchmarks.run_openevidence_benchmark --arm enabled --output benchmarks/results/NEW_RUN
uv run python -m benchmarks.compare_openevidence benchmarks/results/NEW_RUN
```

Use a new output directory: the runner refuses to overwrite an existing arm.
Each arm checkpoints after LLM usage, OpenEvidence completion, and completed
annotations. A failed/incomplete file is not a completed run and is rejected by
the comparison script. Artifacts preserve full retrieved abstracts, selected
PMIDs, supplementary analyses, final annotations, per-call usage, timings,
model settings, and timestamps for audit.

## Validation

No shared `src/` code changed. The benchmark runner, comparison script, and
accounting tests were added under `benchmarks/` and `tests/`.

- `uv run ruff check .` — passed for the full repository.
- `node --check src/static/app.js` — passed.
- `docker build -t agcg-openevidence-benchmark:validation .` — passed.
- Full repository pytest gate: **241 collected test cases, 241 passed, no
  skips** (43.88 seconds). This includes four new benchmark test functions.
  MySQL 8 and Redis 7 ran in isolated disposable containers on ports 13306
  and 16379; the containers were removed afterward. The exact invocation was:

```sh
uv run python - <<'PY'
import os
from dotenv import load_dotenv
import pytest
load_dotenv('.env')
os.environ.update(MYSQL_HOST='127.0.0.1', MYSQL_PORT='13306', MYSQL_USER='acgc',
                  MYSQL_PASSWORD='acgc', MYSQL_DATABASE='acgc', DB_URL='', DB_USERNAME='',
                  DB_PASSWORD='', REDIS_URL='redis://127.0.0.1:16379/0',
                  REDIS_SENTINEL_ENABLED='false')
raise SystemExit(pytest.main(['tests/', '-v', '-rA']))
PY
```

The repository defines no separate static type-check gate. The new tests cover
concurrent gene attribution, separation of reference caching from live
OpenEvidence, refusal to overwrite artifacts, cache-inclusive token accounting,
and rejection of incomplete arm comparisons. The live benchmark itself is
separate from these unit tests.
