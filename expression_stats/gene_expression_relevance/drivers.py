"""Pinned driver-gene sources for the Point Mutation and Copy Number Alteration tracks.

A mutation source supplies the gene universe in which point mutations can be called
putative drivers, and which of those genes have a loss-of-function (tumor suppressor)
role. A copy-number source supplies the genes whose amplification / deep deletion can be
called enriched, and per gene which direction(s) its role allows. Every list is a
committed file verified against a pinned SHA-256 at load; the source names, combined and
per-file hashes are recorded in the manifest and hashed into the release identity.

Sources:
- "bailey2018": Bailey et al. 2018 Table S1 (299 genes; see drivers-pin.json).
- "bailey2018+new_framework": Bailey plus curator-annotated genes from the pinned
  New_Framework lists (curated-lists-pin.json). For mutations, the curated Point Mutation
  genes absent from Bailey are added (Bailey genes keep their Bailey rules unchanged).
- "bailey2018+new_framework+sanchezvega2018" (copy number): Bailey genes, curated Copy
  Number Alteration genes, and Sanchez-Vega et al. 2018 pathway-template genes that the
  paper lists as recurrent GISTIC targets in their role's direction
  (sanchezvega2018-pin.json).
"""

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).parent
AMP, DEL = "amp", "del"
OG, TSG = "OG", "TSG"
# Curated role labels (the "role" column of the derived TSVs) as a set of roles.
CURATED_ROLES = {"OG": {OG}, "TSG": {TSG}, "OG/TSG": {OG, TSG}, "unknown": set()}


@dataclass(frozen=True)
class DriverSource:
    name: str
    sha256: str
    genes: frozenset  # upper-case symbols as published by the source
    tumor_suppressors: frozenset  # subset of genes where truncating mutations count
    files: dict = field(default_factory=dict, compare=False)  # file name -> SHA-256

    def pin(self):
        pin = dict(mutation_driver_source=self.name, mutation_driver_sha256=self.sha256)
        if self.files:
            pin["mutation_driver_files"] = dict(self.files)
        return pin


@dataclass(frozen=True)
class CnaDriverSource:
    """Copy-number driver genes with role-derived allowed directions.

    An oncogene-only gene may be called enriched for amplification only, a tumor
    suppressor-only gene for deep deletion only; a gene with both roles or no known role
    allows both directions.
    """

    name: str
    sha256: str
    genes: frozenset
    oncogenes: frozenset
    tumor_suppressors: frozenset
    files: dict = field(default_factory=dict, compare=False)

    def directions(self, symbol):
        if symbol not in self.genes:
            return ()
        og, tsg = symbol in self.oncogenes, symbol in self.tumor_suppressors
        if og and not tsg:
            return (AMP,)
        if tsg and not og:
            return (DEL,)
        return (AMP, DEL)

    def pin(self):
        return dict(
            cna_driver_source=self.name,
            cna_driver_sha256=self.sha256,
            cna_driver_files=dict(self.files),
        )


def driver_pin():
    return json.loads((ROOT / "drivers-pin.json").read_text())


def curated_pin():
    return json.loads((ROOT / "curated-lists-pin.json").read_text())


def _verified(path, expected, label):
    data = Path(path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if sha != expected:
        raise ValueError(f"{label} SHA-256 mismatch for {Path(path).name}; update the reviewed pin")
    return data, sha


def _combined(files):
    """One digest over (file name, SHA-256) pairs in name order."""
    payload = json.dumps(sorted(files.items()), separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def bailey_rows(path=None):
    pin = driver_pin()
    path = Path(path) if path else ROOT / pin["file"]
    data, sha = _verified(path, pin["sha256"], "Driver list")
    rows = list(csv.DictReader(data.decode().splitlines(), delimiter="\t"))
    return pin, path, sha, rows


def _bailey_roles(rows):
    """(genes, oncogenes, tumor suppressors): any row's 20/20+ role counts."""
    genes = frozenset(row["gene"].strip().upper() for row in rows if row["gene"].strip())
    tsg, og = set(), set()
    for row in rows:
        gene, label = row["gene"].strip().upper(), row["predicted_role_20_20_plus"].strip().lower()
        if not gene:
            continue
        if "tsg" in label:
            tsg.add(gene)
        elif "oncogene" in label:
            og.add(gene)
    if not genes or not tsg <= genes:
        raise ValueError("Malformed driver list")
    return genes, frozenset(og), frozenset(tsg)


def load_bailey2018(path=None):
    """Bailey et al. Cell 2018 Table S1: the 299-gene global union, any tissue row.

    A gene is a tumor suppressor if any of its rows is predicted "tsg" or "possible tsg"
    by 20/20+. Tissue-specific rows are deliberately pooled: selecting the cohort's own
    list would bake the expected answer into the enrichment test.
    """
    pin, _, sha, rows = bailey_rows(path)
    genes, _, tsg = _bailey_roles(rows)
    return DriverSource(name=pin["source"], sha256=sha, genes=genes, tumor_suppressors=tsg)


def load_curated(kind, path=None):
    """{symbol: set of roles} from a pinned New_Framework-derived TSV, plus its file pin."""
    pin = curated_pin()["lists"][kind]
    path = Path(path) if path else ROOT / pin["file"]
    data, sha = _verified(path, pin["sha256"], "Curated gene list")
    rows = list(csv.DictReader(data.decode().splitlines(), delimiter="\t"))
    genes = {}
    for row in rows:
        gene, role = row["gene"].strip().upper(), row["role"].strip()
        if not gene or role not in CURATED_ROLES or gene in genes:
            raise ValueError(f"Malformed curated {kind} list row: {row}")
        genes[gene] = frozenset(CURATED_ROLES[role])
    if len(genes) != pin["genes"]:
        raise ValueError(f"Curated {kind} list has {len(genes)} genes, pin says {pin['genes']}")
    return genes, {path.name: sha}


def sanchezvega_pin():
    return json.loads((ROOT / "sanchezvega2018-pin.json").read_text())


def load_sanchezvega2018_cna(path=None):
    """{symbol: set of roles} for pathway genes that are recurrent GISTIC targets.

    OG genes need a non-empty "GISTIC amp", TSG genes a non-empty "GISTIC del", genes with
    another label (NA / Unknown / blank: no role) either. Roles of repeated genes combine.
    """
    pin = sanchezvega_pin()
    path = Path(path) if path else ROOT / pin["file"]
    data, sha = _verified(path, pin["sha256"], "Pathway gene list")
    genes = {}
    for row in csv.DictReader(data.decode().splitlines(), delimiter="\t"):
        gene, label = row["gene"].strip().upper(), row["og_tsg"].strip()
        amp, deletion = row["gistic_amp"].strip(" ;"), row["gistic_del"].strip(" ;")
        role = {"OG": {OG}, "TSG": {TSG}}.get(label, set())
        recurrent = amp if label == "OG" else deletion if label == "TSG" else amp or deletion
        if gene and recurrent:
            genes[gene] = genes.get(gene, frozenset()) | frozenset(role)
    if not genes:
        raise ValueError("Malformed pathway gene list")
    return genes, {path.name: sha}


def load_bailey2018_new_framework():
    """Bailey 299 plus curated Point Mutation genes absent from Bailey.

    Added genes count truncating mutations only when the curated role includes TSG;
    otherwise (OG or unknown role) only recurrent missense / in-frame changes count.
    """
    pin, path, bailey_sha, rows = bailey_rows()
    genes, _, tsg = _bailey_roles(rows)
    curated, files = load_curated("mutation")
    overlap = genes & set(curated)
    if overlap:
        raise ValueError(f"Curated mutation additions already in Bailey: {sorted(overlap)}")
    files = {path.name: bailey_sha, **files}
    return DriverSource(
        name=f"{pin['source']}+{curated_pin()['source']}",
        sha256=_combined(files),
        genes=genes | frozenset(curated),
        tumor_suppressors=tsg | frozenset(g for g, roles in curated.items() if TSG in roles),
        files=files,
    )


def load_cna_drivers():
    """Copy-number drivers: Bailey (20/20+ roles), curated CNA genes (curated roles) and
    recurrent-GISTIC Sanchez-Vega pathway genes (paper roles); roles are unioned per gene.
    """
    pin, path, bailey_sha, rows = bailey_rows()
    genes, og, tsg = _bailey_roles(rows)
    curated, curated_files = load_curated("cna")
    pathways, pathway_files = load_sanchezvega2018_cna()
    og, tsg = set(og), set(tsg)
    for extra in (curated, pathways):
        og |= {g for g, roles in extra.items() if OG in roles}
        tsg |= {g for g, roles in extra.items() if TSG in roles}
    files = {path.name: bailey_sha, **curated_files, **pathway_files}
    return CnaDriverSource(
        name="+".join((pin["source"], curated_pin()["source"], sanchezvega_pin()["source"])),
        sha256=_combined(files),
        genes=genes | frozenset(curated) | frozenset(pathways),
        oncogenes=frozenset(og),
        tumor_suppressors=frozenset(tsg),
        files=files,
    )


SOURCES = {
    "bailey2018": load_bailey2018,
    "bailey2018+new_framework": load_bailey2018_new_framework,
}
CNA_SOURCES = {"bailey2018+new_framework+sanchezvega2018": load_cna_drivers}


def load(name):
    if name not in SOURCES:
        raise ValueError(f"Unknown mutation driver source {name!r}")
    return SOURCES[name]()


def load_cna(name):
    if name not in CNA_SOURCES:
        raise ValueError(f"Unknown copy-number driver source {name!r}")
    return CNA_SOURCES[name]()
