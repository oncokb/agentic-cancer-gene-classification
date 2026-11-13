"""Derive the pinned curator gene lists from a New_Framework CSV export (review tool).

Usage: python -I scripts/derive_curated_lists.py <New_Framework.csv>

The CSV is read strictly as data (stdlib csv). It writes, next to the package sources:

- new_framework_cna_genes.tsv: every gene whose "Relevant alteration type(s)" lists
  "Copy Number Alteration", with its verbatim "OG or TSG" value;
- new_framework_mutation_genes.tsv: every gene listing "Point Mutation" that is not in
  the pinned Bailey 2018 list, with its verbatim "OG or TSG" value;

and refreshes curated-lists-pin.json (file SHA-256s and the source CSV SHA-256). The
build never reads the CSV: it verifies the committed TSVs against the pin. Re-running this
script on a new export changes the pin, hence the build fingerprint and release ID.
"""

import csv
import hashlib
import io
import json
import sys
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "gene_expression_relevance"
GENE, ALTERATIONS, ROLE = "Genes", "Relevant alteration type(s)", "OG or TSG"
ROLES = {"Oncogene": "OG", "TSG": "TSG", "OG/TSG": "OG/TSG"}
LISTS = {
    "cna": ("new_framework_cna_genes.tsv", "Copy Number Alteration", False),
    "mutation": ("new_framework_mutation_genes.tsv", "Point Mutation", True),
}


def rows(path):
    text = Path(path).read_bytes()
    records = list(csv.reader(io.StringIO(text.decode("utf-8-sig"), newline="")))
    header = [h.strip() for h in records[0]]
    index = {name: header.index(name) for name in (GENE, ALTERATIONS, ROLE)}
    out = []
    # Logical CSV record numbers, header = 1 (quoted cells may span physical lines).
    for number, record in enumerate(records[1:], 2):
        cells = {k: (record[i].strip() if i < len(record) else "") for k, i in index.items()}
        if cells[GENE]:
            out.append((number, cells))
    return hashlib.sha256(text).hexdigest(), out


def bailey_genes():
    pin = json.loads((PACKAGE / "drivers-pin.json").read_text())
    with (PACKAGE / pin["file"]).open() as stream:
        return {row["gene"].strip().upper() for row in csv.DictReader(stream, delimiter="\t")}


def table(selected):
    lines = ["gene\tnew_framework_og_or_tsg\trole\tsource_record"]
    for number, cells in sorted(selected, key=lambda item: item[1][GENE].upper()):
        role = ROLES.get(cells[ROLE], "unknown")
        lines.append(f"{cells[GENE].upper()}\t{cells[ROLE]}\t{role}\t{number}")
    return "\n".join(lines) + "\n"


def main(path):
    source_sha, records = rows(path)
    bailey = bailey_genes()
    pin_path = PACKAGE / "curated-lists-pin.json"
    pin = json.loads(pin_path.read_text())
    for key, (name, label, exclude_bailey) in LISTS.items():
        selected = [
            (n, c)
            for n, c in records
            if label in c[ALTERATIONS] and not (exclude_bailey and c[GENE].upper() in bailey)
        ]
        data = table(selected).encode()
        (PACKAGE / name).write_bytes(data)
        pin["lists"][key].update(
            file=name, sha256=hashlib.sha256(data).hexdigest(), genes=len(selected)
        )
        print(f"{name}: {len(selected)} genes")
    pin["derived_from"]["sha256"] = source_sha
    pin_path.write_text(json.dumps(pin, indent=2) + "\n")


if __name__ == "__main__":
    main(sys.argv[1])
