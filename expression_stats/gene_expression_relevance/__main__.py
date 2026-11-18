import argparse
import sys
import time
from pathlib import Path

_PROCESS_STARTED = time.monotonic()

from . import build as build_mod  # noqa: E402
from .build import build  # noqa: E402
from .fetch import validate_commit  # noqa: E402
from .oncokb import OncoKBBudgetExceeded  # noqa: E402
from .precheck import PrecheckBudgetExceeded  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description="TCGA tumor-type enrichment artifact builder")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("build")
    command.add_argument("--datahub-commit", required=True, type=str)
    command.add_argument("--out", type=Path, default=Path("site"))
    command.add_argument("--genes", help="Comma-separated canonical symbols or unambiguous aliases")
    command.add_argument("--skip-precheck", action="store_true")
    command.add_argument("--cache", type=Path, default=Path(".cache"))
    command.add_argument(
        "--skip-alterations",
        action="store_true",
        help="Omit alteration statistics (review-only, never publishable)",
    )
    command.add_argument(
        "--hotspot-only",
        action="store_true",
        help="Skip OncoKB calls and use hotspot/TSG fallback only (review-only, never publishable)",
    )
    command.add_argument(
        "--delete-alteration-downloads",
        action="store_true",
        help="Delete each raw mutation/CNA/SV download once its summary is cached (CI disk)",
    )
    args = parser.parse_args()
    validate_commit(args.datahub_commit)
    if build is build_mod.build:
        build_mod._CLI_PROCESS_STARTED = _PROCESS_STARTED
    try:
        build(
            args.datahub_commit,
            args.out,
            args.cache,
            args.genes.split(",") if args.genes else None,
            args.skip_precheck,
            skip_alterations=args.skip_alterations,
            hotspot_only=args.hotspot_only,
            delete_downloads=args.delete_alteration_downloads,
        )
    except (OncoKBBudgetExceeded, PrecheckBudgetExceeded) as exc:
        print(f"Build stopped cleanly at deadline: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from None
    finally:
        build_mod._CLI_PROCESS_STARTED = None


if __name__ == "__main__":
    main()
