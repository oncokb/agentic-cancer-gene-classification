"""Background distillation of retrieved abstracts into the permanent
pmid_evidence cache (see run_store.py's pmid_evidence table).

Fired as a fire-and-forget background task after synthesis for any selected
paper not already cached (see orchestrator.py's _annotate_gene) — never on
the critical path, since a distillation is a one-time cost per PMID that
benefits every future gene/fusion annotation that retrieves the same paper.
"""

from __future__ import annotations

import logging
from typing import Any, List

from src.config import settings
from src.models.schema import LiteratureRecord, PMIDEvidenceRecord
from src.pipeline.llm_client import complete_with_tool

logger = logging.getLogger(__name__)

DISTILL_SYSTEM_PROMPT = """\
You permanently distill published cancer-genomics abstracts into short,
reusable takeaways for a literature cache shared across all future gene and
fusion annotations. For each abstract, extract:

- distilled_takeaway: a 25-40 word core clinical/functional conclusion —
  the single most important finding, written so it can replace the full
  abstract in a future synthesis prompt without losing the finding a
  synthesis model would need. Include concrete numbers (trial names,
  survival months, hazard ratios, response rates) whenever the abstract
  reports them.
- evidence_type: one of clinical, preclinical, case_report, review, other.
- oncogenic_role: one of oncogene, tumor_suppressor, resistance, neutral,
  unknown — the role this abstract's evidence supports, if any.
- supporting_quote: one short verbatim sentence from the abstract that most
  directly grounds the takeaway.

Call distill_pmid_evidence with one entry per abstract, in the same order
provided. Never invent facts not present in the abstract.
"""

DISTILL_TOOL: dict = {
    "name": "distill_pmid_evidence",
    "description": "Distill retrieved abstracts into short, permanently-cacheable takeaways.",
    "input_schema": {
        "type": "object",
        "required": ["records"],
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pmid", "distilled_takeaway"],
                    "properties": {
                        "pmid": {
                            "type": "string",
                            "description": "PMID of the abstract this entry distills.",
                        },
                        "distilled_takeaway": {
                            "type": "string",
                            "description": "25-40 word core clinical/functional conclusion.",
                        },
                        "evidence_type": {
                            "type": "string",
                            "enum": ["clinical", "preclinical", "case_report", "review", "other"],
                        },
                        "oncogenic_role": {
                            "type": "string",
                            "enum": [
                                "oncogene",
                                "tumor_suppressor",
                                "resistance",
                                "neutral",
                                "unknown",
                            ],
                        },
                        "supporting_quote": {"type": "string"},
                    },
                },
            },
        },
    },
}


def _build_user_prompt(records: List[LiteratureRecord]) -> str:
    lines = [f"### Abstracts to distill ({len(records)}):"]
    for record in records:
        lines += [
            "---",
            f"PMID: {record.pmid}",
            f"Title: {record.title}",
            f"Journal: {record.journal}",
            f"Abstract: {record.abstract}",
        ]
    return "\n".join(lines)


async def distill_pmid_abstracts(records: List[LiteratureRecord]) -> List[PMIDEvidenceRecord]:
    """Distill `records` into PMIDEvidenceRecord objects via one batched LLM
    call (always the Anthropic SDK path — this never runs in local_mode, see
    orchestrator.py). Returns [] (never raises) on any failure: this is a
    best-effort cache-population step, never something that should surface
    as an error to whatever fired it.
    """
    if not records:
        return []
    records_by_pmid = {record.pmid: record for record in records}
    try:
        tool_input = await complete_with_tool(
            model=settings.pmid_distillation_model,
            system=DISTILL_SYSTEM_PROMPT,
            user=_build_user_prompt(records),
            tool=DISTILL_TOOL,
            max_tokens=2048,
            model_purpose="pmid_distillation",
        )
    except Exception:
        logger.warning("pmid_evidence distillation LLM call failed", exc_info=True)
        return []

    distilled: List[PMIDEvidenceRecord] = []
    for entry in (tool_input or {}).get("records", []):
        if not isinstance(entry, dict):
            continue
        pmid = entry.get("pmid")
        source = records_by_pmid.get(pmid)
        takeaway = entry.get("distilled_takeaway")
        if source is None or not takeaway:
            continue
        distilled.append(
            PMIDEvidenceRecord(
                pmid=pmid,
                title=source.title,
                journal=source.journal,
                publication_year=source.publication_year,
                evidence_type=entry.get("evidence_type") or "other",
                oncogenic_role=entry.get("oncogenic_role") or "unknown",
                distilled_takeaway=takeaway,
                supporting_quote=entry.get("supporting_quote"),
            )
        )
    return distilled


async def distill_and_save_pmid_evidence(run_store: Any, records: List[LiteratureRecord]) -> None:
    """Distill `records` and persist them, swallowing all errors. Meant to be
    fired via asyncio.create_task and never awaited on the annotation path
    (see orchestrator.py's _annotate_gene) — a failure here must never affect
    the annotation that triggered it.
    """
    if run_store is None or not records:
        return
    try:
        distilled = await distill_pmid_abstracts(records)
        if distilled:
            await run_store.save_pmid_evidence_batch(distilled)
    except Exception:
        logger.warning("Failed to persist distilled pmid_evidence", exc_info=True)
