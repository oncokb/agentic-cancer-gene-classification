"""Explicit PubMed literature-cache warmup for likely upcoming annotation runs."""

from __future__ import annotations

import asyncio
from time import perf_counter
from typing import Dict, List, Optional, Union

from src.config import settings
from src.models.schema import FusionInput
from src.pipeline.literature import _tier1_retrieve
from src.pipeline.normalization import is_fusion_input, normalize_fusions


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


async def warm_literature_cache(
    inputs: Union[List[str], List[FusionInput]],
    *,
    concurrency: Optional[int] = None,
) -> dict:
    """
    Warm Tier 1 PubMed esearch/efetch cache entries for genes derived from inputs.

    This intentionally avoids Tier 2 agentic retrieval so cache warming does not
    spend LLM calls or elongate the user-facing annotation critical path.
    """
    total_start = perf_counter()
    input_strings, tumor_type_by_input = _normalize_inputs(inputs)
    gene_map = await normalize_fusions(input_strings)
    semaphore = asyncio.Semaphore(concurrency or max(1, settings.annotation_gene_concurrency))

    async def warm_one(gene: str, gene_inputs: List[str]) -> dict:
        associated_fusions = [value for value in gene_inputs if is_fusion_input(value)]
        tumor_type = next(
            (tumor_type_by_input[value] for value in gene_inputs if value in tumor_type_by_input),
            None,
        )
        start = perf_counter()
        async with semaphore:
            records = await _tier1_retrieve(
                gene,
                fusions=associated_fusions,
                tumor_type=tumor_type,
            )
        return {
            "gene": gene,
            "retrieval_count": len(records),
            "timings_ms": {"literature_warmup": _elapsed_ms(start)},
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
