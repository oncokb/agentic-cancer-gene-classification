"""Swappable mutation driver-gene sources for the Point Mutation classifier.

A source supplies the gene universe in which point mutations can be called putative
drivers, and which of those genes have a loss-of-function (tumor suppressor) role.
Only "bailey2018" exists today; OncoKB labels can replace it by registering another
loader here. The chosen source name and file hash are recorded in the manifest.
"""

import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).parent


@dataclass(frozen=True)
class DriverSource:
    name: str
    sha256: str
    genes: frozenset  # upper-case symbols as published by the source
    tumor_suppressors: frozenset  # subset of genes where truncating mutations count

    def pin(self):
        return dict(mutation_driver_source=self.name, mutation_driver_sha256=self.sha256)


def driver_pin():
    return json.loads((ROOT / "drivers-pin.json").read_text())


def load_bailey2018(path=None):
    """Bailey et al. Cell 2018 Table S1: the 299-gene global union, any tissue row.

    A gene is a tumor suppressor if any of its rows is predicted "tsg" or "possible tsg"
    by 20/20+. Tissue-specific rows are deliberately pooled: selecting the cohort's own
    list would bake the expected answer into the enrichment test.
    """
    pin = driver_pin()
    path = Path(path) if path else ROOT / pin["file"]
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != pin["sha256"]:
        raise ValueError(f"Driver list SHA-256 mismatch for {path.name}; update the reviewed pin")
    rows = list(csv.DictReader(data.decode().splitlines(), delimiter="\t"))
    genes = frozenset(row["gene"].strip().upper() for row in rows if row["gene"].strip())
    tsg = frozenset(
        row["gene"].strip().upper()
        for row in rows
        if "tsg" in row["predicted_role_20_20_plus"].strip().lower()
    )
    if not genes or not tsg <= genes:
        raise ValueError("Malformed driver list")
    return DriverSource(name=pin["source"], sha256=sha, genes=genes, tumor_suppressors=tsg)


SOURCES = {"bailey2018": load_bailey2018}


def load(name):
    if name not in SOURCES:
        raise ValueError(f"Unknown mutation driver source {name!r}")
    return SOURCES[name]()
