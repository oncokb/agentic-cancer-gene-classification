"""
Gene symbol normalization via the HGNC and Ensembl REST APIs.
Splits fusion inputs into partner genes and resolves each symbol to its
canonical HUGO identifier. Ensembl IDs are batch-resolved when possible;
unannotated loci are routed to the insufficient-evidence path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Dict, Iterable, Optional, Set, Tuple

import httpx

from src.models.schema import ResolvedGene
from src.pipeline.cache import cached_call

logger = logging.getLogger(__name__)

HGNC_FETCH_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"
HGNC_SEARCH_URL = "https://rest.genenames.org/search/symbol/{symbol}"
ENSEMBL_LOOKUP_URL = "https://rest.ensembl.org/lookup/id"
ENSEMBL_PATTERN = re.compile(r"^ENSG\d+", re.IGNORECASE)
HGNC_ID_PATTERN = re.compile(r"Acc:(HGNC:\d+)")
HGNC_TIMEOUT_SECONDS = 5.0
ENSEMBL_TIMEOUT_SECONDS = 8.0
NORMALIZATION_CONCURRENCY = 6


# Separators used in fusion notation.
FUSION_SEPARATORS = re.compile(r"[:]{2}|--|/")


def split_fusion(fusion: str) -> Tuple[str, Optional[str]]:
    """
    Split 'GENE1::GENE2' (or '--' / '/' delimited) into partner symbols.
    Returns (gene1, gene2). If no separator found, returns (fusion, None).
    """
    parts = FUSION_SEPARATORS.split(fusion.strip(), maxsplit=1)
    if len(parts) == 2:
        return parts[0].strip(), parts[1].strip()
    return parts[0].strip(), None


def is_fusion_input(value: str) -> bool:
    """Return true when an input contains two non-empty fusion partners."""
    gene1, gene2 = split_fusion(value)
    return bool(gene1 and gene2)


# Fusion-like notation that split_fusion deliberately does not parse. A single
# hyphen is not a separator because real symbols and aliases contain one
# (HLA-A, NKX2-1, GM-CSF).
_SYMBOL_TOKEN = r"[A-Za-z][A-Za-z0-9]{1,14}"
_HYPHEN_PAIR = re.compile(rf"^({_SYMBOL_TOKEN})-({_SYMBOL_TOKEN})$")
_WHITESPACE_PAIR = re.compile(rf"^({_SYMBOL_TOKEN})\s+({_SYMBOL_TOKEN})(?:\s+fusion)?$", re.IGNORECASE)
# HGNC families whose hyphenated members look like two symbols (HLA-DRB1, MT-CO1).
_HYPHENATED_SYMBOL_PREFIXES = frozenset({"HLA", "MT"})
# Words (and protein changes like V600E) that follow a gene without naming a partner.
_NON_PARTNER_WORDS = frozenset(
    {"FUSION", "FUSIONS", "GENE", "GENES", "MUTATION", "MUTANT", "AMPLIFICATION", "DELETION", "WT"}
)
# Greek-letter suffixes are protein names, not partners (TNF-alpha, IFN-gamma).
_GREEK_LETTERS = frozenset(
    {
        "ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA", "ETA", "THETA", "IOTA", "KAPPA", "LAMBDA",
        "MU", "NU", "XI", "OMICRON", "PI", "RHO", "SIGMA", "TAU", "UPSILON", "PHI", "CHI", "PSI", "OMEGA",
    }
)
_PROTEIN_CHANGE = re.compile(r"^(?:[A-Z]\d+[A-Z]?|EXON\d+)$", re.IGNORECASE)
# Short words with lowercase letters (NF-kB, anti-EGFR) are not symbols.
_SHORT_LOWERCASE_WORD = re.compile(r"^(?=[A-Za-z]*[a-z])[A-Za-z]{1,5}$")
HGNC_ALIAS_SEARCH_URL = 'https://rest.genenames.org/search/alias_symbol:"{symbol}"+OR+prev_symbol:"{symbol}"'


def _looks_like_partner(token: str) -> bool:
    upper = token.upper()
    return not (
        upper in _NON_PARTNER_WORDS
        or upper in _GREEK_LETTERS
        or _PROTEIN_CHANGE.match(token)
        or _SHORT_LOWERCASE_WORD.match(token)
    )


def unparsed_fusion_warning(
    value: str,
    resolved: Optional[ResolvedGene] = None,
    known_aliases: Iterable[str] = (),
) -> Optional[str]:
    """
    Return a warning when an input looks like fusion notation that was treated
    as a single gene ('EWSR1-FLI1', 'EML4 ALK fusion'), else None.

    `resolved` is the HGNC resolution of the whole input as a single symbol.
    A hyphenated input only warns when HGNC confirmed it is not a symbol, so a
    real hyphenated gene, or one whose lookup failed, never warns; nor does one
    listed in `known_aliases` (alias/previous symbols of genes already resolved).
    The whitespace form ignores `resolved`: symbols never contain spaces, and
    HGNC's fuzzy search fallback "resolves" 'EML4 ALK fusion' to EML4.
    """
    text = value.strip()
    if is_fusion_input(text):
        return None

    match = _WHITESPACE_PAIR.match(text)
    if match is None:
        match = _HYPHEN_PAIR.match(text)
        if match is None or resolved is None or not resolved.unresolvable or resolved.lookup_failed:
            return None
        if match.group(1).upper() in _HYPHENATED_SYMBOL_PREFIXES:
            return None
        if text.upper() in {alias.upper() for alias in known_aliases}:
            return None

    gene1, gene2 = match.group(1), match.group(2)
    if not (_looks_like_partner(gene1) and _looks_like_partner(gene2)):
        return None
    return (
        f"'{text}' was treated as a single gene. "
        f"To annotate it as a fusion, write '{gene1.upper()}::{gene2.upper()}'."
    )


async def _is_hgnc_alias(symbol: str, client: httpx.AsyncClient) -> Optional[bool]:
    """True when HGNC lists `symbol` as an alias or previous symbol; None if the lookup failed."""
    url = HGNC_ALIAS_SEARCH_URL.format(symbol=symbol)

    async def _fetch() -> list:
        resp = await client.get(url, headers={"Accept": "application/json"}, timeout=HGNC_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp.json().get("response", {}).get("docs", [])

    try:
        docs = await cached_call(f"hgnc:alias:{symbol.upper()}", _fetch)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("HGNC alias lookup failed for %s: %s", symbol, exc)
        return None
    return bool(docs)


async def unparsed_fusion_warnings(
    inputs: Iterable[str],
    gene_map: Dict[str, Tuple[ResolvedGene, list[str]]],
    client: Optional[httpx.AsyncClient] = None,
) -> list[str]:
    """
    Warnings for each distinct input in `inputs`, reusing normalize_fusions' HGNC results.

    A hyphenated input that would warn gets one extra HGNC alias/previous-symbol
    lookup (only on this already-unresolved path); a match, or a failed lookup,
    suppresses the warning.
    """
    resolved_by_symbol = {rg.input_symbol: rg for rg, _ in gene_map.values()}
    known_aliases = {
        alias for rg, _ in gene_map.values() for alias in [*rg.alias_symbols, *rg.prev_symbols]
    }
    candidates: list[tuple[str, str]] = []
    for value in dict.fromkeys(inputs):
        warning = unparsed_fusion_warning(value, resolved_by_symbol.get(value.strip()), known_aliases)
        if warning:
            candidates.append((value.strip(), warning))
    if not any(_HYPHEN_PAIR.match(text) for text, _ in candidates):
        return [warning for _, warning in candidates]

    async def _keep(text: str, http: httpx.AsyncClient) -> bool:
        return not _HYPHEN_PAIR.match(text) or await _is_hgnc_alias(text, http) is False

    async def _filter(http: httpx.AsyncClient) -> list[str]:
        keeps = await asyncio.gather(*(_keep(text, http) for text, _ in candidates))
        return [warning for (_, warning), keep in zip(candidates, keeps) if keep]

    if client is not None:
        return await _filter(client)
    async with httpx.AsyncClient() as http:
        return await _filter(http)


def _is_ensembl_id(symbol: str) -> bool:
    return bool(ENSEMBL_PATTERN.match(symbol))


def _ensembl_lookup_id(symbol: str) -> str:
    """Strip optional stable-ID version suffix before Ensembl lookup."""
    return symbol.split(".", 1)[0]


def _unresolvable_gene(symbol: str, lookup_failed: bool = False) -> ResolvedGene:
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=symbol,
        hgnc_id=None,
        resolved=False,
        unresolvable=True,
        lookup_failed=lookup_failed,
    )


def _resolved_from_ensembl(symbol: str, doc: Optional[dict]) -> ResolvedGene:
    if not doc or doc.get("object_type") != "Gene" or not doc.get("display_name"):
        return _unresolvable_gene(symbol)

    description = doc.get("description") or ""
    hgnc_match = HGNC_ID_PATTERN.search(description)
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=doc["display_name"],
        hgnc_id=hgnc_match.group(1) if hgnc_match else None,
        resolved=True,
    )


async def _resolve_ensembl_ids(
    symbols: Iterable[str],
    client: httpx.AsyncClient,
) -> Dict[str, ResolvedGene]:
    """
    Resolve Ensembl gene IDs in one POST call.

    Ensembl can be slow, so batching avoids one network round-trip per ENSG ID
    and keeps a bounded timeout. Unresolved IDs fall back to insufficient evidence.
    """
    symbol_list = list(dict.fromkeys(symbols))
    if not symbol_list:
        return {}

    lookup_to_symbols: Dict[str, list[str]] = {}
    for symbol in symbol_list:
        lookup_to_symbols.setdefault(_ensembl_lookup_id(symbol), []).append(symbol)

    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    try:
        resp = await client.post(
            ENSEMBL_LOOKUP_URL,
            headers=headers,
            json={"ids": list(lookup_to_symbols)},
            timeout=ENSEMBL_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as exc:
        # A 400/404 for a single ID is Ensembl saying it doesn't exist; anything
        # else (timeouts, 5xx, a 4xx for a whole batch) is a failed lookup.
        confirmed_absent = (
            isinstance(exc, httpx.HTTPStatusError)
            and exc.response.status_code in (400, 404)
            and len(lookup_to_symbols) == 1
        )
        if not confirmed_absent:
            logger.warning("Ensembl lookup failed for %d IDs: %s", len(lookup_to_symbols), exc)
        return {
            symbol: _unresolvable_gene(symbol, lookup_failed=not confirmed_absent) for symbol in symbol_list
        }

    resolved: Dict[str, ResolvedGene] = {}
    for lookup_id, original_symbols in lookup_to_symbols.items():
        for symbol in original_symbols:
            resolved[symbol] = _resolved_from_ensembl(symbol, data.get(lookup_id))
    return resolved


async def resolve_gene(symbol: str, client: httpx.AsyncClient) -> ResolvedGene:
    """
    Resolve a gene symbol to its canonical HGNC entry.
    Ensembl IDs are resolved through Ensembl before falling back to insufficient evidence.
    """
    if _is_ensembl_id(symbol):
        return (await _resolve_ensembl_ids([symbol], client))[symbol]

    url = HGNC_FETCH_URL.format(symbol=symbol)
    headers = {"Accept": "application/json"}
    lookup_failed = False

    try:
        async def _fetch_primary() -> list:
            resp = await client.get(url, headers=headers, timeout=HGNC_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json().get("response", {}).get("docs", [])

        docs = await cached_call(f"hgnc:fetch:{symbol}", _fetch_primary)
        if docs:
            doc = docs[0]
            return ResolvedGene(
                input_symbol=symbol,
                canonical_symbol=doc.get("symbol", symbol),
                hgnc_id=doc.get("hgnc_id"),
                name=doc.get("name"),
                alias_symbols=doc.get("alias_symbol", []) or [],
                prev_symbols=doc.get("prev_symbol", []) or [],
                locus_type=doc.get("locus_type"),
                resolved=True,
            )
    except httpx.HTTPError as exc:
        logger.warning("HGNC fetch lookup failed for %s: %s", symbol, exc)
        lookup_failed = True

    # Try search as fallback (handles minor capitalisation differences)
    try:
        search_url = HGNC_SEARCH_URL.format(symbol=symbol)

        async def _fetch_search() -> list:
            resp = await client.get(search_url, headers=headers, timeout=HGNC_TIMEOUT_SECONDS)
            resp.raise_for_status()
            return resp.json().get("response", {}).get("docs", [])

        docs = await cached_call(f"hgnc:search:{symbol}", _fetch_search)
        if docs:
            doc = docs[0]
            return ResolvedGene(
                input_symbol=symbol,
                canonical_symbol=doc.get("symbol", symbol),
                hgnc_id=doc.get("hgnc_id"),
                name=doc.get("name"),
                alias_symbols=doc.get("alias_symbol", []) or [],
                prev_symbols=doc.get("prev_symbol", []) or [],
                locus_type=doc.get("locus_type"),
                resolved=True,
            )
    except httpx.HTTPError as exc:
        logger.warning("HGNC search lookup failed for %s: %s", symbol, exc)
        lookup_failed = True

    if lookup_failed:
        return ResolvedGene(
            input_symbol=symbol,
            canonical_symbol=symbol,
            hgnc_id=None,
            resolved=False,
            unresolvable=False,
            lookup_failed=True,
        )

    # Could not resolve — treat as unknown locus (insufficient evidence)
    return _unresolvable_gene(symbol)


async def _resolve_hgnc_symbols_concurrently(
    symbols: Iterable[str],
    client: httpx.AsyncClient,
) -> Dict[str, ResolvedGene]:
    semaphore = asyncio.Semaphore(NORMALIZATION_CONCURRENCY)

    async def resolve_one(symbol: str) -> tuple[str, ResolvedGene]:
        async with semaphore:
            return symbol, await resolve_gene(symbol, client)

    pairs = await asyncio.gather(*(resolve_one(symbol) for symbol in symbols))
    return dict(pairs)


async def normalize_fusions(
    fusions: list[str],
    *,
    strict_gene_lookup: bool = False,
) -> Dict[str, Tuple[ResolvedGene, list[str]]]:
    """
    Given a list of gene or fusion strings, return a mapping of
    canonical_symbol -> (ResolvedGene, [input strings involving this gene]).

    Each gene appears once regardless of how many inputs involve it.
    """
    gene_to_fusions: Dict[str, list[str]] = {}
    gene_symbols: Set[str] = set()

    for fusion in fusions:
        g1, g2 = split_fusion(fusion)
        for g in filter(None, [g1, g2]):
            gene_symbols.add(g)
            gene_to_fusions.setdefault(g, []).append(fusion)

    ensembl_symbols = sorted(symbol for symbol in gene_symbols if _is_ensembl_id(symbol))
    hgnc_symbols = sorted(symbol for symbol in gene_symbols if not _is_ensembl_id(symbol))

    async with httpx.AsyncClient() as client:
        resolved: Dict[str, ResolvedGene] = {}
        resolved.update(await _resolve_ensembl_ids(ensembl_symbols, client))
        resolved.update(await _resolve_hgnc_symbols_concurrently(hgnc_symbols, client))

    if strict_gene_lookup:
        # The gene query API must not classify or reuse a cached annotation
        # when HGNC failed to establish the symbol's identity.
        resolved = {
            symbol: gene.model_copy(update={"unresolvable": True}) if gene.lookup_failed else gene
            for symbol, gene in resolved.items()
        }

    result: Dict[str, Tuple[ResolvedGene, list[str]]] = {}
    for symbol, rg in resolved.items():
        key = rg.canonical_symbol or symbol
        if key not in result:
            result[key] = (rg, gene_to_fusions[symbol])
        else:
            # Merge fusion lists if same canonical symbol maps from multiple inputs
            result[key][1].extend(gene_to_fusions[symbol])

    return result
