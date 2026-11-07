"""Entrez joins, HGNC canonical symbols and ambiguity-safe aliases."""

import re

import pandas as pd

from . import http
from .fetch import digest
from .provenance import hgnc_pin


def hgnc_path(cache):
    return cache / "hgnc" / (hgnc_pin()["sha256"] + ".tsv")


def hgnc(cache):
    pin = hgnc_pin()
    target = hgnc_path(cache)
    if not target.exists():
        http.download(pin["url"], target)
    if digest(target) != pin["sha256"]:
        target.unlink(missing_ok=True)
        raise ValueError(
            "HGNC snapshot SHA-256 mismatch; update the reviewed pin or restore its snapshot"
        )
    table = pd.read_csv(target, sep="\t", dtype=str).fillna("")
    symbols, candidates = {}, {}
    for row in table.to_dict("records"):
        if row["status"] != "Approved" or not row["entrez_id"].isdigit():
            continue
        symbol = row["symbol"].upper()
        symbols[int(row["entrez_id"])] = symbol
        for alias in (row["alias_symbol"] + "|" + row["prev_symbol"]).split("|"):
            if alias:
                candidates.setdefault(alias.upper(), set()).add(symbol)
    canonical = set(symbols.values())
    aliases = {
        alias: next(iter(values))
        for alias, values in candidates.items()
        if len(values) == 1 and alias not in canonical and re.fullmatch(r"[A-Z0-9_.-]+", alias)
    }
    return symbols, aliases


def gene_catalog(paths, canonical):
    candidates = {}
    for path in paths:
        frame = pd.read_csv(path, sep="\t", usecols=["Hugo_Symbol", "Entrez_Gene_Id"], dtype=str)
        for symbol, entrez in frame.itertuples(index=False, name=None):
            if pd.notna(entrez) and str(entrez).isdigit() and int(entrez) > 0:
                candidates.setdefault(int(entrez), set()).add(str(symbol).upper())
    resolved = {}
    for entrez, observed in candidates.items():
        symbol = canonical.get(entrez)
        if not symbol and len(observed) == 1:
            symbol = next(iter(observed))
        if symbol and re.fullmatch(r"[A-Z0-9_.-]+", symbol) and symbol != "NAN":
            resolved[entrez] = symbol
    # Refuse ambiguous symbol -> multiple Entrez mappings (never overwrite artifacts).
    counts = pd.Series(list(resolved.values())).value_counts()
    return {entrez: symbol for entrez, symbol in resolved.items() if counts[symbol] == 1}
