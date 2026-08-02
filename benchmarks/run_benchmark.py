"""
M0 Validation Harness.

Compares the annotation engine's output against Nicole's holdout labels.
Scores are computed per field type as specified in the design doc:
  - Categorical fields: Cohen's kappa + per-class F1
  - Citations: set precision / recall / F1
  - Gene summary: LLM-as-a-judge (0–4), skippable with --no-judge

Usage:
  # Run pipeline on holdout, then evaluate
  python -m benchmarks.run_benchmark

  # Load existing pipeline output (skip pipeline re-run)
  python -m benchmarks.run_benchmark --results path/to/results.json

  # Skip the LLM judge step (no API calls for summary scoring)
  python -m benchmarks.run_benchmark --no-judge

  # Use a different holdout file
  python -m benchmarks.run_benchmark --holdout path/to/holdout.jsonl

  # Save full results to file
  python -m benchmarks.run_benchmark --output report.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from src.logging_utils import install_secret_redaction_filter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
install_secret_redaction_filter()
logger = logging.getLogger(__name__)

DEFAULT_HOLDOUT = Path(__file__).parent / "data" / "holdout.jsonl"
BenchmarkRoute = Literal["direct", "local"]


def load_holdout(path: Path) -> List[Dict]:
    records = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _get_fusions_from_holdout(holdout: List[Dict]) -> List[str]:
    """Extract unique fusion strings from the holdout set."""
    seen = set()
    fusions = []
    for record in holdout:
        for fusion in record.get("fusions", []):
            if fusion and fusion not in seen:
                seen.add(fusion)
                fusions.append(fusion)
    return fusions


async def _run_pipeline(
    fusions: List[str],
    local_backend: Optional[str] = None,
    mode: str = "full",
) -> Dict:
    from src.pipeline.orchestrator import run_pipeline
    result = await run_pipeline(fusions, local_backend=local_backend, mode=mode)
    return result.model_dump()


class _BenchmarkRunStore:
    """No-op route store so local route benchmarks do not require MySQL."""

    async def save_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_run(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def save_gene_annotation(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def get_gene_annotation(self, *args: Any, **kwargs: Any) -> None:
        return None

    async def mark_gene_pubmed_checked(self, *args: Any, **kwargs: Any) -> None:
        return None


async def _run_pipeline_via_local_route(
    fusions: List[str],
    local_backend: Optional[str] = None,
    mode: str = "full",
) -> Dict:
    """Exercise the FastAPI /v1/annotate route without requiring a server process."""
    import httpx
    from src.main import app

    app.state.run_store = _BenchmarkRunStore()
    payload: Dict[str, Any] = {"fusions": fusions, "mode": mode, "force_refresh": True}
    if local_backend:
        payload["local_backend"] = local_backend
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post("/v1/annotate", json=payload, timeout=None)
    response.raise_for_status()
    return response.json()


def _align_predictions(
    holdout: List[Dict],
    pipeline_result: Dict,
) -> tuple[List[Dict], List[Dict]]:
    """
    Align pipeline output to holdout by gene symbol.
    Returns (aligned_predictions, aligned_ground_truth).
    Genes in the holdout but missing from pipeline output get an empty prediction.
    """
    pred_by_gene: Dict[str, Dict] = {
        a["gene"]: a for a in pipeline_result.get("annotations", [])
    }

    aligned_pred = []
    aligned_gold = []
    for gt in holdout:
        gene = gt["gene"]
        pred = pred_by_gene.get(gene, {"gene": gene, "citations": []})
        aligned_pred.append(pred)
        aligned_gold.append(gt)

    return aligned_pred, aligned_gold


def build_per_gene_report(
    predictions: List[Dict],
    ground_truth: List[Dict],
) -> List[Dict]:
    """Build per-gene deltas to debug citation and tier tradeoffs."""
    from benchmarks.metrics import citation_scores

    rows = []
    for pred, gold in zip(predictions, ground_truth):
        pred_citations = set(pred.get("citations", []))
        gold_citations = set(gold.get("citations", []))
        precision, recall, f1 = citation_scores(
            list(pred_citations),
            list(gold_citations),
        )
        rows.append(
            {
                "gene": gold["gene"],
                "in_oncokb": pred.get("in_oncokb"),
                "retrieval_count": pred.get("retrieval_count", 0),
                "pred_cancer_associated": pred.get("cancer_associated"),
                "gold_cancer_associated": gold.get("cancer_associated"),
                "citation_precision": round(precision, 4),
                "citation_recall": round(recall, 4),
                "citation_f1": round(f1, 4),
                "citation_tp": sorted(pred_citations & gold_citations),
                "citation_fp": sorted(pred_citations - gold_citations),
                "citation_fn": sorted(gold_citations - pred_citations),
                "pred_citations": sorted(pred_citations),
                "gold_citations": sorted(gold_citations),
            }
        )
    return rows


def print_report(metrics: Dict, judge_results: Optional[Dict] = None) -> None:
    n = metrics["n"]
    print(f"\n{'='*60}")
    print(f"  M0 Benchmark Report  ({n} genes evaluated)")
    print(f"{'='*60}")

    ca = metrics["cancer_associated"]
    print("\n--- cancer_associated ---")
    print(f"  Accuracy:     {ca['accuracy']:.3f}")
    print(f"  Cohen's κ:    {ca['cohen_kappa']:.3f}  (>0.6 = substantial, >0.8 = near-perfect)")

    cites = metrics["citations"]
    print("\n--- citations (set-based) ---")
    print(f"  Precision:    {cites['precision']:.3f}")
    print(f"  Recall:       {cites['recall']:.3f}")
    print(f"  F1:           {cites['f1']:.3f}")

    if judge_results:
        agg = judge_results["aggregate"]
        print("\n--- gene_summary (LLM-as-a-judge, 0–4 scale) ---")
        if agg.get("mean_score") is not None:
            print(f"  Mean score:   {agg['mean_score']:.2f}/4.0  ({agg['mean_pct']:.1f}%)")
            print(f"  Excellent (≥3): {agg['excellent_pct']:.1f}%")
            print(f"  Acceptable (≥2): {agg['acceptable_pct']:.1f}%")
            print(f"  N evaluated:  {agg['n_evaluated']}")
            print("\n  Per-gene scores:")
            for pg in judge_results["per_gene"]:
                score_str = str(pg["score"]) if pg["score"] >= 0 else "ERR"
                print(f"    {pg['gene']:<15} {score_str}/4  — {pg['rationale']}")
        else:
            print("  No summaries evaluated.")

    print(f"\n{'='*60}\n")


def print_per_gene_debug(per_gene_report: List[Dict]) -> None:
    """Print compact debug rows for the largest citation misses."""
    citation_misses = [
        row
        for row in per_gene_report
        if row["citation_fp"] or row["citation_fn"]
    ]
    if not citation_misses:
        return

    citation_misses.sort(
        key=lambda row: len(row["citation_fp"]) + len(row["citation_fn"]),
        reverse=True,
    )
    print("--- per-gene debug (top citation deltas) ---")
    for row in citation_misses[:8]:
        print(
            f"  {row['gene']:<12} "
            f"cite P/R/F1={row['citation_precision']:.2f}/"
            f"{row['citation_recall']:.2f}/{row['citation_f1']:.2f}; "
            f"FP={row['citation_fp']} FN={row['citation_fn']}"
        )
    print()


async def run_benchmark(
    *,
    holdout_path: Path = DEFAULT_HOLDOUT,
    results_path: Optional[Path] = None,
    no_judge: bool = False,
    local_backend: Optional[str] = None,
    max_genes: Optional[int] = None,
    mode: str = "full",
    route: BenchmarkRoute = "direct",
) -> Dict:
    """Run or score the holdout benchmark and return the full report."""
    # --- Load holdout ---
    logger.info("Loading holdout from %s", holdout_path)
    holdout = load_holdout(holdout_path)
    if max_genes is not None:
        holdout = holdout[:max_genes]
    logger.info("Holdout: %d genes", len(holdout))

    # --- Run or load pipeline ---
    if results_path:
        logger.info("Loading existing pipeline results from %s", results_path)
        with open(results_path) as f:
            pipeline_result = json.load(f)
    else:
        fusions = _get_fusions_from_holdout(holdout)
        logger.info("Running pipeline on %d fusions from holdout via %s route...", len(fusions), route)
        if route == "local":
            pipeline_result = await _run_pipeline_via_local_route(
                fusions,
                local_backend=local_backend,
                mode=mode,
            )
        else:
            pipeline_result = await _run_pipeline(
                fusions,
                local_backend=local_backend,
                mode=mode,
            )

    # --- Align ---
    aligned_pred, aligned_gold = _align_predictions(holdout, pipeline_result)

    # --- Categorical metrics ---
    from benchmarks.metrics import compute_categorical_metrics
    metrics = compute_categorical_metrics(aligned_pred, aligned_gold)
    per_gene_report = build_per_gene_report(aligned_pred, aligned_gold)

    # --- LLM judge ---
    judge_results = None
    if not no_judge:
        from benchmarks.judge import run_judge
        genes = [g["gene"] for g in aligned_gold]
        pred_summaries = [p.get("gene_summary") for p in aligned_pred]
        gold_summaries = [g.get("gene_summary") for g in aligned_gold]
        logger.info("Running LLM-as-a-judge on %d gene summaries...", len(genes))
        judge_results = run_judge(genes, pred_summaries, gold_summaries)

    return {
        "holdout_path": str(holdout_path),
        "route": route,
        "mode": mode,
        "n_genes": len(holdout),
        "categorical_metrics": metrics,
        "per_gene_report": per_gene_report,
        "judge": judge_results,
        "pipeline_result": pipeline_result,
    }


def main() -> None:
    from src.pipeline.llm_client import DEFAULT_LOCAL_BACKEND, LOCAL_BACKENDS

    parser = argparse.ArgumentParser(description="M0 Benchmark — validate against Nicole's holdout")
    parser.add_argument(
        "--holdout",
        type=Path,
        default=DEFAULT_HOLDOUT,
        help="Path to holdout JSONL file (default: benchmarks/data/holdout.jsonl)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="Path to existing pipeline results JSON (skip re-running the pipeline)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write full benchmark report to this JSON file",
    )
    parser.add_argument(
        "--results-csv",
        type=Path,
        default=None,
        help="Write full pipeline annotation results to this CSV file",
    )
    parser.add_argument(
        "--no-judge",
        action="store_true",
        help="Skip the LLM-as-a-judge step (no API calls for summary scoring)",
    )
    parser.add_argument(
        "--mode",
        choices=("full", "core"),
        default="full",
        help="Annotation mode passed to the pipeline or API route.",
    )
    parser.add_argument(
        "--route",
        choices=("direct", "local"),
        default="direct",
        help="Use 'local' to benchmark through the in-process FastAPI /v1/annotate route.",
    )
    parser.add_argument(
        "--max-genes",
        type=int,
        default=None,
        help="Limit holdout genes for smoke/regression runs.",
    )
    parser.add_argument(
        "--local",
        nargs="?",
        const=DEFAULT_LOCAL_BACKEND,
        choices=LOCAL_BACKENDS,
        metavar="BACKEND",
        help=(
            "Route pipeline LLM calls through a local agent CLI instead of the Anthropic SDK. "
            f"Choices: {', '.join(LOCAL_BACKENDS)}. Defaults to {DEFAULT_LOCAL_BACKEND} "
            "when --local is provided without a backend. Pair with --no-judge to avoid "
            "benchmark judge API calls."
        ),
    )
    args = parser.parse_args()

    full_report = asyncio.run(
        run_benchmark(
            holdout_path=args.holdout,
            results_path=args.results,
            no_judge=args.no_judge,
            local_backend=args.local,
            max_genes=args.max_genes,
            mode=args.mode,
            route=args.route,
        )
    )

    if args.results_csv:
        from src.models.schema import AnnotationResult
        from src.pipeline.results_export import write_annotation_results_csv

        write_annotation_results_csv(
            AnnotationResult.model_validate(full_report["pipeline_result"]),
            args.results_csv,
        )
        logger.info("Pipeline results CSV written to %s", args.results_csv)

    # --- Report ---
    print_report(full_report["categorical_metrics"], full_report["judge"])
    print_per_gene_debug(full_report["per_gene_report"])

    # --- Optional JSON output ---
    if args.output:
        with open(args.output, "w") as f:
            json.dump(full_report, f, indent=2)
        logger.info("Full report written to %s", args.output)


if __name__ == "__main__":
    main()
