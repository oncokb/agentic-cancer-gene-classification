"""
Compare annotation output with different synthesis-paper caps.

This is intended for the 4-vs-8 paper A/B check: retrieval still uses the same
pipeline, but `MAX_PAPERS_FOR_SYNTHESIS` is varied between arms so the team can
measure whether additional abstracts improve rationale confidence, citations,
and high-confidence clinical-actionability findings.

Example:
  python -m benchmarks.compare_synthesis_paper_counts \
    --fusions "HAPSTR1::ABAT" "EML4::ALK" \
    --paper-counts 4 8 \
    --mode full \
    --output synthesis_ab_report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from statistics import mean
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
        with open(args.input, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return args.fusions


def _avg(values: Iterable[float]) -> Optional[float]:
    values = [value for value in values if value is not None]
    return round(mean(values), 4) if values else None


def summarize_result(result: AnnotationResult, paper_count: int) -> Dict[str, Any]:
    annotations = result.annotations
    actionable = [a for a in annotations if a.clinical_actionability]
    support_scores = [a.evidence_support_score for a in annotations]
    citation_counts = [len(a.citations) for a in annotations]
    escalated = [
        any(flag.code == "deep_model_escalated" for flag in a.quality_flags)
        for a in annotations
    ]
    return {
        "paper_count": paper_count,
        "run_id": result.run_id,
        "fusions_processed": result.fusions_processed,
        "genes_annotated": result.genes_annotated,
        "timings_ms": result.timings_ms,
        "summary": {
            "avg_evidence_support_score": _avg(support_scores),
            "avg_verified_citation_count": _avg(citation_counts),
            "insufficient_evidence_count": sum(1 for a in annotations if a.insufficient_evidence),
            "clinical_actionability_count": len(actionable),
            "deep_model_escalation_count": sum(1 for value in escalated if value),
        },
        "annotations": [
            {
                "gene": annotation.gene,
                "fusions": annotation.fusions,
                "retrieval_count": annotation.retrieval_count,
                "citation_count": len(annotation.citations),
                "citations": annotation.citations,
                "evidence_support_score": annotation.evidence_support_score,
                "insufficient_evidence": annotation.insufficient_evidence,
                "clinical_actionability_score": (
                    annotation.clinical_actionability.confidence_score
                    if annotation.clinical_actionability
                    else None
                ),
                "clinical_actionability_pmids": (
                    annotation.clinical_actionability.pmids
                    if annotation.clinical_actionability
                    else []
                ),
                "deep_model_escalated": any(
                    flag.code == "deep_model_escalated" for flag in annotation.quality_flags
                ),
                "timings_ms": annotation.timings_ms,
            }
            for annotation in annotations
        ],
    }


def compare_arms(arms: list[Dict[str, Any]]) -> Dict[str, Any]:
    if len(arms) < 2:
        return {}
    baseline, candidate = arms[0], arms[-1]
    base_summary = baseline["summary"]
    candidate_summary = candidate["summary"]
    return {
        "baseline_paper_count": baseline["paper_count"],
        "candidate_paper_count": candidate["paper_count"],
        "delta_avg_evidence_support_score": (
            None
            if base_summary["avg_evidence_support_score"] is None
            or candidate_summary["avg_evidence_support_score"] is None
            else round(
                candidate_summary["avg_evidence_support_score"]
                - base_summary["avg_evidence_support_score"],
                4,
            )
        ),
        "delta_avg_verified_citation_count": (
            None
            if base_summary["avg_verified_citation_count"] is None
            or candidate_summary["avg_verified_citation_count"] is None
            else round(
                candidate_summary["avg_verified_citation_count"]
                - base_summary["avg_verified_citation_count"],
                4,
            )
        ),
        "delta_clinical_actionability_count": (
            candidate_summary["clinical_actionability_count"]
            - base_summary["clinical_actionability_count"]
        ),
        "delta_total_ms": round(
            candidate["timings_ms"].get("total", 0.0)
            - baseline["timings_ms"].get("total", 0.0),
            2,
        ),
    }


async def run_comparison(
    inputs: Iterable[str],
    *,
    paper_counts: list[int],
    local_backend: Optional[str] = None,
    mode: AnnotationMode = "full",
) -> Dict[str, Any]:
    arms = []
    input_list = list(inputs)
    for paper_count in paper_counts:
        with temporary_settings({"max_papers_for_synthesis": paper_count}):
            result = await run_pipeline(
                input_list,
                local_backend=local_backend,
                force_refresh=True,
                mode=mode,
            )
        arms.append(summarize_result(result, paper_count))

    return {
        "inputs": input_list,
        "mode": mode,
        "paper_counts": paper_counts,
        "arms": arms,
        "comparison": compare_arms(arms),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare synthesis-paper-count A/B outputs.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="Text file with one input per line.")
    source.add_argument("--fusions", nargs="+", help="Gene/fusion strings to annotate.")
    parser.add_argument("--paper-counts", nargs="+", type=int, default=[4, 8])
    parser.add_argument("--local-backend", choices=("claude-code", "codex", "antigravity"))
    parser.add_argument("--mode", choices=("full", "core"), default="full")
    parser.add_argument("--output", type=Path, help="Optional JSON report path.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report = asyncio.run(
        run_comparison(
            load_inputs(args),
            paper_counts=args.paper_counts,
            local_backend=args.local_backend,
            mode=args.mode,
        )
    )
    rendered = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
