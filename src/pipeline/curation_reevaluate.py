"""Draft-only synthesis of curator-pinned literature. Never writes annotation/run stores."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

from src.config import settings
from src.models.schema import (
    ClinicalActionability,
    ClinicalActionabilityEvidence,
    EvidenceCard,
    FusionEvidenceCard,
    LiteratureRecord,
)
from src.pipeline import clinical_actionability as actionability
from src.pipeline import literature
from src.pipeline.db_lookups import check_oncokb_membership, get_msk_genie_prevalence
from src.pipeline.llm_client import complete_with_tool
from src.pipeline.normalization import is_fusion_input, resolve_gene, split_fusion
from src.pipeline.orchestrator import _format_gene_identity
from src.pipeline.synthesis import build_gene_annotation, synthesize_gene_annotation, _evidence_type

logger = logging.getLogger(__name__)


class ReevaluateRequest(BaseModel):
    subject_type: Literal["gene", "fusion"]
    subject_key: str = Field(min_length=1, max_length=128)
    tumor_type: Optional[str] = Field(default=None, max_length=256)
    evidence_pmids: list[str] = Field(default_factory=list)
    precedent_pmids: list[str] = Field(default_factory=list)

    @field_validator("subject_key", "tumor_type")
    @classmethod
    def strip_text(cls, value):
        return value.strip() if value is not None else None

    @field_validator("evidence_pmids", "precedent_pmids")
    @classmethod
    def unique_pmids(cls, values):
        if any(not re.fullmatch(r"[1-9][0-9]*", value) for value in values):
            raise ValueError("PMIDs must be positive numeric strings")
        if len(set(values)) != len(values):
            raise ValueError("Duplicate PMIDs within a list are not allowed")
        return values

    @model_validator(mode="after")
    def valid_subject(self):
        if not self.subject_key or (self.subject_type == "fusion") != is_fusion_input(
            self.subject_key
        ):
            raise ValueError("subject_key must match subject_type (two partners for fusion)")
        if not self.evidence_pmids and not self.precedent_pmids:
            raise ValueError("Supply at least one evidence or precedent PMID")
        return self


class InvalidPMIDs(ValueError):
    def __init__(self, reasons: dict[str, list[str]]):
        super().__init__("Curator PMIDs failed validation")
        self.reasons = reasons


async def validated_records(pmids: list[str]) -> dict[str, LiteratureRecord]:
    records = await literature._efetch(pmids, literature._get_ncbi_client(), filter_retracted=False)
    by_pmid = {record.pmid: record for record in records}
    # _efetch omits non-retracted articles with no abstract. Verify existence
    # of missing IDs so these are reported separately from unknown IDs.
    missing = [pmid for pmid in pmids if pmid not in by_pmid]
    existing_without_abstract = (
        set(
            await literature._esearch(
                " OR ".join(f"{pmid}[uid]" for pmid in missing),
                len(missing),
                literature._get_ncbi_client(),
            )
        )
        if missing
        else set()
    )
    reasons: dict[str, list[str]] = {"unknown": [], "retracted": [], "no_abstract": []}
    for pmid in pmids:
        record = by_pmid.get(pmid)
        if record is None:
            reasons["no_abstract" if pmid in existing_without_abstract else "unknown"].append(pmid)
        else:
            if literature._is_retracted_publication(record):
                reasons["retracted"].append(pmid)
            if not record.abstract.strip():
                reasons["no_abstract"].append(pmid)
    if any(reasons.values()):
        raise InvalidPMIDs(reasons)
    return by_pmid


def verbatim_quote(record: LiteratureRecord, candidate: Any = None) -> str:
    if isinstance(candidate, str) and candidate.strip() and candidate in record.abstract:
        return candidate
    # Do not append punctuation or normalize whitespace: exact abstract substring.
    return re.split(r"(?<=[.!?])\s+", record.abstract.strip(), maxsplit=1)[0]


CARD_TOOL = {
    "name": "suggest_evidence_cards",
    "description": "Suggest curator evidence descriptions and a precedent summary.",
    "input_schema": {
        "type": "object",
        "required": ["records", "precedent_summary"],
        "properties": {
            "precedent_summary": {"type": "string"},
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pmid", "selected_reason", "quote", "evidence_type"],
                    "properties": {
                        key: {"type": "string"}
                        for key in ("pmid", "selected_reason", "quote", "evidence_type")
                    },
                },
            },
        },
    },
}


async def suggest_cards(subject: str, records: list[LiteratureRecord], precedent_pmids: list[str]):
    if not records:
        return {}, None
    try:
        result = await complete_with_tool(
            model=settings.pmid_distillation_model,
            system=(
                "Suggest evidence for curator review. Evaluate relevance to the supplied gene or "
                "exact fusion; acknowledge indirect or unrelated evidence. Give a selected_reason, "
                "evidence_type and short quote copied verbatim from each abstract. Summarize only "
                "the supplied precedent PMIDs as therapeutic/domain or fusion-partner precedent; "
                "do not assume an exact fusion is supported by partner precedent."
            ),
            user=f"Subject: {subject}\nPrecedent PMIDs: {precedent_pmids}\n"
            + "\n".join(
                f"PMID: {r.pmid}\nTitle: {r.title}\nAbstract: {r.abstract}" for r in records
            ),
            tool=CARD_TOOL,
            max_tokens=max(2048, 160 * len(records)),
            model_purpose="curation_reevaluate_cards",
        )
        result = result or {}
        return {
            r["pmid"]: r
            for r in result.get("records", [])
            if isinstance(r, dict) and isinstance(r.get("pmid"), str)
        }, result.get("precedent_summary")
    except Exception:
        logger.warning("Evidence suggestions failed; using abstract fallbacks", exc_info=True)
        return {}, None


async def gene_context(subject: str, tumor_type: Optional[str], run_store):
    async with httpx.AsyncClient() as client:
        resolved = await resolve_gene(subject, client)
    if not resolved.resolved:
        raise ValueError(f"Could not resolve gene: {subject}")
    gene = resolved.canonical_symbol
    cached = (
        await run_store.get_gene_annotation(gene, tumor_type=tumor_type)
        if run_store is not None
        else None
    )
    if cached:
        annotation = cached["annotation"]
        return (
            gene,
            annotation.get("fusions", []),
            annotation.get("in_oncokb"),
            annotation.get("cancer_type_prevalence"),
            _format_gene_identity(resolved),
        )
    return (
        gene,
        [],
        await check_oncokb_membership(gene),
        get_msk_genie_prevalence(gene),
        _format_gene_identity(resolved),
    )


async def reevaluate(request: ReevaluateRequest, run_store=None) -> dict:
    pmids = list(dict.fromkeys(request.evidence_pmids + request.precedent_pmids))
    by_pmid = await validated_records(pmids)
    records = [by_pmid[p] for p in request.evidence_pmids]
    precedents = [by_pmid[p] for p in request.precedent_pmids]
    fusion = request.subject_type == "fusion"
    if fusion:
        five, three = split_fusion(request.subject_key)
        subject, fusions, oncokb, prevalence, identity = (
            f"{five}::{three}",
            [f"{five}::{three}"],
            None,
            None,
            "Assess the exact two-partner fusion, not either partner alone. "
            "Curator-selected papers may be indirect evidence; explicitly state limitations.",
        )
    else:
        subject, fusions, oncokb, prevalence, identity = await gene_context(
            request.subject_key, request.tumor_type, run_store
        )
    if request.tumor_type:
        identity = f"{identity or ''}; Tumor context: {request.tumor_type}"
    synthesis = await synthesize_gene_annotation(
        gene=subject,
        fusions=fusions,
        in_oncokb=oncokb,
        cancer_type_prevalence=prevalence,
        records=records,
        gene_identity=identity,
        pinned=True,
    )
    raw_quotes = {
        q["pmid"]: q
        for q in synthesis.get("supporting_quotes", []) or []
        if isinstance(q, dict) and isinstance(q.get("pmid"), str)
    }
    # Suggest descriptions for missing LLM cards and for each precedent. One batched Haiku call.
    missing = [r for r in records if not raw_quotes.get(r.pmid, {}).get("selected_reason")]
    suggestions, summary = await suggest_cards(
        subject, list({r.pmid: r for r in missing + precedents}.values()), request.precedent_pmids
    )
    cards = []
    quotes = []
    for record in records:
        supplied = raw_quotes.get(record.pmid, {})
        suggested = suggestions.get(record.pmid, {})
        quote = verbatim_quote(record, supplied.get("quote") or suggested.get("quote"))
        values = dict(
            pmid=record.pmid,
            title=record.title,
            journal=record.journal,
            publication_types=record.publication_types,
            is_preprint=literature.is_preprint_publication(record.publication_types),
            abstract=record.abstract,
            quote=quote,
            selected_reason=supplied.get("selected_reason")
            or suggested.get("selected_reason")
            or "Curator-selected evidence; review the abstract for relevance to this subject.",
            evidence_type=supplied.get("evidence_type")
            or suggested.get("evidence_type")
            or _evidence_type(record),
        )
        cards.append(
            FusionEvidenceCard(fusion=subject, **values) if fusion else EvidenceCard(**values)
        )
        quotes.append({"pmid": record.pmid, "quote": quote})
    synthesis["citations"] = request.evidence_pmids
    synthesis["supporting_quotes"] = quotes
    annotation = build_gene_annotation(
        subject, fusions, oncokb, prevalence, records, synthesis, tumor_type=request.tumor_type
    )
    draft = annotation.model_dump(mode="json")
    draft["cache_status"] = None
    draft["cache_reason"] = None
    draft["evidence_cards"] = [c.model_dump() for c in cards]
    draft["quality_flags"].append(
        {
            "code": "curator_pinned_draft",
            "label": "Curator-selected draft",
            "severity": "info",
            "detail": "All supplied evidence PMIDs are included in curator order. Inclusion does not establish relevance; review this unpersisted suggestion.",
        }
    )
    precedent_block = None
    if precedents:
        precedent_block = ClinicalActionability(
            confidence_score=1.0,
            confidence_level="high",
            summary=summary
            or f"Curator-selected precedent for {subject}; review each abstract for applicability.",
            confidence_explanation="Curator-supplied PMIDs: threshold bypassed. Confidence denotes curator selection, not a calibrated evidence assessment.",
            pmids=request.precedent_pmids,
            evidence=[
                ClinicalActionabilityEvidence(
                    pmid=r.pmid,
                    title=r.title,
                    abstract=r.abstract,
                    evidence_type=actionability._evidence_type(r),
                    therapies=actionability._extract_therapies(actionability._record_text(r)),
                    domains=actionability._extract_domains(actionability._record_text(r)),
                    quote=verbatim_quote(r, suggestions.get(r.pmid, {}).get("quote")),
                )
                for r in precedents
            ],
        ).model_dump()
    draft["clinical_actionability"] = precedent_block
    if fusion:
        draft.pop("gene")
        draft.update(
            fusion=subject,
            tumor_type=request.tumor_type,
            pmids=request.evidence_pmids,
            retrieved_count=len(records),
            interpretation=draft["cancer_association_rationale"] or "",
            well_supported=synthesis.get("cancer_associated") is True
            and not annotation.insufficient_evidence,
        )
    draft["model_info"] = {
        "synthesis_model": settings.synthesis_model
        if synthesis.get("_synthesis_escalated") or not settings.synthesis_model_escalation
        else settings.synthesis_fast_model,
        "synthesis_escalated": bool(synthesis.get("_synthesis_escalated")),
        "suggestion_model": settings.pmid_distillation_model if missing or precedents else None,
    }
    draft["generated_at"] = datetime.now(timezone.utc).isoformat()
    return draft
