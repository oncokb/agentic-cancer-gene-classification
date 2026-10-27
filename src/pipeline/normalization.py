"""
Gene symbol normalization via the HGNC and Ensembl REST APIs.
Splits fusion inputs into partner genes and resolves each symbol to its
canonical HUGO identifier. Ensembl gene, transcript and protein IDs (also as
GENE(ENST...) and ENST...:c.... forms) are resolved through Ensembl, and
RefSeq transcript/protein accessions (NM_..., NP_..., GENE(NM_...),
NM_...:c....) through NCBI, never HGNC search; unannotated loci are routed to
the insufficient-evidence path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

import httpx

from src.config import settings
from src.models.schema import ResolvedGene
from src.pipeline.cache import cached_call

logger = logging.getLogger(__name__)

HGNC_FETCH_URL = "https://rest.genenames.org/fetch/symbol/{symbol}"
HGNC_SEARCH_URL = "https://rest.genenames.org/search/symbol/{symbol}"
ENSEMBL_LOOKUP_URL = "https://rest.ensembl.org/lookup/id"
# NCBI Datasets gene report for an RNA or protein accession (one call, any version).
NCBI_GENE_BY_ACCESSION_URL = "https://api.ncbi.nlm.nih.gov/datasets/v2/gene/accession/{accession}"
_ENSEMBL_ID = r"ENS[GTP]\d{11}(?:\.\d+)?"
# Human Ensembl gene/transcript/protein stable ID, optionally versioned.
ENSEMBL_PATTERN = re.compile(rf"^{_ENSEMBL_ID}$", re.IGNORECASE)
# RefSeq transcript (NM_/NR_/XM_/XR_) or protein (NP_/XP_) accession, optionally versioned.
_REFSEQ_ID = r"[NX][MRP]_\d{6,9}(?:\.\d+)?"
REFSEQ_PATTERN = re.compile(rf"^{_REFSEQ_ID}$", re.IGNORECASE)
# GENE(ENST...) / GENE(NM_...): a symbol annotated with the transcript (or gene/protein) it was called on.
_DECORATED_PATTERN = re.compile(
    rf"^(?P<gene>[A-Za-z0-9][A-Za-z0-9.\-]*)\s*\(\s*(?P<id>{_ENSEMBL_ID}|{_REFSEQ_ID})\s*\)$", re.IGNORECASE
)
# HGVS on an Ensembl or RefSeq transcript/protein: ENST...:c.215C>G,
# NM_000546.6(TP53):c.215C>G, NP_000537.3:p.Arg72Pro.
_HGVS_PATTERN = re.compile(
    rf"^(?P<id>ENS[TP]\d{{11}}(?:\.\d+)?|{_REFSEQ_ID})"
    r"(?:\((?P<gene>[A-Za-z0-9][A-Za-z0-9.\-]*)\))?:(?P<variant>[cnrp]\..+)$",
    re.IGNORECASE,
)
# Contains something Ensembl-like outside the forms above (ENSG123,
# ENSMUSG00000017167, ENST...:x, 'EML4(ENST...) ALK(ENST...) fusion'):
# rejected outright, never sent to HGNC search, which would fuzzy-match it.
_ENSEMBL_LIKE_PATTERN = re.compile(r"(?<![A-Za-z0-9])ENS[A-Z]{0,4}[EGTPR]\d{3,}", re.IGNORECASE)
# Likewise RefSeq-like text (NM_12, NC_000017.11, NM_000546:x): HGNC symbols
# never contain '_', so two letters and '_' before a digit is an accession attempt.
_REFSEQ_LIKE_PATTERN = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}_\d", re.IGNORECASE)
HGNC_ID_PATTERN = re.compile(r"Acc:(HGNC:\d+)")
HGNC_TIMEOUT_SECONDS = 5.0
ENSEMBL_TIMEOUT_SECONDS = 8.0
NCBI_TIMEOUT_SECONDS = 8.0
# NCBI answers 429 when a burst outruns its per-second budget, even under the
# shared limiter (it bursts its full allowance); back off and retry a few times.
NCBI_RATE_LIMIT_RETRIES = 3
NCBI_RATE_LIMIT_BACKOFF_SECONDS = 0.5
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
_HYPHEN_PAIR = re.compile(rf"^(?P<gene1>{_SYMBOL_TOKEN})-(?P<gene2>{_SYMBOL_TOKEN})$")
# Two whitespace-separated partners only count as a fusion when the input says
# so ('EML4 ALK fusion', 'fusion EML4 ALK'); a bare 'BRAF melanoma' is a gene
# plus context, and a bare 'EML4 ALK' is an accepted miss.
_FUSION_WORD = r"(?:fusions?|rearrangements?|translocations?)"
_WHITESPACE_PAIR = re.compile(
    rf"^(?:(?P<lead>{_FUSION_WORD})\s+)?(?P<gene1>{_SYMBOL_TOKEN})\s+(?P<gene2>{_SYMBOL_TOKEN})"
    rf"(?:\s+(?P<trail>{_FUSION_WORD}))?$",
    re.IGNORECASE,
)
# HGNC families whose hyphenated members look like two symbols (HLA-DRB1, MT-CO1).
_HYPHENATED_SYMBOL_PREFIXES = frozenset({"HLA", "MT"})
# Words that accompany a gene without naming a partner: event and status words,
# Greek-letter protein names (TNF-alpha, IFN-gamma), and prose (NF-kB, anti-EGFR).
_NON_PARTNER_WORDS = frozenset(
    {
        "FUSION", "FUSIONS", "REARRANGEMENT", "REARRANGEMENTS", "TRANSLOCATION", "TRANSLOCATIONS",
        "GENE", "GENES", "PROTEIN", "VARIANT", "VARIANTS", "MUTATION", "MUTATIONS", "MUTANT", "MUTATED",
        "POSITIVE", "NEGATIVE", "POS", "NEG", "AMPLIFICATION", "AMPLIFIED", "AMP", "DELETION", "DELETED",
        "DEL", "LOSS", "GAIN", "EXPRESSION", "OVEREXPRESSION", "WILDTYPE", "WT", "NULL", "INHIBITOR",
        "ANTI", "NON", "KB", "CELL", "SMALL", "LARGE", "HIGH", "LOW",
        "ALPHA", "BETA", "GAMMA", "DELTA", "EPSILON", "ZETA", "ETA", "THETA", "IOTA", "KAPPA", "LAMBDA",
        "MU", "NU", "XI", "OMICRON", "PI", "RHO", "SIGMA", "TAU", "UPSILON", "PHI", "CHI", "PSI", "OMEGA",
    }
)
# Short English words that join hyphenated prose (in-frame, one-two); none is an HGNC symbol.
_SHORT_ENGLISH_WORDS = frozenset(
    {
        "IN", "ON", "OF", "TO", "AT", "BY", "OR", "AN", "AS", "IS", "IT", "BE", "NO", "UP", "SO", "IF",
        "WE", "ONE", "TWO", "SIX", "TEN", "AND", "THE", "FOR", "NOT", "OUT", "OFF", "END", "NEW", "OLD",
        "BUT", "ARE", "WAS", "HAS", "TOP", "PRE",
    }
)
# Single-word tumor names and abbreviations. Superset of the single-word terms in
# literature._TUMOR_TYPE_ALIASES (copied: literature imports this module; a test
# keeps them in sync).
_TUMOR_TYPE_WORDS = frozenset(
    {
        "LUAD", "LUSC", "NSCLC", "SCLC", "TNBC", "CRC", "COAD", "READ", "PDAC", "HCC", "GC", "STAD",
        "EAC", "ESCC", "CCA", "RCC", "CCRCC", "KIRC", "KIRP", "GBM", "LGG", "AML", "CML", "ALL", "CLL",
        "MM", "DLBCL", "FL", "MCL", "NHL", "HL", "OV", "UCEC", "CESC", "SKCM", "UVM", "HNSCC", "THCA",
        "PRAD", "BLCA", "UC", "MESO", "SARC", "LCNEC", "PCPG", "MDS", "MPN", "GIST", "IMT", "ALCL",
        "CANCER", "TUMOR", "TUMOUR", "NEOPLASM", "MALIGNANCY", "CARCINOMA", "ADENOCARCINOMA", "SARCOMA",
        "MELANOMA", "GLIOMA", "GLIOBLASTOMA", "LYMPHOMA", "LEUKEMIA", "LEUKAEMIA", "MYELOMA",
        "MESOTHELIOMA", "CHOLANGIOCARCINOMA", "PHEOCHROMOCYTOMA", "PARAGANGLIOMA", "NEUROBLASTOMA",
        "BLASTOMA", "LUNG", "BREAST", "COLON", "RECTAL", "COLORECTAL", "GASTRIC", "PANCREATIC",
        "THYROID", "PROSTATE", "BLADDER", "OVARIAN", "RENAL", "KIDNEY", "LIVER", "SKIN", "BRAIN",
        "ESOPHAGOGASTRIC", "ESS", "HGESS", "LGESS", "ULMS", "USARC", "UTROSCT",
    }
)
# Protein changes and exon references (V600E, G12C, T790M, Q61fs, exon19, ex20).
_PROTEIN_CHANGE = re.compile(r"^(?:[A-Z]\d+(?:[A-Z]|FS|DEL|INS|DUP)?|(?:EXON|EX)\d+)$", re.IGNORECASE)
HGNC_ALIAS_SEARCH_URL = 'https://rest.genenames.org/search/alias_symbol:"{symbol}"+OR+prev_symbol:"{symbol}"'


def _looks_like_partner(token: str) -> bool:
    upper = token.upper()
    return not (
        upper in _NON_PARTNER_WORDS
        or upper in _SHORT_ENGLISH_WORDS
        or upper in _TUMOR_TYPE_WORDS
        or _PROTEIN_CHANGE.match(token)
    )


def _has_digit(token: str) -> bool:
    return any(char.isdigit() for char in token)


def _partners_are_symbol_cased(text: str, gene1: str, gene2: str) -> bool:
    """
    Case check that keeps hyphenated prose (well-differentiated, Well-Differentiated)
    from reading as two genes. An all-lowercase input needs a digit in a partner
    (ewsr1-fli1, bcr-abl1; a bare bcr-abl is an accepted miss); otherwise every
    partner must be all-uppercase or contain a digit (EWSR1-FLI1, C11orf95-RELA).
    """
    if not any(char.isupper() for char in text):
        return _has_digit(gene1) or _has_digit(gene2)
    return all(token.isupper() or _has_digit(token) for token in (gene1, gene2))


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
    The whitespace form must name a fusion explicitly and ignores `resolved`:
    symbols never contain spaces, and HGNC's fuzzy search fallback "resolves"
    'EML4 ALK fusion' to EML4.
    """
    text = value.strip()
    if is_fusion_input(text) or has_accession_identifier(text):
        return None

    match = _WHITESPACE_PAIR.match(text)
    if match is not None:
        if not (match.group("lead") or match.group("trail")):
            return None
    else:
        match = _HYPHEN_PAIR.match(text)
        if match is None or resolved is None or not resolved.unresolvable or resolved.lookup_failed:
            return None
        if match.group("gene1").upper() in _HYPHENATED_SYMBOL_PREFIXES:
            return None
        if text.upper() in {alias.upper() for alias in known_aliases}:
            return None

    gene1, gene2 = match.group("gene1"), match.group("gene2")
    if not (_looks_like_partner(gene1) and _looks_like_partner(gene2)):
        return None
    if not _partners_are_symbol_cased(text, gene1, gene2):
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


def _is_refseq_id(symbol: str) -> bool:
    return bool(REFSEQ_PATTERN.match(symbol))


@dataclass(frozen=True)
class _AccessionToken:
    """A fusion partner (or single gene input) written with an Ensembl ID or RefSeq accession."""

    raw: str
    accession: Optional[str]  # upper-cased, version kept; None when malformed
    named_gene: Optional[str] = None  # GENE in GENE(ENST...) or ENST...(GENE):c....
    variant: Optional[str] = None  # HGVS suffix, e.g. c.215C>G
    refseq: bool = False  # RefSeq (NCBI) rather than Ensembl

    @property
    def source(self) -> str:
        return "RefSeq" if self.refseq else "Ensembl"

    @property
    def id_noun(self) -> str:
        return "accession" if self.refseq else "ID"


def _parse_accession_token(token: str) -> Optional[_AccessionToken]:
    """Parse an Ensembl- or RefSeq-form partner token; None for anything else (plain symbols)."""
    text = token.strip()
    if ENSEMBL_PATTERN.match(text) or REFSEQ_PATTERN.match(text):
        accession, named_gene, variant = text.upper(), None, None
    elif match := _DECORATED_PATTERN.match(text):
        accession, named_gene, variant = match.group("id").upper(), match.group("gene"), None
    elif match := _HGVS_PATTERN.match(text):
        accession, named_gene, variant = match.group("id").upper(), match.group("gene"), match.group("variant")
    elif _ENSEMBL_LIKE_PATTERN.search(text):
        return _AccessionToken(raw=text, accession=None)
    elif _REFSEQ_LIKE_PATTERN.search(text):
        return _AccessionToken(raw=text, accession=None, refseq=True)
    else:
        return None
    return _AccessionToken(
        raw=text, accession=accession, named_gene=named_gene, variant=variant, refseq=_is_refseq_id(accession)
    )


def has_accession_identifier(value: str) -> bool:
    """True when a gene or fusion input names a partner with an Ensembl ID or RefSeq accession (valid or not)."""
    return any(_parse_accession_token(token) for token in split_fusion(value) if token)


def _unresolvable_gene(
    symbol: str,
    lookup_failed: bool = False,
    resolution_error: Optional[str] = None,
) -> ResolvedGene:
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=symbol,
        hgnc_id=None,
        resolved=False,
        unresolvable=True,
        lookup_failed=lookup_failed,
        resolution_error=resolution_error,
    )


def _ensembl_absent(symbol: str) -> ResolvedGene:
    return _unresolvable_gene(symbol, resolution_error=f"Ensembl has no human gene, transcript or protein {symbol}.")


def _ensembl_failed(symbol: str) -> ResolvedGene:
    return _unresolvable_gene(
        symbol, lookup_failed=True, resolution_error=f"The Ensembl lookup for {symbol} failed; please retry."
    )


def _is_human(doc: dict) -> bool:
    return doc.get("species") in (None, "homo_sapiens")


def _resolved_from_ensembl(symbol: str, doc: Optional[dict]) -> ResolvedGene:
    if not doc or doc.get("object_type") != "Gene" or not doc.get("display_name") or not _is_human(doc):
        return _ensembl_absent(symbol)

    description = doc.get("description") or ""
    hgnc_match = HGNC_ID_PATTERN.search(description)
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=doc["display_name"],
        hgnc_id=hgnc_match.group(1) if hgnc_match else None,
        resolved=True,
    )


async def _resolve_ensembl_gene_ids(
    symbols: List[str],
    client: httpx.AsyncClient,
) -> Dict[str, ResolvedGene]:
    """
    Resolve Ensembl gene IDs in one POST call.

    Ensembl can be slow, so batching avoids one network round-trip per ENSG ID
    and keeps a bounded timeout. Unresolved IDs fall back to insufficient evidence.
    """
    if not symbols:
        return {}

    lookup_to_symbols: Dict[str, list[str]] = {}
    for symbol in symbols:
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
            symbol: _ensembl_absent(symbol) if confirmed_absent else _ensembl_failed(symbol)
            for symbol in symbols
        }

    resolved: Dict[str, ResolvedGene] = {}
    for lookup_id, original_symbols in lookup_to_symbols.items():
        for symbol in original_symbols:
            resolved[symbol] = _resolved_from_ensembl(symbol, data.get(lookup_id))
    return resolved


async def _ensembl_lookup(stable_id: str, client: httpx.AsyncClient) -> Optional[dict]:
    """GET /lookup/id/{id}, cached. None when Ensembl says the ID doesn't exist;
    raises httpx.HTTPError / ValueError when the lookup itself failed."""

    async def _fetch() -> Optional[dict]:
        resp = await client.get(
            f"{ENSEMBL_LOOKUP_URL}/{stable_id}",
            headers={"Accept": "application/json"},
            timeout=ENSEMBL_TIMEOUT_SECONDS,
        )
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        return resp.json()

    return await cached_call(f"ensembl:lookup:{stable_id}", _fetch)


async def _resolve_ensembl_transcript_id(symbol: str, client: httpx.AsyncClient) -> ResolvedGene:
    """ENST -> parent gene, ENSP -> parent transcript -> parent gene, then the gene's symbol."""
    stable_id = _ensembl_lookup_id(symbol).upper()
    try:
        doc = await _ensembl_lookup(stable_id, client)
        if stable_id.startswith("ENSP"):
            if not doc or doc.get("object_type") != "Translation" or not doc.get("Parent"):
                return _ensembl_absent(symbol)
            doc = await _ensembl_lookup(doc["Parent"], client)
        if not doc or doc.get("object_type") != "Transcript" or not doc.get("Parent") or not _is_human(doc):
            return _ensembl_absent(symbol)
        gene_doc = await _ensembl_lookup(doc["Parent"], client)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Ensembl lookup failed for %s: %s", symbol, exc)
        return _ensembl_failed(symbol)
    resolved = _resolved_from_ensembl(symbol, gene_doc)
    if resolved.unresolvable:
        return resolved
    transcript_id = symbol.upper() if stable_id.startswith("ENST") else doc.get("id") or stable_id
    return resolved.model_copy(update={"ensembl_transcript_id": transcript_id})


async def _resolve_ensembl_ids(
    symbols: Iterable[str],
    client: httpx.AsyncClient,
) -> Dict[str, ResolvedGene]:
    """Resolve Ensembl gene (one batch POST), transcript and protein IDs to gene symbols."""
    symbol_list = list(dict.fromkeys(symbols))
    gene_ids = [symbol for symbol in symbol_list if symbol.upper().startswith("ENSG")]
    other_ids = [symbol for symbol in symbol_list if not symbol.upper().startswith("ENSG")]
    resolved = await _resolve_ensembl_gene_ids(gene_ids, client)

    semaphore = asyncio.Semaphore(NORMALIZATION_CONCURRENCY)

    async def resolve_one(symbol: str) -> ResolvedGene:
        async with semaphore:
            return await _resolve_ensembl_transcript_id(symbol, client)

    resolved.update(zip(other_ids, await asyncio.gather(*(resolve_one(symbol) for symbol in other_ids))))
    return resolved


def _refseq_absent(symbol: str) -> ResolvedGene:
    return _unresolvable_gene(symbol, resolution_error=f"NCBI has no human RefSeq transcript or protein {symbol}.")


def _refseq_failed(symbol: str) -> ResolvedGene:
    return _unresolvable_gene(
        symbol, lookup_failed=True, resolution_error=f"The NCBI RefSeq lookup for {symbol} failed; please retry."
    )


async def _refseq_gene_lookup(accession: str, client: httpx.AsyncClient) -> Optional[dict]:
    """NCBI's gene for a RefSeq accession, cached by unversioned accession (NCBI
    reports the gene for any version). None when NCBI has no gene for it;
    raises httpx.HTTPError / ValueError when the lookup itself failed."""
    # Lazy: literature imports this module. Shares the NCBI request budget with PubMed.
    from src.pipeline.literature import _get_ncbi_rate_limiter

    lookup_id = _ensembl_lookup_id(accession).upper()

    async def _fetch() -> Optional[dict]:
        headers = {"Accept": "application/json"}
        if settings.ncbi_api_key:
            headers["api-key"] = settings.ncbi_api_key
        for attempt in range(NCBI_RATE_LIMIT_RETRIES + 1):
            async with _get_ncbi_rate_limiter():
                resp = await client.get(
                    NCBI_GENE_BY_ACCESSION_URL.format(accession=lookup_id),
                    headers=headers,
                    timeout=NCBI_TIMEOUT_SECONDS,
                )
            if resp.status_code != 429 or attempt == NCBI_RATE_LIMIT_RETRIES:
                break
            await asyncio.sleep(NCBI_RATE_LIMIT_BACKOFF_SECONDS * 2**attempt)
        if resp.status_code in (400, 404):
            return None
        resp.raise_for_status()
        reports = resp.json().get("reports") or []
        genes = [report.get("gene") or {} for report in reports]
        human = [gene for gene in genes if str(gene.get("tax_id")) == "9606" and gene.get("symbol")]
        if not human:
            return None
        gene = human[0]
        authority = gene.get("nomenclature_authority") or {}
        return {
            "symbol": gene["symbol"],
            "name": gene.get("description"),
            "hgnc_id": authority.get("identifier") if authority.get("authority") == "HGNC" else None,
        }

    return await cached_call(f"ncbi:refseq_gene:{lookup_id}", _fetch)


async def _resolve_refseq_id(symbol: str, client: httpx.AsyncClient) -> ResolvedGene:
    """RefSeq transcript/protein accession -> its human gene's symbol."""
    try:
        doc = await _refseq_gene_lookup(symbol, client)
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("NCBI RefSeq lookup failed for %s: %s", symbol, exc)
        return _refseq_failed(symbol)
    if not doc:
        return _refseq_absent(symbol)
    accession = symbol.upper()
    return ResolvedGene(
        input_symbol=symbol,
        canonical_symbol=doc["symbol"],
        hgnc_id=doc.get("hgnc_id"),
        name=doc.get("name"),
        resolved=True,
        # Proteins (NP_/XP_) name no transcript.
        refseq_transcript_id=accession if accession[1] in "MR" else None,
    )


async def _resolve_refseq_ids(symbols: Iterable[str], client: httpx.AsyncClient) -> Dict[str, ResolvedGene]:
    symbol_list = list(dict.fromkeys(symbols))
    semaphore = asyncio.Semaphore(NORMALIZATION_CONCURRENCY)

    async def resolve_one(symbol: str) -> ResolvedGene:
        async with semaphore:
            return await _resolve_refseq_id(symbol, client)

    return dict(zip(symbol_list, await asyncio.gather(*(resolve_one(symbol) for symbol in symbol_list))))


async def resolve_gene(symbol: str, client: httpx.AsyncClient) -> ResolvedGene:
    """
    Resolve a gene symbol to its canonical HGNC entry.
    Ensembl IDs are resolved through Ensembl and RefSeq accessions through NCBI
    before falling back to insufficient evidence.
    """
    if _is_ensembl_id(symbol):
        return (await _resolve_ensembl_ids([symbol], client))[symbol]
    if _is_refseq_id(symbol):
        return await _resolve_refseq_id(symbol, client)
    if _parse_accession_token(symbol):
        # Decorated, HGVS or malformed Ensembl/RefSeq forms: HGNC search would
        # fuzzy-match 'EML4(ENST...)' to EML4, so never send them there.
        return _unresolvable_gene(symbol, resolution_error=f"'{symbol}' is not a gene symbol.")

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


@dataclass
class CanonicalInput:
    """How one submitted gene/fusion input was read before HGNC normalization."""

    submitted: str
    # The input with each resolved Ensembl/RefSeq-form partner replaced by its
    # gene symbol ('ENST...::ENST...' -> 'EML4::ALK'); the submitted string itself
    # when nothing was rewritten.
    canonical: str
    five_transcript: Optional[str] = None
    three_transcript: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    # True when an error is only a failed (retryable) Ensembl/NCBI/HGNC lookup.
    lookup_failed: bool = False


class GeneMap(Dict[str, Tuple[ResolvedGene, List[str]]]):
    """normalize_fusions' result. The input lists hold canonical input strings;
    `submitted` holds the same inputs as submitted (same order), and `inputs`
    maps each submitted input to how it was read."""

    def __init__(self, *args, inputs: Optional[Dict[str, CanonicalInput]] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.inputs: Dict[str, CanonicalInput] = inputs or {}
        self.submitted: Dict[str, List[str]] = {}


def _read_accession_token(
    token: _AccessionToken,
    by_accession: Dict[str, ResolvedGene],
    hgnc: Dict[str, ResolvedGene],
) -> Tuple[Optional[str], ResolvedGene, Optional[str], Optional[str]]:
    """
    Decide what an Ensembl- or RefSeq-form token means.

    Returns (symbol, resolved, transcript, warning). `symbol` is the gene name
    the canonical input uses; it is None when the token can't be used, in
    which case `resolved` is unresolvable and carries the reason. For a
    GENE(ENST...) whose transcript belongs to GENE, `symbol` is GENE as
    written and the caller resolves it through HGNC like a plain symbol.
    """
    if token.accession is None:
        expected = (
            "NM_, NR_, XM_, XR_, NP_ or XP_ followed by digits and an optional .version, "
            "GENE(NM_...), or NM_...:c...."
            if token.refseq
            else "ENSG, ENST or ENSP followed by 11 digits and an optional .version, GENE(ENST...), or ENST...:c...."
        )
        noun = "human RefSeq transcript or protein accession" if token.refseq else "human Ensembl ID"
        return None, _unresolvable_gene(
            token.raw,
            resolution_error=f"'{token.raw}' is not a valid {noun} or supported form (expected {expected}).",
        ), None, None

    by_id = by_accession[token.accession]
    if by_id.unresolvable:
        return None, by_id.model_copy(update={"input_symbol": token.raw, "canonical_symbol": token.raw}), None, None

    ensembl_symbol = by_id.canonical_symbol or ""
    transcript = by_id.ensembl_transcript_id or by_id.refseq_transcript_id
    named = token.named_gene
    if named and named.upper() != ensembl_symbol.upper():
        named_resolved = hgnc.get(named)
        if named_resolved is not None and named_resolved.lookup_failed:
            return None, _unresolvable_gene(
                token.raw,
                lookup_failed=True,
                resolution_error=(
                    f"Could not check that {token.accession} belongs to {named}: the HGNC lookup failed; "
                    "please retry."
                ),
            ), None, None
        if not (named_resolved and named_resolved.resolved
                and (named_resolved.canonical_symbol or "").upper() == ensembl_symbol.upper()):
            return None, _unresolvable_gene(
                token.raw,
                resolution_error=(
                    f"'{token.raw}': {token.source} {token.accession} belongs to {ensembl_symbol}, not {named}. "
                    f"Correct the gene or the {token.source} {token.id_noun}."
                ),
            ), None, None

    warning = None
    if token.variant:
        warning = (
            f"'{token.raw}' was annotated as gene {ensembl_symbol} (from {token.accession}); "
            f"variant-level interpretation of {token.variant} is not supported."
        )
    if named and not token.variant:
        # GENE(ENST...) matching its transcript: annotate GENE as written.
        return named, by_id.model_copy(update={"input_symbol": named}), transcript, warning
    return ensembl_symbol, by_id.model_copy(update={"input_symbol": token.raw}), transcript, warning


async def normalize_fusions(
    fusions: list[str],
    *,
    strict_gene_lookup: bool = False,
) -> GeneMap:
    """
    Given a list of gene or fusion strings, return a mapping of
    canonical_symbol -> (ResolvedGene, [canonical input strings involving this gene]).

    Each gene appears once regardless of how many inputs involve it. Ensembl
    IDs (bare, GENE(ENST...) or ENST...:c....) are resolved through Ensembl,
    RefSeq accessions (bare, GENE(NM_...) or NM_...:c....) through NCBI, and
    both are replaced by their gene symbol in the canonical input strings; see
    GeneMap.inputs for how each submitted input was read.
    """
    tokens_by_input: Dict[str, list[tuple[str, Optional[_AccessionToken]]]] = {}
    for fusion in dict.fromkeys(fusions):
        g1, g2 = split_fusion(fusion)
        tokens_by_input[fusion] = [(g, _parse_accession_token(g)) for g in filter(None, [g1, g2])]
    accession_tokens = [token for tokens in tokens_by_input.values() for _, token in tokens if token]
    ensembl_ids = sorted({token.accession for token in accession_tokens if token.accession and not token.refseq})
    refseq_ids = sorted({token.accession for token in accession_tokens if token.accession and token.refseq})
    plain_symbols = {g for tokens in tokens_by_input.values() for g, token in tokens if token is None}

    async with httpx.AsyncClient() as client:
        ensembl, refseq = await asyncio.gather(
            _resolve_ensembl_ids(ensembl_ids, client), _resolve_refseq_ids(refseq_ids, client)
        )
        by_accession = {**ensembl, **refseq}
        # GENE in GENE(ENST...) / GENE(NM_...) is resolved alongside the plain
        # symbols (one HGNC pass), both to check it against the transcript's
        # gene and, when it matches, to annotate it like a plain symbol.
        named_symbols = {
            token.named_gene
            for token in accession_tokens
            if token.named_gene and token.accession and by_accession[token.accession].resolved
        }
        hgnc = await _resolve_hgnc_symbols_concurrently(sorted(plain_symbols | named_symbols), client)

    # Ensembl/RefSeq-resolved (or failed) tokens keep their own ResolvedGene, keyed by
    # the submitted token; everything else is keyed by the HGNC symbol.
    ensembl_resolved: Dict[str, ResolvedGene] = {}
    # A failed token's row is shared by every input that used it, so it
    # collects each input's own message (fusions name the rejected partner).
    row_errors: Dict[str, list[str]] = {}
    inputs: Dict[str, CanonicalInput] = {}
    keys_by_input: Dict[str, list[str]] = {}
    for fusion, tokens in tokens_by_input.items():
        info = CanonicalInput(submitted=fusion, canonical=fusion)
        symbols: list[str] = []
        transcripts: list[Optional[str]] = []
        keys: list[str] = []
        readings = [
            (raw, token, _read_accession_token(token, by_accession, hgnc) if token else None)
            for raw, token in tokens
        ]
        for index, (raw, token, reading) in enumerate(readings):
            if reading is None:
                symbols.append(raw)
                transcripts.append(None)
                keys.append(raw)
                continue
            symbol, resolved, transcript, warning = reading
            if warning:
                info.warnings.append(warning)
            if symbol is None:
                error = resolved.resolution_error or f"'{raw}' could not be resolved."
                note = ""
                if len(readings) == 2:
                    # Say which partner was rejected; the other one is still annotated.
                    side, other_side = ("5′", "3′") if index == 0 else ("3′", "5′")
                    error = f"{side} partner of '{fusion}' rejected — {error}"
                    other = readings[1 - index][2]
                    if other is None or other[0] is not None:
                        note = f" The {other_side} partner is still annotated on its own."
                if error not in row_errors.setdefault(raw, []):
                    row_errors[raw].append(error)
                info.errors.append(error + note)
                info.lookup_failed = info.lookup_failed or resolved.lookup_failed
                symbols.append(raw)
                transcripts.append(None)
                keys.append(raw)
                ensembl_resolved[raw] = resolved
            elif token.named_gene and not token.variant:
                symbols.append(symbol)
                transcripts.append(transcript)
                keys.append(symbol)
            else:
                symbols.append(symbol)
                transcripts.append(transcript)
                keys.append(raw)
                ensembl_resolved[raw] = resolved
        if symbols != [raw for raw, _ in tokens]:
            info.canonical = "::".join(symbols)
        if len(transcripts) == 2:
            info.five_transcript, info.three_transcript = transcripts
        inputs[fusion] = info
        keys_by_input[fusion] = keys

    for raw, errors in row_errors.items():
        ensembl_resolved[raw] = ensembl_resolved[raw].model_copy(update={"resolution_error": " ".join(errors)})

    # (request position, canonical input, submitted input) per key, in request
    # order, so merged genes keep first-input-wins whatever the identifier type.
    entries_by_key: Dict[str, list[tuple[int, str, str]]] = {}
    for position, fusion in enumerate(fusions):
        for key in keys_by_input[fusion]:
            entries_by_key.setdefault(key, []).append((position, inputs[fusion].canonical, fusion))

    resolved = {key: ensembl_resolved[key] if key in ensembl_resolved else hgnc[key] for key in entries_by_key}

    if strict_gene_lookup:
        # The gene query API must not classify or reuse a cached annotation
        # when HGNC failed to establish the symbol's identity.
        resolved = {
            symbol: gene.model_copy(update={"unresolvable": True}) if gene.lookup_failed else gene
            for symbol, gene in resolved.items()
        }

    result = GeneMap(inputs=inputs)
    merged: Dict[str, list[tuple[int, str, str]]] = {}
    for symbol, rg in resolved.items():
        # The first-submitted spelling of a gene supplies its ResolvedGene.
        key = rg.canonical_symbol or symbol
        if key not in result:
            result[key] = (rg, [])
        merged.setdefault(key, []).extend(entries_by_key[symbol])
    for key, entries in merged.items():
        entries.sort(key=lambda entry: entry[0])
        result[key][1].extend(canonical for _, canonical, _ in entries)
        result.submitted[key] = [submitted for _, _, submitted in entries]

    return result


async def read_gene_input(value: str) -> CanonicalInput:
    """How one gene/fusion input reads: its canonical form, transcripts, warnings and errors."""
    return (await normalize_fusions([value])).inputs[value]
