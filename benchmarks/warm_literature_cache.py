"""Warm Tier 1 PubMed literature cache for upcoming benchmark or annotation runs."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import List

from benchmarks.run_benchmark import DEFAULT_HOLDOUT, _get_fusions_from_holdout, load_holdout
from src.logging_utils import install_secret_redaction_filter
from src.pipeline.literature_warmup import warm_literature_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
install_secret_redaction_filter()
logger = logging.getLogger(__name__)


def _inputs_from_args(args: argparse.Namespace) -> List[str]:
    if args.fusions:
        return args.fusions
    holdout = load_holdout(args.holdout)
    if args.max_genes:
        holdout = holdout[: args.max_genes]
    return _get_fusions_from_holdout(holdout)


def main() -> None:
    parser = argparse.ArgumentParser(description="Warm Tier 1 PubMed literature cache")
    parser.add_argument(
        "--fusions",
        nargs="+",
        help="Gene or fusion strings to warm. Defaults to the holdout fusions.",
    )
    parser.add_argument(
        "--holdout",
        type=Path,
        default=DEFAULT_HOLDOUT,
        help="Holdout JSONL used when --fusions is omitted.",
    )
    parser.add_argument(
        "--max-genes",
        type=int,
        default=None,
        help="Limit holdout genes before extracting fusions.",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="Number of genes to warm concurrently. Defaults to ANNOTATION_GENE_CONCURRENCY.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional JSON report path.",
    )
    args = parser.parse_args()

    inputs = _inputs_from_args(args)
    logger.info("Warming literature cache for %d inputs", len(inputs))
    report = asyncio.run(warm_literature_cache(inputs, concurrency=args.concurrency))
    logger.info(
        "Warmed %d/%d genes in %.2f ms",
        report["genes_warmed"],
        report["genes_total"],
        report["timings_ms"]["total"],
    )
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2)
        logger.info("Warmup report written to %s", args.output)
    else:
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
