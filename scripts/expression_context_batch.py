"""Submit an expression-context gene batch, poll until complete, and download all three CSV exports.

Usage:
    ACGC_API_KEY=... python3 scripts/expression_context_batch.py --base-url URL --genes FILE --out-dir DIR
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

import httpx

PREFIX = "/v1/expression-context"


def parse_genes_file(path: Path) -> List[str]:
    text = path.read_text(encoding="utf-8-sig")
    genes: List[str] = []
    is_delimited = path.suffix.lower() in (".csv", ".tsv")
    for idx, raw_line in enumerate(text.splitlines()):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if is_delimited or "\t" in line or "," in line:
            delim = "\t" if "\t" in line else ","
            tokens = [line.split(delim, 1)[0].strip().strip("\"'").strip()]
        else:
            tokens = [t.strip() for t in re.split(r"[\s,;]+", line) if t.strip()]
        for token in tokens:
            if not token:
                continue
            if idx == 0 and token.lower() in (
                "gene",
                "genes",
                "hugo_symbol",
                "symbol",
                "gene_symbol",
            ):
                continue
            genes.append(token)
    return genes


def run_batch_cli(
    *,
    base_url: str,
    genes_file: Path,
    out_dir: Path,
    api_key: Optional[str] = None,
    poll_interval: float = 2.0,
    max_polls: int = 1800,
    include_excluded: bool = False,
    transport: Optional[httpx.BaseTransport] = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Dict[str, Path]:
    key = (api_key if api_key is not None else os.environ.get("ACGC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("ACGC_API_KEY environment variable is required.")

    genes = parse_genes_file(genes_file)
    if not genes:
        raise ValueError(f"No genes found in {genes_file}")

    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
    }
    base = base_url.rstrip("/")

    with httpx.Client(base_url=base, headers=headers, timeout=60.0, transport=transport) as client:
        submit_resp = client.post(f"{PREFIX}/batches", json={"genes": genes})
        if submit_resp.status_code != 202:
            raise RuntimeError(
                f"Batch submission failed with HTTP {submit_resp.status_code}"
            )
        accepted = submit_resp.json()
        batch_id = accepted["batch_id"]
        status_url = accepted.get("status_url") or f"{PREFIX}/batches/{batch_id}"

        status_data = None
        for _ in range(max_polls):
            poll_resp = client.get(status_url)
            if poll_resp.status_code != 200:
                raise RuntimeError(
                    f"Polling batch {batch_id} failed with HTTP {poll_resp.status_code}"
                )
            status_data = poll_resp.json()
            state = status_data.get("status")
            if state in ("done", "failed", "cancelled"):
                break
            sleep_fn(poll_interval)
        else:
            raise RuntimeError(f"Timed out waiting for batch {batch_id} to complete")

        if status_data and status_data.get("status") in ("failed", "cancelled"):
            raise RuntimeError(
                f"Batch {batch_id} ended with status {status_data.get('status')!r}"
            )

        out_dir.mkdir(parents=True, exist_ok=True)
        exports = {
            "per_pair": (
                f"{PREFIX}/batches/{batch_id}/export.csv"
                + ("?include_excluded=true" if include_excluded else ""),
                out_dir / "export.csv",
            ),
            "golden_set": (
                f"{PREFIX}/batches/{batch_id}/golden-set.csv",
                out_dir / "golden-set.csv",
            ),
            "curator_sheet": (
                f"{PREFIX}/batches/{batch_id}/curator-sheet.csv",
                out_dir / "curator-sheet.csv",
            ),
        }
        written: Dict[str, Path] = {}
        for name, (url, dest) in exports.items():
            dl_resp = client.get(url, headers={"Accept": "text/csv"})
            if dl_resp.status_code != 200:
                raise RuntimeError(
                    f"Downloading {name} CSV failed with HTTP {dl_resp.status_code}"
                )
            dest.write_bytes(dl_resp.content)
            written[name] = dest

    return written


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="ACGC server base URL")
    parser.add_argument(
        "--genes", required=True, type=Path, help="Path to gene list file"
    )
    parser.add_argument(
        "--out-dir", required=True, type=Path, help="Output directory for downloaded CSVs"
    )
    parser.add_argument(
        "--poll-interval", type=float, default=2.0, help="Seconds between status polls"
    )
    parser.add_argument(
        "--include-excluded",
        action="store_true",
        help="Include excluded cancer types in per-pair export.csv",
    )
    args = parser.parse_args(argv)
    try:
        written = run_batch_cli(
            base_url=args.base_url,
            genes_file=args.genes,
            out_dir=args.out_dir,
            poll_interval=args.poll_interval,
            include_excluded=args.include_excluded,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(
        f"Downloaded {len(written)} CSV exports to {args.out_dir}: "
        + ", ".join(str(p.name) for p in written.values())
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
