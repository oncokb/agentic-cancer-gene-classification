# OpenEvidence pointed-question live benchmark

**Verdict: pointed phrasing measurably cuts OpenEvidence's own response latency and
partially closes one of PR #75's two flagged grounding failures, but it does not
move PR #75's "not worth enabling" conclusion.** Whole-run wall time and
OpenEvidence response time both improved meaningfully; synthesis/all-LLM token
cost and the final verified-citation count did not improve at all. The manual
re-check below finds the BRCA1 numeric-risk mismatch did not recur, but a
different, equally real citation-attribution problem replaced it on ALK.

This repeats PR #75's unmodified `run_openevidence_benchmark.py` and
`compare_openevidence.py` on commit `f06b87c` (PR #76's pointed/fusion-aware
OpenEvidence question, merged with PR #75's benchmark script), against the same
16 genes: TP53, KRAS, EGFR, BRAF, BRCA1, ALK, ACACB, AIRE, ANKRD13A, CRACD,
DENND2C, FAM117A, RFX7, RP1, TRARG1, CLCN3P1. No production code or benchmark
script was changed to produce these numbers.

## 1. Headline metrics — this run, disabled vs enabled

| Metric | Disabled | Enabled | Change |
|---|---:|---:|---:|
| Whole-run wall time | 98.9s | 861.0s | **8.7×** |
| Per-gene latency (mean / median / range) | 17.2s / 16.4s / 10.1–33.6s | 148.5s / 149.3s / 104.8–213.8s | +131.3s mean |
| OpenEvidence response time (mean / median / range) | — | 133.0s / 126.1s / 90.6–198.6s | — |
| Synthesis input tokens (total, incl. cache) | 82,787 | 152,131 | **+83.8%** |
| Synthesis output tokens | 14,688 | 18,446 | +25.6% |
| All-pipeline LLM input tokens | 123,125 | 195,122 | +58.5% |
| All-pipeline LLM output tokens | 16,623 | 20,467 | +23.1% |
| Verified citations (summed, 16 genes) | 49 | 49 | **+0** |
| Holdout reference recall (6 genes / 18 PMIDs) | 5/18 (27.8%) | 6/18 (33.3%) | +1 hit |
| Cancer-association agreement | — | 14/16 | 2 flips (AIRE, TRARG1) |
| OpenEvidence supplementary references (summed) | — | 312 | new, not independently verified |

Same-direction result as PR #75 (latency and token cost rise sharply when
enabled, for a single-digit citation/recall gain), but at somewhat different
absolute levels — see the run-to-run variance note in [§6](#6-methodology-and-caveats).

## 2. Before/after: PR #75's generic question vs this run's pointed question

All figures are the **enabled arm**, since the pointed-question change only
alters the text sent to OpenEvidence — the disabled arm never calls it and its
small run-to-run differences here are noise, not a phrasing effect (see
[§6](#6-methodology-and-caveats)).

| Metric | PR #75 (generic question) | This run (pointed question) | Change |
|---|---:|---:|---:|
| Whole-run wall time | 1,159.2s | 861.0s | **−25.7%** |
| Wall-time multiplier vs disabled | 11.7× | 8.7× | improved |
| OpenEvidence response time (mean / median) | 184.9s / 191.8s | 133.0s / 126.1s | **−28.1% / −34.3%** |
| Synthesis input tokens (total) | 150,559 | 152,131 | +1.0% (flat) |
| Synthesis output tokens | 15,655 | 18,446 | +17.8% (worse) |
| All-LLM input tokens | 193,562 | 195,122 | +0.8% (flat) |
| All-LLM output tokens | 17,647 | 20,467 | +16.0% (worse) |
| Verified citations (summed, disabled→enabled) | 47 → 48 (+1) | 49 → 49 (+0) | citation gain disappeared |
| Holdout recall (disabled→enabled) | 7/18 → 8/18 (+1) | 5/18 → 6/18 (+1) | same +1 pattern, lower absolute base |
| Supplementary references (summed) | 479 | 312 | −34.9% |

**Per-gene detail** (latency seconds disabled→enabled, synthesis input tokens
disabled→enabled, verified citations disabled→enabled, supplementary references):

| Gene | PR #75 latency | This run latency | PR #75 synth-in | This run synth-in | PR #75 citations | This run citations | OE refs (75→now) |
|---|---:|---:|---:|---:|---:|---:|---:|
| TP53 | 18.1→275.2 | 20.3→117.2 | 6,279→13,248 | 6,279→9,254 | 4→4 | 4→4 | 51→29 |
| KRAS | 15.0→265.9 | 15.7→131.4 | 6,156→12,856 | 6,156→9,158 | 4→4 | 4→4 | 53→31 |
| EGFR | 14.1→256.1 | 14.1→213.8 | 6,916→12,802 | 6,916→10,688 | 4→4 | 4→4 | 51→34 |
| BRAF | 18.0→237.1 | 12.5→160.5 | 6,190→12,200 | 6,099→22,050 | 4→4 | 4→4 | 64→27 |
| BRCA1 | 15.5→209.9 | 20.6→107.5 | 6,092→11,009 | 4,907→8,517 | 4→4 | 4→4 | 46→19 |
| ALK | 15.6→244.0 | 15.1→141.3 | 5,561→11,277 | 5,476→7,967 | 4→4 | 4→4 | 36→20 |
| ACACB | 14.7→211.9 | 11.1→157.2 | 4,223→9,168 | 4,209→6,561 | 4→4 | 4→4 | 29→13 |
| AIRE | 19.7→265.3 | 17.1→176.9 | 2,659→7,792 | 2,659→5,826 | 0→1 | 1→1 | 40→28 |
| ANKRD13A | 11.6→142.7 | 14.5→137.2 | 4,242→6,687 | 4,242→5,432 | 3→4 | 3→3 | 7→5 |
| CRACD | 10.3→208.2 | 14.2→119.7 | 4,856→7,827 | 5,344→6,770 | 4→4 | 4→4 | 9→5 |
| DENND2C | 31.6→123.2 | 33.6→166.8 | 8,245→4,905 | 8,872→11,599 | 2→0 | 3→1 | 19→13 |
| FAM117A | 11.6→157.8 | 10.1→157.8 | 4,339→6,641 | 4,339→6,482 | 4→4 | 4→4 | 7→11 |
| RFX7 | 21.0→145.5 | 22.9→104.8 | 4,251→7,397 | 4,251→6,877 | 3→4 | 4→4 | 12→9 |
| RP1 | 16.5→179.8 | 17.9→175.3 | 7,263→15,898 | 7,263→14,727 | 0→0 | 0→0 | 21→25 |
| TRARG1 | 16.5→166.4 | 17.1→192.8 | 3,672→6,627 | 3,382→6,821 | 3→3 | 2→3 | 21→22 |
| CLCN3P1 | 13.9→107.9 | 17.6→116.6 | 2,393→4,225 | 2,393→13,402 | 0→0 | 0→1 | 13→21 |

Latency improved for 13/16 genes (BRAF, DENND2C, RP1, TRARG1 got slower or flat).
Synthesis tokens improved for only 6/16 genes; three genes (BRAF, CLCN3P1,
RP1) show markedly *higher* enabled-arm token cost than PR #75, driven by
longer OpenEvidence text and, for BRAF, a synthesis escalation that PR #75's
run didn't trigger. **The headline hypothesis — that narrower phrasing reduces
token cost — is not supported by these numbers; only the latency half of the
hypothesis holds, and only partially.**

## 3. Manual re-check: did the ALK/BRCA1 grounding failures recur?

PR #75 flagged two specific claim-to-citation mismatches: ALK's enabled summary
attached a 7-year lorlatinib PFS of 55% and a 3-year adjuvant alectinib DFS of
88.7% to PMID 34175504 (whose abstract discusses fusion variants, not those
numbers); BRCA1's enabled summary attached ~72% breast / ~44% ovarian lifetime
risk to PMID 41498327 (whose abstract concerns ELF3 and replication stress, not
risk estimates). I read this run's actual final `gene_summary` /
`cancer_association_rationale` text, the pipeline's own `supporting_quotes` for
each retained citation, the raw retrieved PubMed abstracts, and the full
OpenEvidence supplementary text for both genes.

**BRCA1 — resolved in this sample.** PMID 41498327 (last run's mismatched PMID)
is no longer in the top-4 retained citations (`citations_removed: ['41498327']`,
replaced by `41359628`); it's still in the selected pool and is now cited only
for the claim it actually supports — "replication stress-driven transcriptional
changes in breast cancer" — which matches its abstract's own finding almost
verbatim ("replication stress is a feature of luminal progenitors... leads to
significant upregulation of ELF3"). All four retained citations
(`42323326`, `40202107`, `39265497`, `41359628`) have a matching pipeline
`supporting_quotes` entry, and each quote independently checks out against the
raw abstract text: choline/FAM3C metastasis (42323326), promoter-methylation
prognosis (40202107), GPX4-mediated ferroptosis resistance (39265497), and
K11-ubiquitination/PARPi sensitivity (41359628) are all stated near-verbatim in
their cited abstracts. No numeric claim in this run's BRCA1 summary lacks
abstract support.

**ALK — not resolved, but the specific failure changed shape.** The old
55%/88.7% numeric mismatch did not recur — no numeric trial statistic appears
unsupported in this run's ALK summary. But a different, equally real citation
problem replaced it: the final rationale states "clinical responses highly
dependent on ALK pathway activity and resistance predominantly mediated by
secondary ALK kinase-domain mutations (PMID 34175504, PMID 36423211)" and
"differential response correlates with specific EML4-ALK structural variants
(PMID 36423211)". PMID 36423211's actual abstract ("PI3Kβ inhibition enhances
ALK-inhibitor sensitivity in ALK-rearranged lung cancer") states the opposite
of the first claim — "clinical resistance typically develops over time and,
**in the majority of cases, resistance mechanisms are ALK-independent**"
(driven by EGFR/PI3K-AKT reactivation, not secondary ALK mutations) — and
never discusses EML4-ALK variant-specific response at all (that finding
belongs to PMID 34175504 alone). Consistent with this, the pipeline's own
`supporting_quotes` list has entries for the other three ALK citations
(40055571, 34175504, 41071329) but **no quote for 36423211** — the pipeline
itself never extracted supporting text for the citation it used to back these
two claims.

**Interpretation.** This is one paired run, so it cannot show that pointed
phrasing systematically fixes BRCA1-style failures and leaves ALK-style ones —
but it does show, concretely, that the two failure modes PR #75 flagged are not
a single bug with one fix. The specific numeric-hallucination pattern (a trial
statistic from OpenEvidence's supplementary text leaking into a final claim
attached to an unrelated PMID) did not appear in either gene this time. What
remains is an existing, narrower problem: the pipeline's own citation-selection
step can attach a real PMID to a claim its abstract does not support (or
contradicts), independent of whether OpenEvidence is involved or what the
question asks. Narrower phrasing did not touch that mechanism.

## 4. EML4::ALK fusion qualitative case (enabled-only, excluded from paired stats)

None of the 16 genes above exercise PR #76's new fusion-aware question path
(`_build_question(gene, fusion=...)`), so this is a separate, single,
enabled-only run of `EML4::ALK` via `python -m src.cli --fusions "EML4::ALK"`.
It is **not** included in any aggregate or table above. Raw output (both
genes' full annotations, including the OpenEvidence supplementary text and
`supporting_quotes` referenced below) is committed at
[`eml4_alk_fusion_qualitative.json`](results/openevidence_pointed_20260908/eml4_alk_fusion_qualitative.json).

- **Default 60s OpenEvidence timeout was insufficient.** A first attempt at the
  default `openevidence_timeout_seconds=60` failed for both partner genes with
  an empty-message timeout (`OpenEvidence supplementary lookup failed for
  EML4: ` / `for ALK: `, no exception text — consistent with a bare
  `httpx.ReadTimeout`). This matches PR #75's own finding that the integration
  needs a generous timeout; the CLI's production default is not that timeout.
  Re-running with `OPENEVIDENCE_TIMEOUT_SECONDS=900` (matching the benchmark's
  override) succeeded for both EML4 and ALK.
- **Fusion-aware OpenEvidence calls fire for both partner genes, not just the
  target.** `_maybe_fetch_openevidence_context` was invoked once for EML4 and
  once for ALK (each receiving the `fusion="EML4::ALK"` parameter per PR #76),
  producing two independent OpenEvidence analyses, both opening with "EML4::ALK
  is a definitively oncogenic, gain-of-function driver fusion... under
  somatic oncogenicity frameworks (ClinGen/CGC/VICC)" / "OncoKB 'Oncogenic,'
  highest therapeutic level" — a substantive, closed, fusion-specific classification
  answer for each gene, unlike a generic per-gene summary.
- **Trial-statistic grounding checked out.** EML4's final `gene_summary` states
  "median progression-free survival of 34.8 months with alectinib versus 10.9
  months with crizotinib in the ALEX trial... (PMID 30902613)". This is exactly
  the class of claim that failed for ALK in PR #75 (a specific trial PFS number
  attached to a PMID). Here it is grounded: the pipeline's own
  `supporting_quotes` for PMID 30902613 reads "The median PFS times were 34.8
  months with alectinib and 10.9 months with crizotinib" — verbatim from the
  retrieved abstract — and the OpenEvidence text for both EML4 and ALK
  independently cites the same ALEX figures (34.8 vs 10.9 months, HR 0.43).
  Three converging sources (pipeline-selected abstract, OpenEvidence, final
  summary) agree, which is the opposite pattern from PR #75's ALK finding.
- **Both partner genes reach the same classification**: `cancer_associated:
  true` for both ALK and EML4, `in_oncokb: true` for ALK. Citations differ per
  gene (ALK: `27573755, 34661367, 29650534, 32020234`; EML4:
  `37149843, 30902613, 40986428, 40875402`) since each gene's retrieval pool is
  built independently even though both describe the same fusion.
- This is one qualitative run of one fusion under a generous timeout — it shows
  the fusion-question path works end-to-end and produces a well-grounded
  result on this input, not that it always will.

## 5. Bottom-line verdict

**Pointed phrasing partially addresses PR #75's finding but does not move its
"not worth enabling" conclusion.** Concretely:

- **What it changed:** whole-run wall-time multiplier dropped from 11.7× to
  8.7×, driven almost entirely by OpenEvidence itself responding faster to a
  closed question (mean 184.9s → 133.0s, −28%). The BRCA1-specific numeric
  grounding failure PR #75 flagged did not recur in this sample, and the
  EML4::ALK qualitative case shows the fusion-question path can produce a
  well-grounded, three-source-corroborated answer.
- **What it did not change:** synthesis and all-pipeline LLM token cost are
  essentially flat versus PR #75 (+1.0% and +0.8% input respectively; output
  tokens are actually *higher*, +17.8%/+16.0%). The net citation gain PR #75
  measured (47→48) disappeared (49→49, +0). Holdout recall still shows only a
  +1-hit gain, off a lower base than PR #75. And the underlying grounding
  mechanism — a retained citation whose abstract doesn't support (or
  contradicts) the claim attached to it — is still present, just relocated
  from BRCA1 to ALK.
- **Bottom line:** this is evidence of a real, measurable latency improvement
  from narrower phrasing, not evidence that the integration's core cost/value
  tradeoff has changed. PR #75's core objection — the integration costs far
  more (still ~9× wall time, still no token reduction) for a marginal,
  inconsistent citation/recall gain, with an unresolved grounding-attribution
  risk — still holds. This does not newly justify blanket enablement; it
  narrows, but does not close, one of the two specific failure examples PR #75
  used to illustrate that objection.

## 6. Methodology and caveats

Both arms ran `run_pipeline(mode="full", force_refresh=True)` via the unmodified
`run_openevidence_benchmark.py`/`compare_openevidence.py`, disabled first, each
in a fresh process with an empty in-memory reference cache and a live
OpenEvidence cache bypass. The runner set `openevidence_enabled` per arm and
used the same 900-second OpenEvidence timeout as PR #75. Models, concurrency
(three genes / two LLM calls), the eight-paper synthesis limit, the
four-citation cap, and Python/dependency versions match the historical
artifacts (`environment.json` in both result directories). No production code
or benchmark script changed to produce these numbers; `f06b87c` is PR #76's
pointed/fusion-aware question layered on PR #75's benchmark harness.

- **This is one live run per arm** of a system with stochastic LLM selection
  and synthesis, live PubMed/OncoKB/OpenEvidence services, and a September
  2026 literature snapshot different from PR #75's September 4 snapshot.
  Disabled-arm numbers differ modestly between the two reports for several
  genes (e.g., BRCA1 synthesis-input 6,092→4,907, BRAF 6,190→6,099) purely from
  this run-to-run variance, since the disabled arm never touches the changed
  question text. That variance sets the noise floor against which the
  enabled-arm before/after numbers in §2 should be read — it does not
  invalidate the directional latency finding, but it does mean small
  differences (e.g., recall's absolute level) are not attributable to the
  phrasing change.
- **Holdout recall** (§1, §2) reuses PR #75's incomplete, fusion-context-labeled
  `benchmarks/data/holdout.jsonl` (18 reference PMIDs across genes with a
  non-empty label) against these standalone-gene runs — not a clinical
  accuracy score, and not a fresh gold-label review.
- **Verified citations** are the pipeline's retained unique PMIDs after its
  existing checks, not expert-validated claim support; §3's manual read is the
  only claim-level check in this report and covers two genes, not all 16.
  **Supplementary reference counts** are OpenEvidence's own deduplicated
  per-analysis citation keys, may overlap across genes, and are not
  independently verified additions.
- **Token counts are not dollar costs.** OpenEvidence's internal token usage
  and billing are not exposed by this integration in either report.
- The EML4::ALK case (§4) is a single qualitative run under a raised timeout,
  reported separately per the task scope — it is not part of any statistic in
  §1 or §2.

## Validation

No shared `src/` code changed; only benchmark artifacts and this report were
added.

- `uv run ruff check .` — passed, full repository.
- Full repository `tests/`: **264 collected test cases, 256 passed, 7 skipped
  (pre-existing Redis/MySQL-not-reachable skips in this sandbox; CI provisions
  both services), 1 failed (unrelated)** (`uv run pytest tests/ -q`). The
  single failure, `tests/test_local_backends_e2e.py::test_codex_backend_real_round_trip`,
  requires a live authenticated `codex` CLI and is documented in PR #75/#76 as
  a pre-existing, unrelated failure present on the base commit before either
  change.
- The repository defines no separate static type-check gate.

## Reproduce and artifacts

With locally configured credentials, use new output directories (existing arms
are never overwritten). `.env` is ignored and no credentials are committed.

```sh
uv run python -m benchmarks.run_openevidence_benchmark --arm disabled --output benchmarks/results/NEW_POINTED_RUN
OPENEVIDENCE_ENABLED=true uv run python -m benchmarks.run_openevidence_benchmark --arm enabled --output benchmarks/results/NEW_POINTED_RUN
uv run python -m benchmarks.compare_openevidence benchmarks/results/NEW_POINTED_RUN

# EML4::ALK qualitative case (needs a raised timeout; the CLI's own default is 60s)
OPENEVIDENCE_ENABLED=true OPENEVIDENCE_TIMEOUT_SECONDS=900 uv run python -m src.cli --fusions "EML4::ALK" --output OUT.json
```

- [This run's disabled arm](results/openevidence_pointed_20260908/disabled.json)
- [This run's enabled arm](results/openevidence_pointed_20260908/enabled.json)
- [This run's comparison](results/openevidence_pointed_20260908/comparison.json)
- [This run's runtime versions/settings](results/openevidence_pointed_20260908/environment.json)
- [EML4::ALK fusion qualitative case, raw output](results/openevidence_pointed_20260908/eml4_alk_fusion_qualitative.json)
- [PR #75's report](openevidence_value_report.md) and its
  [historical comparison](results/openevidence_live_20260904/comparison.json)
