"""
Compare baseline-like and optimized latency profiles on the same input set.

This runs the current pipeline twice with different runtime settings:
- baseline-like: sequential genes, broad Tier 1 retrieval, selection LLM enabled
- optimized: configured concurrency, staged Tier 1 retrieval, and selection skipping

Example:
  python -m benchmarks.compare_latency --fusions "TP53::BRAF" "ETV6::NTRK3" --mode core
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from src.config import settings
from src.models.schema import AnnotationMode, AnnotationResult
from src.pipeline.orchestrator import run_pipeline


@contextmanager
def temporary_settings(values: Dict[str, Any]):
    previous = {key: getattr(settings, key) for key in values}
    try:
        for key, value in values.items():
            setattr(settings, key, value)
        yield
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


def load_inputs(args: argparse.Namespace) -> list[str]:
    if args.input:
        with open(args.input) as f:
            return [line.strip() for line in f if line.strip()]
    return args.fusions


def summarize_result(result: AnnotationResult) -> Dict[str, Any]:
    gene_timings = [
        {
            "gene": annotation.gene,
            "total_ms": annotation.timings_ms.get("total", 0.0),
            "literature_retrieval_ms": annotation.timings_ms.get("literature_retrieval", 0.0),
            "paper_selection_ms": annotation.timings_ms.get("paper_selection", 0.0),
            "synthesis_ms": annotation.timings_ms.get("synthesis", 0.0),
            "retrieval_count": annotation.retrieval_count,
            "cache_status": annotation.cache_status,
        }
        for annotation in result.annotations
    ]
    return {
        "run_id": result.run_id,
        "fusions_processed": result.fusions_processed,
        "genes_annotated": result.genes_annotated,
        "timings_ms": result.timings_ms,
        "gene_timings": gene_timings,
    }


def savings(before_ms: float, after_ms: float) -> Optional[float]:
    if before_ms <= 0:
        return None
    return round((before_ms - after_ms) / before_ms * 100, 2)


def build_report(baseline: AnnotationResult, optimized: AnnotationResult) -> Dict[str, Any]:
    baseline_summary = summarize_result(baseline)
    optimized_summary = summarize_result(optimized)
    baseline_total = baseline.timings_ms.get("total", 0.0)
    optimized_total = optimized.timings_ms.get("total", 0.0)
    return {
        "baseline": baseline_summary,
        "optimized": optimized_summary,
        "savings": {
            "total_ms": round(baseline_total - optimized_total, 2),
            "total_pct": savings(baseline_total, optimized_total),
            "baseline_total_ms": baseline_total,
            "optimized_total_ms": optimized_total,
        },
    }


async def run_comparison(
    inputs: Iterable[str],
    *,
    local_backend: Optional[str] = None,
    mode: AnnotationMode = "full",
) -> Dict[str, Any]:
    inputs = list(inputs)
    optimized_settings = {
        "annotation_gene_concurrency": settings.annotation_gene_concurrency,
        "llm_concurrency": settings.llm_concurrency,
        "pubmed_staged_retrieval": settings.pubmed_staged_retrieval,
        "selection_llm_threshold": settings.selection_llm_threshold,
    }
    baseline_settings = {
        "annotation_gene_concurrency": 1,
        "llm_concurrency": 1,
        "pubmed_staged_retrieval": False,
        "selection_llm_threshold": 1_000_000,
    }

    with temporary_settings(baseline_settings):
        baseline = await run_pipeline(
            inputs,
            local_backend=local_backend,
            force_refresh=True,
            mode=mode,
        )

    with temporary_settings(optimized_settings):
        optimized = await run_pipeline(
            inputs,
            local_backend=local_backend,
            force_refresh=True,
            mode=mode,
        )

    return build_report(baseline, optimized)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare annotation latency before/after optimization knobs.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Text file with one input per line.")
    source.add_argument("--fusions", nargs="+", help="Gene/fusion strings to annotate.")
    parser.add_argument("--local-backend", choices=("claude-code", "codex", "antigravity"))
    parser.add_argument("--mode", choices=("full", "core"), default="full")
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(
        run_comparison(
            load_inputs(args),
            local_backend=args.local_backend,
            mode=args.mode,
        )
    )
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered)
    print(rendered)


if __name__ == "__main__":
    main()
