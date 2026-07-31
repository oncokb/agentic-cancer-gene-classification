"""
M0 pipeline orchestrator.
Coordinates normalization → DB lookups → literature retrieval → LLM synthesis
for each gene derived from an input gene/fusion list.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from src.config import settings
from src.models.schema import AnnotationResult, FusionInput, GeneAnnotation, ResolvedGene
from src.pipeline.db_lookups import check_oncokb_membership, get_msk_genie_prevalence
from src.pipeline.literature import retrieve_literature, search_recent_pubmed_pmids
from src.pipeline.llm_client import resolve_local_backend
from src.pipeline.normalization import is_fusion_input, normalize_fusions
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import build_gene_annotation, synthesize_gene_annotation

logger = logging.getLogger(__name__)


def _isoformat(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _cache_window_days(annotation: GeneAnnotation) -> tuple[int, str]:
    if annotation.in_oncokb is True:
        return settings.gene_cache_oncokb_check_days, "oncokb_cached_annotation"
    if (
        annotation.insufficient_evidence
        or annotation.evidence_support_score < settings.gene_cache_medium_support_threshold
    ):
        return settings.gene_cache_low_support_days, "low_support_or_insufficient_evidence"
    if annotation.evidence_support_score >= settings.gene_cache_high_support_threshold:
        return settings.gene_cache_high_support_days, "fresh_high_evidence_support"
    return settings.gene_cache_medium_support_days, "fresh_medium_evidence_support"


def _cached_annotation_for_request(
    annotation_payload: dict,
    fusions: List[str],
    reason: str,
    updated_at: Optional[datetime],
    last_pubmed_checked_at: Optional[datetime],
) -> GeneAnnotation:
    annotation = GeneAnnotation(**annotation_payload)
    annotation.fusions = list(dict.fromkeys(fusions))
    annotation.cache_status = "reused"
    annotation.cache_reason = reason
    annotation.cached_at = _isoformat(updated_at)
    annotation.last_pubmed_checked_at = _isoformat(last_pubmed_checked_at)
    return annotation


async def _maybe_reuse_cached_annotation(
    *,
    gene: str,
    fusions: List[str],
    tumor_type: Optional[str],
    now: datetime,
    run_store: Any = None,
    force_refresh: bool = False,
    local_mode: bool = False,
) -> Optional[GeneAnnotation]:
    if (
        not settings.gene_cache_enabled
        or run_store is None
        or force_refresh
        or local_mode
    ):
        return None

    cached = await run_store.get_gene_annotation(gene)
    if cached is None:
        return None

    annotation = GeneAnnotation(**cached["annotation"])
    updated_at = cached.get("updated_at")
    if updated_at is None:
        return None
    last_pubmed_checked_at = cached.get("last_pubmed_checked_at") or updated_at
    window_days, reason = _cache_window_days(annotation)

    if now - updated_at <= timedelta(days=window_days):
        return _cached_annotation_for_request(
            cached["annotation"],
            fusions,
            reason,
            updated_at,
            last_pubmed_checked_at,
        )

    recent_pmids = await search_recent_pubmed_pmids(
        gene,
        fusions,
        since=last_pubmed_checked_at,
        tumor_type=tumor_type,
    )
    if recent_pmids:
        logger.info(
            "Refreshing cached annotation for %s because %d recent PMID(s) were found since %s",
            gene,
            len(recent_pmids),
            _isoformat(last_pubmed_checked_at),
        )
        return None

    reused = _cached_annotation_for_request(
        cached["annotation"],
        fusions,
        "no_new_pubmed_pmids_since_last_check",
        updated_at,
        now,
    )
    await run_store.mark_gene_pubmed_checked(gene, now, reused)
    return reused


def _format_gene_identity(resolved_gene: ResolvedGene) -> Optional[str]:
    """Return concise HGNC identity context for retrieval-grounded LLM prompts."""
    if not resolved_gene.resolved:
        return None

    parts = []
    if resolved_gene.name:
        parts.append(f"HGNC name: {resolved_gene.name}")
    if resolved_gene.hgnc_id:
        parts.append(f"HGNC ID: {resolved_gene.hgnc_id}")
    if resolved_gene.locus_type:
        parts.append(f"Locus type: {resolved_gene.locus_type}")
    if resolved_gene.alias_symbols:
        aliases = ", ".join(resolved_gene.alias_symbols[:8])
        parts.append(f"Accepted aliases: {aliases}")
    return "; ".join(parts) if parts else None


async def _annotate_gene(
    gene: str,
    fusions: List[str],
    resolved_gene: ResolvedGene,
    unresolvable: bool,
    tumor_type: Optional[str] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> GeneAnnotation:
    """Run the full annotation pipeline for a single gene."""
    if unresolvable:
        logger.info("Gene %s is unresolvable (bare Ensembl / unannotated locus)", gene)
        return GeneAnnotation(
            gene=gene,
            fusions=list(dict.fromkeys(fusions)),
            in_oncokb=False,
            cancer_associated=None,
            insufficient_evidence=True,
            evidence_support_score=0.0,
            evidence_support_explanation=(
                "Evidence support score 0.00: the gene symbol was unresolvable, "
                "so no literature-grounded annotation was generated."
            ),
            cache_status="bypassed",
            cache_reason="unresolvable_gene",
            error="Unresolvable gene symbol — bare Ensembl ID or unannotated locus",
        )

    # Run DB lookup and literature retrieval concurrently
    oncokb_membership, (records, retrieval_tier) = await asyncio.gather(
        check_oncokb_membership(gene),
        retrieve_literature(
            gene, fusions,
            tumor_type=tumor_type,
            local_mode=local_mode,
            local_backend=local_backend,
        ),
    )

    prevalence = get_msk_genie_prevalence(gene)
    gene_identity = _format_gene_identity(resolved_gene)

    # Citation selection pass: filter broad retrieval corpus down to the
    # most directly relevant papers before synthesis to improve precision
    # without shrinking the recall pool.
    selected_records = await select_papers_for_synthesis(
        gene,
        records,
        settings.max_papers_for_synthesis,
        gene_identity=gene_identity,
        local_mode=local_mode,
        local_backend=local_backend,
    )

    try:
        synthesis = await synthesize_gene_annotation(
            gene=gene,
            fusions=fusions,
            in_oncokb=oncokb_membership,
            cancer_type_prevalence=prevalence,
            records=selected_records,
            retrieval_tier=retrieval_tier,
            gene_identity=gene_identity,
            local_mode=local_mode,
            local_backend=local_backend,
        )
    except Exception as e:
        logger.error("Synthesis failed for gene %s: %s", gene, e)
        return GeneAnnotation(
            gene=gene,
            fusions=list(dict.fromkeys(fusions)),
            in_oncokb=oncokb_membership,
            retrieval_count=len(records),
            insufficient_evidence=True,
            evidence_support_score=0.0,
            evidence_support_explanation=(
                "Evidence support score 0.00: synthesis failed, so no "
                "literature-grounded annotation was generated."
            ),
            cache_status="bypassed",
            cache_reason="synthesis_error",
            error=f"Synthesis error: {e}",
        )

    return build_gene_annotation(
        gene=gene,
        fusions=fusions,
        in_oncokb=oncokb_membership,
        cancer_type_prevalence=prevalence,
        records=records,       # full count for retrieval_count field
        synthesis_result=synthesis,
    )


def _normalize_fusion_inputs(
    fusions: Union[List[str], List[FusionInput]],
) -> tuple[List[str], Dict[str, str]]:
    """
    Accept either plain strings or FusionInput objects.
    Returns (input_strings, tumor_type_by_input_string).
    """
    input_strings: List[str] = []
    tumor_type_map: Dict[str, str] = {}

    for item in fusions:
        if isinstance(item, str):
            input_strings.append(item)
        else:
            input_strings.append(item.fusion)
            if item.tumor_type:
                tumor_type_map[item.fusion] = item.tumor_type

    return input_strings, tumor_type_map


async def run_pipeline(
    fusions: Union[List[str], List[FusionInput]],
    local_mode: bool = False,
    local_backend: Optional[str] = None,
    run_store: Any = None,
    force_refresh: bool = False,
) -> AnnotationResult:
    """
    Main entry point: accepts a list of gene/fusion strings (or FusionInput objects) and returns
    a structured AnnotationResult with one GeneAnnotation per gene.
    """
    run_id = str(uuid.uuid4())
    started_at = datetime.now(timezone.utc)
    timestamp = started_at.isoformat()
    local_backend = resolve_local_backend(local_mode=local_mode, local_backend=local_backend)
    local_mode = local_backend is not None

    input_strings, tumor_type_by_input = _normalize_fusion_inputs(fusions)
    logger.info("Pipeline run %s started — %d inputs", run_id, len(input_strings))

    gene_map = await normalize_fusions(input_strings)
    logger.info("Resolved %d unique genes from %d inputs", len(gene_map), len(input_strings))

    # Build per-gene tumor_type: first non-null tumor_type from the gene's submitted inputs.
    gene_tumor_type: Dict[str, Optional[str]] = {}
    for canonical, (_, gene_inputs) in gene_map.items():
        for f in gene_inputs:
            tt = tumor_type_by_input.get(f)
            if tt:
                gene_tumor_type[canonical] = tt
                break

    # Annotate all genes; run sequentially to respect rate limits
    # (PubMed: 3 req/s without key; LLM calls are already async within each gene)
    annotations: List[GeneAnnotation] = []
    for canonical, (resolved_gene, gene_inputs) in gene_map.items():
        associated_fusions = [value for value in gene_inputs if is_fusion_input(value)]
        tumor_type = gene_tumor_type.get(canonical)
        annotation = await _maybe_reuse_cached_annotation(
            gene=canonical,
            fusions=associated_fusions,
            tumor_type=tumor_type,
            now=started_at,
            run_store=run_store,
            force_refresh=force_refresh,
            local_mode=local_mode,
        )
        if annotation is None:
            annotation = await _annotate_gene(
                gene=canonical,
                fusions=associated_fusions,
                resolved_gene=resolved_gene,
                unresolvable=resolved_gene.unresolvable,
                tumor_type=tumor_type,
                local_mode=local_mode,
                local_backend=local_backend,
            )
            if force_refresh and annotation.cache_status == "refreshed":
                annotation.cache_reason = "force_refresh"
            elif annotation.cache_status == "refreshed":
                annotation.cache_reason = annotation.cache_reason or "cache_miss_or_stale"

            if (
                run_store is not None
                and settings.gene_cache_enabled
                and not local_mode
                and annotation.error is None
            ):
                annotation.cached_at = timestamp
                annotation.last_pubmed_checked_at = timestamp
                try:
                    await run_store.save_gene_annotation(annotation, started_at)
                except Exception:
                    logger.exception("Failed to persist cached gene annotation for %s", canonical)

        annotations.append(annotation)
        logger.info(
            "Annotated %s — cancer_associated=%s, citations=%d, evidence_support_score=%.2f",
            canonical,
            annotation.cancer_associated,
            len(annotation.citations),
            annotation.evidence_support_score,
        )

    annotations.sort(key=lambda a: a.gene)

    return AnnotationResult(
        run_id=run_id,
        timestamp=timestamp,
        fusions_processed=len(input_strings),
        genes_annotated=len(annotations),
        annotations=annotations,
    )
