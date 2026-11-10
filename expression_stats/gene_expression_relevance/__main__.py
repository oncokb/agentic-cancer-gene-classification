import argparse
from pathlib import Path

from .build import build
from .fetch import validate_commit


def main():
    parser = argparse.ArgumentParser(description="TCGA tumor-type enrichment artifact builder")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("build")
    command.add_argument("--datahub-commit", required=True, type=str)
    command.add_argument("--out", type=Path, default=Path("site"))
    command.add_argument("--genes", help="Comma-separated canonical symbols or unambiguous aliases")
    command.add_argument("--skip-precheck", action="store_true")
    command.add_argument("--cache", type=Path, default=Path(".cache"))
    args = parser.parse_args()
    validate_commit(args.datahub_commit)
    build(
        args.datahub_commit,
        args.out,
        args.cache,
        args.genes.split(",") if args.genes else None,
        args.skip_precheck,
    )


if __name__ == "__main__":
    main()
