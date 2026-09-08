"""Explicit OpenEvidence supplementary-evidence cache warmup for likely upcoming annotation runs.

Mirrors src.pipeline.literature_warmup's pattern for the PubMed Tier 1 cache,
but warms OpenEvidenceClient's Redis-backed cache (see src.pipeline.cache and
src.pipeline.openevidence) instead. Runs at its OWN configurable concurrency
(OPENEVIDENCE_WARMUP_CONCURRENCY), independent of ANNOTATION_GENE_CONCURRENCY
— an offline warmup pass is not live annotation traffic and can safely fan
out wider than the semaphore that gates concurrent per-gene annotation
requests.
"""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Dict, List, Optional, Union

from src.config import settings
from src.models.schema import FusionInput
from src.pipeline.normalization import is_fusion_input, normalize_fusions
from src.pipeline.openevidence import OpenEvidenceClient


def _elapsed_ms(start: float) -> float:
    return round((perf_counter() - start) * 1000, 2)


def _normalize_inputs(
    inputs: Union[List[str], List[FusionInput]],
) -> tuple[List[str], Dict[str, str]]:
    input_strings: List[str] = []
    tumor_type_map: Dict[str, str] = {}
    for item in inputs:
        if isinstance(item, str):
            input_strings.append(item)
        else:
            input_strings.append(item.fusion)
            if item.tumor_type:
                tumor_type_map[item.fusion] = item.tumor_type
    return input_strings, tumor_type_map


async def warm_openevidence_cache(
    inputs: Union[List[str], List[FusionInput]],
    *,
    concurrency: Optional[int] = None,
    client: Optional[OpenEvidenceClient] = None,
) -> dict:
    """
    Pre-fetch OpenEvidence supplementary-evidence analyses into the shared
    Redis cache for genes derived from `inputs`, ahead of live annotation
    traffic.

    Idempotent: OpenEvidenceClient.get_gene_analysis is itself backed by
    cached_call (see src.pipeline.cache), so re-running this against genes
    that are already warm is a cache hit — no duplicate live HTTP call.

    Mirrors orchestrator.py's annotate_one() in deriving each gene's
    associated fusion (if any) from its own raw `gene_inputs` and passing it
    into get_gene_analysis as `fusion`. This matters because
    get_gene_analysis's cache key does not vary by fusion (see
    openevidence.py's _cache_key) — the cached entry is keyed only by
    gene/tumor_type/model, so it holds whatever QUESTION was actually asked
    when it was populated. If warmup asked the generic question here while a
    live annotation request for the same gene asks the fusion-specific
    question, the live request would get a cache HIT on warmup's
    generic-question answer and never actually ask (or cache) the
    fusion-specific one. Deriving the same fusion context here that
    annotate_one() would derive keeps the two paths asking, and therefore
    caching, the same question for the same gene.
    """
    total_start = perf_counter()
    input_strings, tumor_type_by_input = _normalize_inputs(inputs)
    gene_map = await normalize_fusions(input_strings)
    semaphore = asyncio.Semaphore(concurrency or max(1, settings.openevidence_warmup_concurrency))
    oe_client = client or OpenEvidenceClient()

    async def warm_one(gene: str, gene_inputs: List[str]) -> dict:
        tumor_type = next(
            (tumor_type_by_input[value] for value in gene_inputs if value in tumor_type_by_input),
            None,
        )
        associated_fusions = [value for value in gene_inputs if is_fusion_input(value)]
        fusion = associated_fusions[0] if associated_fusions else None
        start = perf_counter()
        async with semaphore:
            analysis = await oe_client.get_gene_analysis(gene, tumor_type=tumor_type, fusion=fusion)
        return {
            "gene": gene,
            "citation_count": len(analysis.citations),
            "timings_ms": {"openevidence_warmup": _elapsed_ms(start)},
        }

    results = await asyncio.gather(
        *[
            warm_one(gene, gene_inputs)
            for gene, (_resolved_gene, gene_inputs) in gene_map.items()
        ],
        return_exceptions=True,
    )

    warmed = []
    errors = []
    for gene, result in zip(gene_map.keys(), results):
        if isinstance(result, Exception):
            errors.append({"gene": gene, "error": str(result)})
        else:
            warmed.append(result)

    warmed.sort(key=lambda item: item["gene"])
    return {
        "inputs_processed": len(input_strings),
        "genes_total": len(gene_map),
        "genes_warmed": len(warmed),
        "genes_failed": len(errors),
        "warmed": warmed,
        "errors": errors,
        "timings_ms": {"total": _elapsed_ms(total_start)},
    }
