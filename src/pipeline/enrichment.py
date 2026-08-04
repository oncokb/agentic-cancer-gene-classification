"""Lazy enrichment for core annotations.

This module intentionally stays off the primary annotation path. It accepts
already-returned core annotations and expands them with full synthesis fields
such as supporting quotes, pathway/class context, prevalence, and a fuller
summary.
"""

from __future__ import annotations

import asyncio
import inspect
from time import perf_counter
from typing import Awaitable, Callable, Dict, List, Optional, Union

from src.config import settings
from src.models.schema import GeneAnnotation
from src.pipeline.db_lookups import check_oncokb_membership, get_msk_genie_prevalence
from src.pipeline.literature import retrieve_literature
from src.pipeline.llm_client import resolve_local_backend
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import build_gene_annotation, synthesize_gene_annotation

EnrichmentProgressCallback = Callable[[GeneAnnotation], Union[Awaitable[None], None]]


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000, 2)


async def _maybe_call_progress(
    callback: Optional[EnrichmentProgressCallback],
    annotation: GeneAnnotation,
) -> None:
    if callback is None:
        return
    result = callback(annotation)
    if inspect.isawaitable(result):
        await result


def _merge_enriched_annotation(
    original: GeneAnnotation,
    enriched: GeneAnnotation,
) -> GeneAnnotation:
    """Preserve request/cache identity while replacing lazy fields."""
    enriched.fusions = list(dict.fromkeys(original.fusions or enriched.fusions))
    enriched.cache_status = original.cache_status
    enriched.cache_reason = original.cache_reason
    enriched.cached_at = original.cached_at
    enriched.last_pubmed_checked_at = original.last_pubmed_checked_at
    return enriched


async def enrich_gene_annotation(
    annotation: GeneAnnotation,
    *,
    local_backend: Optional[str] = None,
) -> GeneAnnotation:
    """Run full lazy enrichment for a single already-returned annotation."""
    total_start = perf_counter()
    timings: Dict[str, float] = {}
    local_backend = resolve_local_backend(local_backend=local_backend)
    local_mode = local_backend is not None

    retrieval_start = perf_counter()
    records, retrieval_tier = await retrieve_literature(
        annotation.gene,
        annotation.fusions,
        local_mode=local_mode,
        local_backend=local_backend,
    )
    timings["literature_retrieval"] = _elapsed_ms(retrieval_start)

    in_oncokb = annotation.in_oncokb
    if in_oncokb is None:
        oncokb_start = perf_counter()
        in_oncokb = await check_oncokb_membership(annotation.gene)
        timings["oncokb"] = _elapsed_ms(oncokb_start)

    prevalence_start = perf_counter()
    prevalence = get_msk_genie_prevalence(annotation.gene)
    timings["prevalence"] = _elapsed_ms(prevalence_start)

    selection_start = perf_counter()
    selected_records = await select_papers_for_synthesis(
        annotation.gene,
        records,
        settings.max_papers_for_synthesis,
        local_mode=local_mode,
        local_backend=local_backend,
    )
    timings["paper_selection"] = _elapsed_ms(selection_start)

    synthesis_start = perf_counter()
    synthesis = await synthesize_gene_annotation(
        gene=annotation.gene,
        fusions=annotation.fusions,
        in_oncokb=in_oncokb,
        cancer_type_prevalence=prevalence,
        records=selected_records,
        retrieval_tier=retrieval_tier,
        local_mode=local_mode,
        local_backend=local_backend,
        mode="full",
    )
    timings["synthesis"] = _elapsed_ms(synthesis_start)

    enriched = build_gene_annotation(
        gene=annotation.gene,
        fusions=annotation.fusions,
        in_oncokb=in_oncokb,
        cancer_type_prevalence=prevalence,
        records=records,
        synthesis_result=synthesis,
        retrieval_tier=retrieval_tier,
        mode="full",
    )
    enriched = _merge_enriched_annotation(annotation, enriched)
    timings["total"] = _elapsed_ms(total_start)
    enriched.timings_ms = timings
    return enriched


async def enrich_gene_annotations(
    annotations: List[GeneAnnotation],
    *,
    local_backend: Optional[str] = None,
    on_annotation: Optional[EnrichmentProgressCallback] = None,
) -> List[GeneAnnotation]:
    """Enrich annotations concurrently and return them sorted by gene."""
    semaphore = asyncio.Semaphore(max(1, settings.annotation_gene_concurrency))

    async def enrich_one(annotation: GeneAnnotation) -> GeneAnnotation:
        async with semaphore:
            enriched = await enrich_gene_annotation(
                annotation,
                local_backend=local_backend,
            )
        await _maybe_call_progress(on_annotation, enriched)
        return enriched

    tasks = [asyncio.create_task(enrich_one(annotation)) for annotation in annotations]
    enriched = [await task for task in asyncio.as_completed(tasks)]
    enriched.sort(key=lambda item: item.gene)
    return enriched
