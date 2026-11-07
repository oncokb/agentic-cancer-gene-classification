"""Literature gate: does the literature characterize a gene IN a tumor type?

The router and service only see the ``LiteratureGate`` protocol, so the
implementation can later be swapped for LRAS (``/lras/v1/retrieve``) without
touching them. The gate name is part of the stored verdict key, so verdicts
from different gates (or gate versions) never mix: bumping ``name`` makes
every stored verdict of the previous version unreachable, and the pair is
evaluated again.

v2 (``acgc_tumor_specific_v2``) replaces the v1 rule ("pipeline not
insufficient AND >=1 citation"), which was gene-general: KLK3 was "supported"
in kidney chromophobe purely on prostate-cancer papers. v2 is a narrow,
tumor-specific judgement modelled on ACGC's fusion-partner evidence check
(tumor-scoped PubMed retrieval + a per-record filter):

1. Retrieve with ACGC's public ``retrieve_literature(gene, tumor_type=...)``
   (its first stage is the tumor-type query with ACGC's alias expansion).
2. Deterministic guard: only papers whose title/abstract mention the tumor
   type (ACGC's public ``requested_tumor_terms_in_record`` alias matcher, or
   the manifest's name / cohort code) can count. No such paper -> verdict
   ``insufficient`` without an LLM call.
3. One LLM call (``complete_with_tool``) judges each remaining paper: does it
   describe the gene's expression or role IN THIS tumor type? Plus an overall
   ``supported | not_supported | insufficient`` and a short rationale.
4. Validation: judgements for PMIDs that were not sent are dropped (no
   hallucinated PMIDs); the citations are only the PMIDs judged tumor-specific.

A pair is supported iff verdict == "supported" AND >=1 tumor-specific PMID
(``verdict_supported``, re-applied by the service to every stored verdict).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Sequence, runtime_checkable

from src.expression_context.models import LiteratureVerdict, PaperJudgement, TumorContext

logger = logging.getLogger(__name__)

TUMOR_SPECIFIC_GATE = "acgc_tumor_specific_v2"
JUDGE_MODEL_PURPOSE = "expression_context_tumor_evidence"
_PMID = re.compile(r"^\d{1,9}$")


class LiteratureGateError(RuntimeError):
    """The gate could not produce a verdict (not the same as 'unsupported')."""


@runtime_checkable
class LiteratureGate(Protocol):
    name: str

    async def evaluate(self, hugo_symbol: str, tumor: TumorContext) -> LiteratureVerdict:
        ...


def is_supported(insufficient_evidence: bool, pmids: Sequence[str]) -> bool:
    """Legacy v1 rule (verdicts without a ``verdict`` field)."""
    return (not insufficient_evidence) and len(pmids) >= 1


def verdict_supported(verdict: LiteratureVerdict) -> bool:
    """The surfacing rule, re-applied to every verdict (fresh or stored).

    Tumor-specific verdicts: ``verdict == "supported"`` AND >=1 cited PMID,
    every cited PMID being one of the papers judged tumor-specific (when the
    judgements are recorded). Verdicts without ``verdict`` use the v1 rule.
    """
    if verdict.verdict is None:
        return is_supported(verdict.insufficient_evidence, verdict.pmids)
    if verdict.verdict != "supported" or not verdict.pmids:
        return False
    if verdict.papers:
        specific = {paper.pmid for paper in verdict.papers if paper.tumor_specific}
        return all(pmid in specific for pmid in verdict.pmids)
    return True


# --------------------------------------------------------------------------
# v2: tumor-specific judgement
# --------------------------------------------------------------------------

JUDGE_SYSTEM_PROMPT = """\
You check whether published papers characterize a gene in ONE SPECIFIC tumor
type. You are given the gene, the tumor type (cohort code, name and the
disease phrase), and PubMed abstracts that mention that tumor type somewhere.

For each paper decide tumor_specific:
- true only if the paper itself reports the gene's expression, alteration,
  or functional/clinical role IN THIS tumor type (patient tumors, cell lines
  or models of this tumor type), as a finding about the gene being present,
  elevated or relevant there.
- false if the paper is about a different tumor type and only mentions this
  one (e.g. in a list, a differential diagnosis, a pan-cancer table, or as a
  comparison), if the gene is only mentioned in passing, if the finding is
  that the gene is absent/negative in this tumor type, or if the gene is
  studied in another tumor type. Papers about a different tumor type never
  count, however strong they are for that other tumor type.

Then give an overall verdict:
- supported: at least one paper is tumor_specific and, taken together, the
  tumor-specific papers show the gene is expressed or relevant in this tumor
  type.
- not_supported: the papers that discuss this tumor type indicate the gene is
  not expressed or not relevant there.
- insufficient: no paper is tumor_specific, or the evidence is too thin.

Give a short rationale (1-3 sentences) that names the tumor type and cites
only tumor-specific papers. Judge only the papers provided, by their PMID;
never cite any other PMID. Call judge_tumor_specific_evidence exactly once.
"""

JUDGE_TOOL: Dict[str, Any] = {
    "name": "judge_tumor_specific_evidence",
    "description": "Judge, per paper and overall, whether the gene is characterized in the given tumor type.",
    "input_schema": {
        "type": "object",
        "required": ["papers", "verdict", "rationale"],
        "properties": {
            "papers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pmid", "tumor_specific"],
                    "properties": {
                        "pmid": {"type": "string", "description": "PMID of a provided paper."},
                        "tumor_specific": {"type": "boolean"},
                        "reason": {"type": "string", "description": "One short sentence."},
                    },
                },
            },
            "verdict": {"type": "string", "enum": ["supported", "not_supported", "insufficient"]},
            "rationale": {"type": "string"},
        },
    },
}

Retriever = Callable[[str, str], Awaitable[List[Any]]]
Judge = Callable[..., Awaitable[Dict[str, Any]]]
TermMatcher = Callable[[Any, str], List[str]]


async def _acgc_retrieve(gene: str, tumor_type_query: str) -> List[Any]:
    # Imported lazily: pulls in NCBI/LLM clients only when a check runs.
    from src.pipeline.literature import retrieve_literature

    records, _tier = await retrieve_literature(gene, tumor_type=tumor_type_query)
    return list(records)


def _acgc_tumor_terms(record: Any, tumor_type: str) -> List[str]:
    from src.pipeline.literature import requested_tumor_terms_in_record

    return list(requested_tumor_terms_in_record(record, tumor_type))


async def _acgc_judge(**kwargs: Any) -> Dict[str, Any]:
    from src.pipeline.llm_client import complete_with_tool

    return await complete_with_tool(**kwargs)


def _default_model() -> str:
    from src.config import settings as acgc_settings

    return acgc_settings.selection_model


def _word(term: str, flags: int = 0) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", flags)


# Words that don't identify a tumor type on their own.
_GENERIC_WORDS = frozenset({
    "cancer", "cancers", "carcinoma", "carcinomas", "adenocarcinoma", "adenocarcinomas",
    "tumor", "tumors", "tumour", "tumours", "neoplasm", "neoplasms", "malignancy",
    "malignancies", "malignant", "cell", "cells", "invasive", "primary", "type", "and",
    "of", "the", "in", "with", "or",
})


def _distinctive_words(phrase: str) -> List[str]:
    words = re.findall(r"[A-Za-z0-9]+", phrase or "")
    return [w for w in words if w.lower() not in _GENERIC_WORDS and len(w) > 2]


class TumorSpecificGate:
    """v2 gate: tumor-scoped retrieval + deterministic guard + narrow LLM judge."""

    name = TUMOR_SPECIFIC_GATE

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        max_papers: int = 12,
        abstract_chars: int = 1500,
        retrieve: Retriever = _acgc_retrieve,
        tumor_terms: TermMatcher = _acgc_tumor_terms,
        judge: Judge = _acgc_judge,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._model = model
        self._max_papers = max_papers
        self._abstract_chars = abstract_chars
        self._retrieve = retrieve
        self._tumor_terms = tumor_terms
        self._judge = judge
        self._clock = clock

    # ------------------------------------------------------------ guard

    def _matched_terms(self, record: Any, tumor: TumorContext) -> tuple[int, List[str]]:
        """(strength, terms): tumor-type terms in the record's title/abstract.

        Strength 2: ACGC's alias matcher, the literal disease phrase or name,
        or the cohort code as a whole upper-case word. Strength 1: every
        distinctive word of the phrase or the name (recall for wordings the
        alias table lacks, e.g. "chromophobe kidney tumours"). 0: no mention,
        so the paper can't count for this tumor type.
        """
        strong: List[str] = []
        for phrase in (tumor.tumor_type_query, tumor.name):
            if phrase:
                try:
                    strong.extend(self._tumor_terms(record, phrase))
                except Exception:  # matcher problems must not fail the check
                    logger.debug("Tumor-term matcher failed for %r", phrase, exc_info=True)
        text = f"{getattr(record, 'title', '') or ''} {getattr(record, 'abstract', '') or ''}"
        lowered = text.lower()
        for phrase in (tumor.tumor_type_query, tumor.name):
            phrase = (phrase or "").strip()
            if phrase and phrase.lower() in lowered:
                strong.append(phrase)
        code = (tumor.oncotree_code or "").strip()
        if len(code) >= 3 and _word(code).search(text):
            strong.append(code)
        if strong:
            return 2, list(dict.fromkeys(strong))
        for phrase in (tumor.tumor_type_query, tumor.name):
            words = _distinctive_words(phrase)
            if words and all(_word(w, re.IGNORECASE).search(text) for w in words):
                return 1, words
        return 0, []

    # ------------------------------------------------------------ evaluate

    async def evaluate(self, hugo_symbol: str, tumor: TumorContext) -> LiteratureVerdict:
        try:
            records = await self._retrieve(hugo_symbol, tumor.tumor_type_query)
        except Exception as exc:
            raise LiteratureGateError(f"literature retrieval failed: {type(exc).__name__}") from exc

        ranked: List[tuple[int, int, Any]] = []
        matched: Dict[str, List[str]] = {}
        seen: set[str] = set()
        for index, record in enumerate(records):
            pmid = str(getattr(record, "pmid", "") or "").strip()
            if not _PMID.fullmatch(pmid) or pmid in seen:
                continue
            seen.add(pmid)
            strength, terms = self._matched_terms(record, tumor)
            if strength:
                matched[pmid] = terms
                ranked.append((-strength, index, record))
        # Stronger tumor mentions first, then retrieval order; capped.
        candidates = [record for _, _, record in sorted(ranked, key=lambda item: item[:2])]
        candidates = candidates[: self._max_papers]

        if not candidates:
            return self._verdict(
                tumor, "insufficient", [], len(seen),
                f"No retrieved paper on {hugo_symbol} mentions {tumor.name} "
                f"({tumor.oncotree_code}); papers about other tumor types do not count.",
            )

        try:
            result = await self._judge(
                model=self._model or _default_model(),
                system=JUDGE_SYSTEM_PROMPT,
                user=self._prompt(hugo_symbol, tumor, candidates, matched),
                tool=JUDGE_TOOL,
                max_tokens=2048,
                model_purpose=JUDGE_MODEL_PURPOSE,
            )
        except Exception as exc:
            raise LiteratureGateError(f"tumor-specific judgement failed: {type(exc).__name__}") from exc
        if not isinstance(result, dict):
            raise LiteratureGateError("tumor-specific judgement returned no tool input")

        sent = {str(record.pmid).strip(): record for record in candidates}
        judged: Dict[str, Dict[str, Any]] = {}
        dropped: List[str] = []
        for entry in result.get("papers") or []:
            if not isinstance(entry, dict):
                continue
            pmid = str(entry.get("pmid", "")).strip()
            if pmid not in sent:
                dropped.append(pmid)  # never sent: hallucinated or garbled
                continue
            judged.setdefault(pmid, entry)  # first judgement per PMID wins
        if dropped:
            logger.warning(
                "Tumor-specific judge for %s/%s returned unknown PMIDs %s; dropped",
                hugo_symbol, tumor.oncotree_code, dropped[:10],
            )

        papers: List[PaperJudgement] = []
        for pmid, record in sent.items():
            entry = judged.get(pmid, {})
            papers.append(PaperJudgement(
                pmid=pmid,
                title=(getattr(record, "title", None) or None),
                matched_terms=matched.get(pmid, []),
                tumor_specific=entry.get("tumor_specific") is True,
                reason=_clip(entry.get("reason"), 300),
            ))
        raw_verdict = result.get("verdict")
        verdict = raw_verdict if raw_verdict in ("supported", "not_supported", "insufficient") else "insufficient"
        rationale = _clip(result.get("rationale"), 1000)
        specific = [paper.pmid for paper in papers if paper.tumor_specific]
        if verdict == "supported" and not specific:
            verdict = "insufficient"
            rationale = (rationale + " " if rationale else "") + (
                "[No paper was judged specific to this tumor type.]"
            )
        return self._verdict(tumor, verdict, papers, len(seen), rationale)

    def _verdict(
        self,
        tumor: TumorContext,
        verdict: str,
        papers: List[PaperJudgement],
        retrieved: int,
        rationale: Optional[str],
    ) -> LiteratureVerdict:
        pmids = [paper.pmid for paper in papers if paper.tumor_specific]
        result = LiteratureVerdict(
            literature_gate=self.name,
            tumor_type_query=tumor.tumor_type_query,
            supported=False,
            insufficient_evidence=verdict == "insufficient",
            pmids=pmids,
            rationale=rationale,
            evaluated_at=self._clock(),
            verdict=verdict,
            oncotree_code=tumor.oncotree_code,
            cancer_type_name=tumor.name,
            retrieved_count=retrieved,
            papers=papers,
        )
        return result.model_copy(update={"supported": verdict_supported(result)})

    def _prompt(
        self,
        gene: str,
        tumor: TumorContext,
        records: List[Any],
        matched: Dict[str, List[str]],
    ) -> str:
        lines = [
            f"Gene: {gene}",
            f"Tumor type: {tumor.name} (TCGA cohort code {tumor.oncotree_code}; "
            f"disease phrase \"{tumor.tumor_type_query}\")",
            f"Names for this tumor type: {', '.join(tumor.terms)}",
            "",
            f"### Papers ({len(records)})",
        ]
        for record in records:
            pmid = str(record.pmid).strip()
            abstract = (getattr(record, "abstract", "") or "").strip()
            if len(abstract) > self._abstract_chars:
                abstract = abstract[: self._abstract_chars].rstrip() + " …"
            lines += [
                "---",
                f"PMID: {pmid}",
                f"Title: {getattr(record, 'title', '') or ''}",
                f"Tumor-type terms found: {', '.join(matched.get(pmid, []))}",
                f"Abstract: {abstract}",
            ]
        return "\n".join(lines)


def _clip(value: Any, limit: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    value = " ".join(value.split())
    return value[:limit] if value else None
