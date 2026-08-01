"""Fusion position context parsing and optional fusion-annotation-service enrichment.

Enrichment calls out to the sibling `fusion-annotation` service (not Genome Nexus
directly — Genome Nexus is just one of that service's internal data providers),
which computes protein-domain retention deterministically from real transcript/
domain coordinates and attaches CIViC-sourced treatment knowledge. No LLM is
involved anywhere in this module.
"""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from io import StringIO
from typing import Iterable, Optional

import httpx

from src.config import settings
from src.models.schema import FusionPartnerContext, FusionPositionContext, FusionTreatmentKnowledge
from src.pipeline.normalization import split_fusion

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ParsedFusionInput:
    raw: str
    fusion: str
    five_gene: str
    three_gene: str
    five_exon: Optional[str] = None
    three_exon: Optional[str] = None
    five_genomic: Optional[str] = None
    three_genomic: Optional[str] = None
    five_transcript: Optional[str] = None
    three_transcript: Optional[str] = None
    genome_build: str = "GRCh38"


def parse_fusion_input(value: str) -> ParsedFusionInput:
    """Parse plain or position-aware fusion input.

    Supported compact forms:
      - GENE1::GENE2
      - GENE1::GENE2,five_exon,three_exon
      - GENE1::GENE2,five_exon,three_exon,five_tx,three_tx
    """
    row = next(csv.reader(StringIO(value.strip())))
    if not row:
        raise ValueError("Fusion input cannot be blank.")
    fields = [field.strip() for field in row]
    fusion = fields[0]
    five_gene, three_gene = split_fusion(fusion)
    if not five_gene or not three_gene:
        raise ValueError(f"Could not parse fusion partners from: {value}")
    canonical_fusion = f"{five_gene}::{three_gene}"
    return ParsedFusionInput(
        raw=value,
        fusion=canonical_fusion,
        five_gene=five_gene,
        three_gene=three_gene,
        five_exon=fields[1] if len(fields) > 1 and fields[1] else None,
        three_exon=fields[2] if len(fields) > 2 and fields[2] else None,
        five_transcript=fields[3] if len(fields) > 3 and fields[3] else None,
        three_transcript=fields[4] if len(fields) > 4 and fields[4] else None,
    )


def parse_fusion_inputs(values: Iterable[str]) -> list[ParsedFusionInput]:
    parsed = []
    for value in values:
        stripped = value.strip()
        if not stripped:
            continue
        parsed.append(parse_fusion_input(stripped))
    return parsed


def input_position_context(parsed: ParsedFusionInput, error: Optional[str] = None) -> FusionPositionContext:
    return FusionPositionContext(
        fusion=parsed.fusion,
        five_prime=FusionPartnerContext(
            gene=parsed.five_gene,
            side="five_prime",
            transcript_id=parsed.five_transcript,
            exon=parsed.five_exon,
            genomic_breakpoint=parsed.five_genomic,
        ),
        three_prime=FusionPartnerContext(
            gene=parsed.three_gene,
            side="three_prime",
            transcript_id=parsed.three_transcript,
            exon=parsed.three_exon,
            genomic_breakpoint=parsed.three_genomic,
        ),
        source="input",
        error=error,
    )


def _int_or_none(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _batch_request_item(parsed: ParsedFusionInput) -> dict:
    return {
        "five_gene": parsed.five_gene,
        "three_gene": parsed.three_gene,
        "five_exon": _int_or_none(parsed.five_exon),
        "three_exon": _int_or_none(parsed.three_exon),
        "five_genomic": parsed.five_genomic,
        "three_genomic": parsed.three_genomic,
        "five_transcript": parsed.five_transcript,
        "three_transcript": parsed.three_transcript,
        "genome_build": parsed.genome_build,
    }


def _breakpoint_genomic(partner: dict) -> Optional[str]:
    breakpoint = partner.get("breakpoint") or {}
    position = breakpoint.get("genomic_position")
    structure = partner.get("structure") or {}
    chrom = structure.get("chrom")
    if position is None or not chrom:
        return None
    return f"{chrom}:{position}"


def _domain_label(domain: dict) -> str:
    name = str(domain.get("name") or domain.get("accession") or "domain")
    start = domain.get("start")
    end = domain.get("end")
    if start is not None and end is not None:
        return f"{name} ({start}-{end})"
    return name


def _domains_by_gene(result: dict, gene: str, status: str) -> list[str]:
    domains = result.get("interface", {}).get("domains") or []
    labels = []
    for domain in domains:
        if str(domain.get("gene", "")).upper() != gene.upper():
            continue
        if str(domain.get("status", "")).upper() != status:
            continue
        labels.append(_domain_label(domain))
    return list(dict.fromkeys(labels))


def _is_kinase_domain(domain: dict) -> bool:
    text = " ".join(
        str(domain.get(key, ""))
        for key in ("name", "type", "accession")
    ).lower()
    return "kinase" in text


def _kinase_signal(result: dict, five_gene: str, three_gene: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    domains = result.get("interface", {}).get("domains") or []
    kinase_domains = [domain for domain in domains if _is_kinase_domain(domain)]
    if not kinase_domains:
        return None, None, None

    status_rank = {"RETAINED": 3, "DISRUPTED": 2, "LOST": 1}
    kinase_domains.sort(key=lambda domain: status_rank.get(str(domain.get("status", "")).upper(), 0), reverse=True)
    domain = kinase_domains[0]
    gene = str(domain.get("gene", "") or "")
    side = None
    if gene.upper() == five_gene.upper():
        side = "five_prime"
    elif gene.upper() == three_gene.upper():
        side = "three_prime"
    status = str(domain.get("status", "")).lower() or "unknown"
    if status not in {"retained", "lost", "disrupted"}:
        status = "unknown"
    return gene or None, side, status


def _partner_context(parsed: ParsedFusionInput, result: dict, side: str) -> FusionPartnerContext:
    resolved_side = "five" if side == "five_prime" else "three"
    partner = result.get("resolved", {}).get(resolved_side) or {}
    iface = result.get("interface", {}) or {}
    gene = parsed.five_gene if side == "five_prime" else parsed.three_gene
    protein_key = "five_last_aa" if side == "five_prime" else "three_first_aa"
    breakpoint = partner.get("breakpoint") or {}
    context = breakpoint.get("context") or {}
    cds_coord = breakpoint.get("cds_coord")
    return FusionPartnerContext(
        gene=str(partner.get("gene") or gene),
        side=side,
        transcript_id=partner.get("transcript"),
        exon=str(context.get("exon_rank")) if context.get("exon_rank") is not None else None,
        genomic_breakpoint=_breakpoint_genomic(partner),
        transcript_breakpoint=f"c.{cds_coord}" if cds_coord is not None else None,
        protein_breakpoint=f"p.{iface.get(protein_key)}" if iface.get(protein_key) is not None else None,
        retained_domains=_domains_by_gene(result, gene, "RETAINED"),
        lost_domains=_domains_by_gene(result, gene, "LOST"),
        disrupted_domains=_domains_by_gene(result, gene, "DISRUPTED"),
    )


def _treatment_knowledge(result: dict) -> Optional[FusionTreatmentKnowledge]:
    knowledge = result.get("knowledge")
    if not isinstance(knowledge, dict):
        return None
    return FusionTreatmentKnowledge(
        oncogenic=knowledge.get("oncogenic"),
        therapies=list(knowledge.get("therapies") or []),
        evidence=list(knowledge.get("evidence") or []),
        diseases=list(knowledge.get("diseases") or []),
        sources=list(knowledge.get("sources") or []),
    )


def genome_nexus_position_context(parsed: ParsedFusionInput, item_result: dict) -> FusionPositionContext:
    error = item_result.get("error")
    result = item_result.get("result")
    if error or not isinstance(result, dict):
        return input_position_context(parsed, error=error or "fusion-annotation service returned no annotation result.")

    kinase_gene, kinase_side, kinase_status = _kinase_signal(
        result,
        parsed.five_gene,
        parsed.three_gene,
    )
    return FusionPositionContext(
        fusion=parsed.fusion,
        five_prime=_partner_context(parsed, result, "five_prime"),
        three_prime=_partner_context(parsed, result, "three_prime"),
        kinase_gene=kinase_gene,
        kinase_gene_side=kinase_side,
        kinase_domain_status=kinase_status,
        knowledge=_treatment_knowledge(result),
        source="genome_nexus",
        error=None,
    )


def parsed_input_from_fields(
    fusion: str,
    *,
    five_exon: Optional[int] = None,
    three_exon: Optional[int] = None,
    five_genomic: Optional[str] = None,
    three_genomic: Optional[str] = None,
    five_transcript: Optional[str] = None,
    three_transcript: Optional[str] = None,
) -> ParsedFusionInput:
    """Build a ParsedFusionInput directly from structured fields (e.g. FusionInput),
    bypassing the compact CSV-string format that parse_fusion_input() supports —
    that format has no slot for genomic breakpoints."""
    five_gene, three_gene = split_fusion(fusion)
    if not five_gene or not three_gene:
        raise ValueError(f"Could not parse fusion partners from: {fusion}")
    return ParsedFusionInput(
        raw=fusion,
        fusion=f"{five_gene}::{three_gene}",
        five_gene=five_gene,
        three_gene=three_gene,
        five_exon=str(five_exon) if five_exon is not None else None,
        three_exon=str(three_exon) if three_exon is not None else None,
        five_genomic=five_genomic,
        three_genomic=three_genomic,
        five_transcript=five_transcript,
        three_transcript=three_transcript,
    )


async def annotate_fusion_position_contexts(parsed_inputs: list[ParsedFusionInput]) -> list[FusionPositionContext]:
    """Return fusion-event context from already-parsed input plus optional
    fusion-annotation-service enrichment. Shared core for both the compact
    string-list entry point (annotate_fusion_positions) and structured callers
    (e.g. the on-demand /v1/fusion-context endpoint) that need genomic
    breakpoints, which the compact-string format can't carry."""
    if not parsed_inputs:
        return []

    base_url = settings.fusion_annotation_api_base_url.strip().rstrip("/")
    if not settings.fusion_annotation_api_enabled or not base_url:
        return [input_position_context(parsed) for parsed in parsed_inputs]

    payload = {"fusions": [_batch_request_item(parsed) for parsed in parsed_inputs]}
    try:
        async with httpx.AsyncClient(timeout=settings.fusion_annotation_api_timeout_seconds) as client:
            response = await client.post(f"{base_url}/api/annotate/batch", json=payload)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("fusion-annotation service lookup failed: %s", exc)
        return [
            input_position_context(parsed, error=f"fusion-annotation service lookup failed: {exc}")
            for parsed in parsed_inputs
        ]

    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        return [
            input_position_context(parsed, error="fusion-annotation service batch response did not include results.")
            for parsed in parsed_inputs
        ]

    contexts = []
    for parsed, item_result in zip(parsed_inputs, results):
        contexts.append(genome_nexus_position_context(parsed, item_result))
    if len(results) < len(parsed_inputs):
        for parsed in parsed_inputs[len(results):]:
            contexts.append(input_position_context(parsed, error="fusion-annotation service omitted this fusion from the batch response."))
    return contexts


async def annotate_fusion_positions(fusions: list[str]) -> list[FusionPositionContext]:
    """Return fusion-event context from compact string input (e.g. "EML4::ALK,13,20"),
    plus optional fusion-annotation-service enrichment."""
    return await annotate_fusion_position_contexts(parse_fusion_inputs(fusions))

