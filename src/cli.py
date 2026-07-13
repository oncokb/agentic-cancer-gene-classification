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

from src.pipeline.kinase_curation import (
    build_kinase_fusion_curation_rows,
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

    if args.output == "-":
        print(output)
    else:
        with open(args.output, "w") as f:
            f.write(output)
        print(f"Results written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
