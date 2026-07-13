"""
CLI entry point for manual invocation.
Usage:
  # Annotate from a text file (one fusion per line)
  python -m src.cli --input fusions.txt --output results.json

  # Annotate from command-line args
  python -m src.cli --fusions "ANKRD13A::ACACB" "ASAP3::HNRNPR"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from src.pipeline.kinase_curation import (
    build_kinase_fusion_curation_rows,
    compare_kinase_curation_rows,
    read_kinase_fusion_curation_csv,
    write_kinase_curation_comparison_csv,
    write_kinase_fusion_curation_csv,
)
from src.pipeline.llm_client import DEFAULT_LOCAL_BACKEND, LOCAL_BACKENDS
from src.pipeline.orchestrator import run_pipeline


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agentic Cancer Gene Classification — M0 CLI"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--input",
        metavar="FILE",
        help="Path to a text file with one fusion per line (e.g. GENE1::GENE2)",
    )
    group.add_argument(
        "--fusions",
        nargs="+",
        metavar="FUSION",
        help="One or more fusion strings inline (e.g. ANKRD13A::ACACB)",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default="-",
        help="Output JSON file path. Use '-' for stdout (default).",
    )
    parser.add_argument(
        "--kinase-curation-csv",
        metavar="FILE",
        help=(
            "Write a fusion-level CSV focused on literature-curated functional kinase fusions."
        ),
    )
    parser.add_argument(
        "--kinase-truth-csv",
        metavar="FILE",
        help=(
            "Read-only CSV export of the Google Sheet source of truth to compare "
            "against the generated kinase curation rows."
        ),
    )
    parser.add_argument(
        "--kinase-comparison-csv",
        metavar="FILE",
        help=(
            "Write per-row comparison of generated kinase curation rows against "
            "--kinase-truth-csv. Defaults to <kinase-curation-csv>.comparison.csv "
            "when both CSV paths are provided."
        ),
    )
    parser.add_argument(
        "--local",
        nargs="?",
        const=DEFAULT_LOCAL_BACKEND,
        choices=LOCAL_BACKENDS,
        metavar="BACKEND",
        help=(
            "Route LLM calls through a local agent CLI instead of the Anthropic SDK. "
            f"Choices: {', '.join(LOCAL_BACKENDS)}. Defaults to {DEFAULT_LOCAL_BACKEND} "
            "when --local is provided without a backend."
        ),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.input:
        with open(args.input) as f:
            fusions = [line.strip() for line in f if line.strip()]
    else:
        fusions = args.fusions

    if not fusions:
        print("No fusions provided.", file=sys.stderr)
        sys.exit(1)

    if args.local:
        print(
            f"Local mode: LLM calls routed through `{args.local}` (no API key required).",
            file=sys.stderr,
        )

    result = asyncio.run(run_pipeline(fusions, local_backend=args.local))
    output = result.model_dump_json(indent=2)

    if args.kinase_curation_csv:
        rows = build_kinase_fusion_curation_rows(result)
        write_kinase_fusion_curation_csv(rows, args.kinase_curation_csv)
        print(
            f"Kinase fusion curation CSV written to {args.kinase_curation_csv} "
            f"({len(rows)} rows)",
            file=sys.stderr,
        )
    else:
        rows = build_kinase_fusion_curation_rows(result)

    if args.kinase_truth_csv:
        truth_rows = read_kinase_fusion_curation_csv(args.kinase_truth_csv)
        comparison = compare_kinase_curation_rows(rows, truth_rows)
        summary = comparison["summary"]
        print(
            "Kinase curation comparison: "
            f"matched={summary['matched_keys']}, "
            f"pipeline_only={summary['pipeline_only_keys']}, "
            f"truth_only={summary['truth_only_keys']}, "
            f"fusion_kinase_f1={summary['fusion_kinase_f1']:.4f}, "
            f"matched_citation_f1={summary['matched_citation_f1']:.4f}",
            file=sys.stderr,
        )

        comparison_path = args.kinase_comparison_csv
        if comparison_path is None and args.kinase_curation_csv:
            curation_path = Path(args.kinase_curation_csv)
            comparison_path = str(
                curation_path.with_name(
                    f"{curation_path.stem}.comparison{curation_path.suffix}"
                )
            )
        if comparison_path:
            write_kinase_curation_comparison_csv(comparison, comparison_path)
            print(
                f"Kinase curation comparison CSV written to {comparison_path}",
                file=sys.stderr,
            )

    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Results written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
