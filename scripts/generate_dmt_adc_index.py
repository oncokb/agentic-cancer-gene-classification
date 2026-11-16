"""Regenerate the reviewable DMT annotation from a curator CSV export.

Usage: python3 scripts/generate_dmt_adc_index.py CSV --hgnc HGNC_TSV
The HGNC TSV must match expression_stats/gene_expression_relevance/hgnc-pin.json.
"""

import argparse
import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path


TISSUES = {
    "Bladder": ["BLCA"], "Breast": ["BRCA"], "CRC": ["COADREAD"],
    "GI": ["STAD", "ESCA"], "Chol/gall": ["CHOL"], "HCC": ["LIHC"],
    "Pancreas": ["PAAD"], "RCC": ["KIRC", "KIRP", "KICH"],
    "HeadAndNeck": ["HNSC"], "Prostate": ["PRAD"], "Glioma": ["GBM", "LGG"],
    "Sarcoma": ["SARC"], "GCT": ["TGCT"], "GYN": ["UCEC", "UCS", "OV", "CESC"],
    "Ovarian": ["OV"], "Uterine": ["UCEC", "UCS"], "Cervical": ["CESC"],
    "Melanoma": ["SKCM", "UVM"], "NSCLC": ["LUAD", "LUSC"],
    "Lung": ["LUAD", "LUSC"], "Lung/solid?": ["LUAD", "LUSC"], "SCLC": [],
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--hgnc", required=True, type=Path)
    parser.add_argument("--output", type=Path, default=Path("src/expression_context/dmt_adc_index.json"))
    args = parser.parse_args()
    pin = json.loads(Path("expression_stats/gene_expression_relevance/hgnc-pin.json").read_text())
    digest = hashlib.sha256(args.hgnc.read_bytes()).hexdigest()
    if digest != pin["sha256"]:
        parser.error("HGNC TSV does not match the pinned snapshot")
    canonical = set()
    candidates = defaultdict(set)
    with args.hgnc.open(newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row["status"] != "Approved" or not row["entrez_id"].isdigit():
                continue
            symbol = row["symbol"].upper()
            canonical.add(symbol)
            for alias in (row["alias_symbol"] + "|" + row["prev_symbol"]).split("|"):
                if alias:
                    candidates[alias.upper()].add(symbol)
    aliases = {alias: next(iter(targets)) for alias, targets in candidates.items()
               if len(targets) == 1 and alias not in canonical}
    genes = defaultdict(list)
    unresolved = set()
    with args.csv.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        raw = (row["Gene"] or "").strip().upper()
        # A slash denotes two targets; each receives the curator's row.
        symbols = raw.split("/")
        for symbol in symbols:
            resolved = symbol if symbol in canonical else aliases.get(symbol)
            if not resolved and len(symbols) == 1:
                other = (row.get("Other_name") or "").strip().upper()
                resolved = other if other in canonical else aliases.get(other)
            if not resolved:
                unresolved.add(symbol)
                continue
            tissue = (row["Tissue type"] or "").strip()
            if tissue not in TISSUES:
                parser.error(f"Unmapped tissue: {tissue}")
            genes[resolved].append({
                "tissue_type": tissue, "tcga_codes": TISSUES[tissue],
                "from": (row["From…"] or "").strip(),
                "notes": "; ".join(part for part in (
                    f"Type: {row['Type'].strip()}" if row.get("Type") else "",
                    (row["Notes"] or "").strip(),
                ) if part),
            })
    result = {
        "version": 1,
        "source": {"name": "DMT ADC Compiled List", "csv_sha256": hashlib.sha256(args.csv.read_bytes()).hexdigest(),
                   "hgnc_snapshot_sha256": digest, "rows": len(rows), "uncertain_tissues": ["Lung/solid?"],
                   "unresolved_genes": sorted(unresolved)},
        "tissue_to_tcga": TISSUES,
        "genes": dict(sorted(genes.items())),
    }
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
    print(f"{len(rows)} rows, {len(genes)} resolved genes; unresolved: {sorted(unresolved)}")


if __name__ == "__main__":
    main()
