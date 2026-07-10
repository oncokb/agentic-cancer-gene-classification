"""
M0 pipeline orchestrator.
Coordinates normalization → DB lookups → literature retrieval → LLM synthesis
for each gene derived from an input fusion list.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from src.config import settings
from src.models.schema import AnnotationResult, GeneAnnotation, ResolvedGene
from src.pipeline.db_lookups import check_oncokb_membership, get_msk_genie_prevalence
from src.pipeline.literature import retrieve_literature
from src.pipeline.llm_client import resolve_local_backend
from src.pipeline.normalization import normalize_fusions
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import build_gene_annotation, synthesize_gene_annotation

logger = logging.getLogger(__name__)


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
            confidence=0.0,
            error="Unresolvable gene symbol — bare Ensembl ID or unannotated locus",
        )

    # Run DB lookup and literature retrieval concurrently
    oncokb_membership, (records, retrieval_tier) = await asyncio.gather(
        check_oncokb_membership(gene),
        retrieve_literature(gene, fusions, local_mode=local_mode, local_backend=local_backend),
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
            confidence=0.0,
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


async def run_pipeline(
    fusions: List[str],
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> AnnotationResult:
    """
    Main entry point: accepts a list of fusion strings and returns
    a structured AnnotationResult with one GeneAnnotation per gene.
    """
    run_id = str(uuid.uuid4())
    timestamp = datetime.now(timezone.utc).isoformat()
    local_backend = resolve_local_backend(local_mode=local_mode, local_backend=local_backend)
    local_mode = local_backend is not None
    logger.info("Pipeline run %s started — %d fusions", run_id, len(fusions))

    gene_map = await normalize_fusions(fusions)
    logger.info("Resolved %d unique genes from %d fusions", len(gene_map), len(fusions))

    # Annotate all genes; run sequentially to respect rate limits
    # (PubMed: 3 req/s without key; LLM calls are already async within each gene)
    annotations: List[GeneAnnotation] = []
    for canonical, (resolved_gene, gene_fusions) in gene_map.items():
        annotation = await _annotate_gene(
            gene=canonical,
            fusions=gene_fusions,
            resolved_gene=resolved_gene,
            unresolvable=resolved_gene.unresolvable,
            local_mode=local_mode,
            local_backend=local_backend,
        )
        annotations.append(annotation)
        logger.info(
            "Annotated %s — cancer_associated=%s, citations=%d, confidence=%.2f",
            canonical,
            annotation.cancer_associated,
            len(annotation.citations),
            annotation.confidence,
        )

    annotations.sort(key=lambda a: a.gene)

    return AnnotationResult(
        run_id=run_id,
        timestamp=timestamp,
        fusions_processed=len(fusions),
        genes_annotated=len(annotations),
        annotations=annotations,
    )
