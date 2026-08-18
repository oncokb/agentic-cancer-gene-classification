"""
Two-tier literature retrieval.

Tier 1 (cheap): Direct NCBI E-utilities query with a structured search term.
                Sufficient for well-characterised genes with abundant literature.

Tier 2 (fallback): When Tier 1 returns fewer than MIN_PAPERS, Claude becomes
                   the retriever. It receives a search_pubmed tool and decides
                   what queries to run — trying aliases, fusion-specific terms,
                   pathway names, or any angle it judges relevant — until it
                   signals it has enough evidence.

The fallthrough boundary is settings.min_papers_for_strong_association (default 4).
PMID verification at synthesis time applies regardless of which tier produced the records.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

import anthropic
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import (
    EvidenceCard,
    FusionEvidenceCard,
    FusionEvidenceResult,
    FusionPartnerEvidenceResult,
    LiteraturePaperScore,
    LiteratureRecord,
)
from src.pipeline.normalization import split_fusion
from src.pipeline.cache import cached_call
from src.pipeline.llm_client import complete_with_tool, make_async_sdk_client, resolve_sdk_model

logger = logging.getLogger(__name__)

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"

_RATE_LIMIT_DELAY = 0.34 if not settings.ncbi_api_key else 0.11
_request_semaphore = asyncio.Semaphore(3 if not settings.ncbi_api_key else 10)

MAX_AGENTIC_TOOL_CALLS = 6  # cap Claude's search budget per gene

_RETRACTION_PUBLICATION_TYPES: frozenset[str] = frozenset({
    "retracted publication",
    "retraction of publication",
})
_RETRACTION_QUERY_EXCLUSION = (
    '("Retracted Publication"[Publication Type] OR '
    '"Retraction of Publication"[Publication Type])'
)

# ---------------------------------------------------------------------------
# Tumor type alias expansion
# ---------------------------------------------------------------------------

_TUMOR_TYPE_ALIASES: dict[str, tuple[str, ...]] = {
    # NSCLC subtypes
    "luad": ("lung adenocarcinoma", "LUAD"),
    "lusc": ("lung squamous cell carcinoma", "LUSC"),
    "nsclc": ("non-small cell lung cancer", "NSCLC"),
    "non-small cell lung cancer": ("non-small cell lung cancer", "NSCLC"),
    "sclc": ("small cell lung cancer", "SCLC"),
    # Breast
    "tnbc": ("triple-negative breast cancer", "TNBC"),
    "invasive breast carcinoma": ("breast cancer", "breast carcinoma", "invasive breast carcinoma"),
    # GI
    "crc": ("colorectal cancer", "CRC", "colon cancer"),
    "coad": ("colon adenocarcinoma", "colorectal cancer", "COAD"),
    "read": ("rectal adenocarcinoma", "rectal cancer", "READ"),
    "colorectal adenocarcinoma": ("colorectal cancer", "colon cancer", "rectal cancer", "CRC"),
    "pdac": ("pancreatic ductal adenocarcinoma", "PDAC", "pancreatic cancer"),
    "pancreatic adenocarcinoma": ("pancreatic cancer", "PDAC", "pancreatic ductal adenocarcinoma"),
    "hcc": ("hepatocellular carcinoma", "HCC", "liver cancer"),
    "gc": ("gastric cancer", "stomach cancer"),
    "stad": ("stomach adenocarcinoma", "gastric cancer", "STAD"),
    "eac": ("esophageal adenocarcinoma", "EAC"),
    "escc": ("esophageal squamous cell carcinoma", "ESCC"),
    "esophagogastric cancer": (
        "esophageal cancer", "gastroesophageal cancer", "esophagogastric", "gastric cancer",
    ),
    "cca": ("cholangiocarcinoma", "bile duct cancer"),
    # Renal
    "rcc": ("renal cell carcinoma", "RCC", "kidney cancer"),
    "ccrcc": ("clear cell renal cell carcinoma", "ccRCC"),
    "kirc": ("kidney renal clear cell carcinoma", "clear cell RCC"),
    "kirp": ("kidney renal papillary cell carcinoma", "papillary RCC"),
    # Brain / CNS
    "gbm": ("glioblastoma", "GBM", "glioblastoma multiforme"),
    "lgg": ("lower grade glioma", "LGG", "glioma"),
    # Hematologic malignancies
    "aml": ("acute myeloid leukemia", "AML"),
    "cml": ("chronic myeloid leukemia", "CML"),
    "all": ("acute lymphoblastic leukemia", "ALL", "acute lymphocytic leukemia"),
    "cll": ("chronic lymphocytic leukemia", "CLL"),
    "mm": ("multiple myeloma", "MM"),
    "dlbcl": ("diffuse large B-cell lymphoma", "DLBCL"),
    "fl": ("follicular lymphoma", "FL"),
    "mcl": ("mantle cell lymphoma", "MCL"),
    "nhl": ("non-Hodgkin lymphoma", "NHL"),
    "hl": ("Hodgkin lymphoma", "HL", "Hodgkin disease"),
    # Gynecologic
    "ov": ("ovarian cancer", "ovarian serous carcinoma", "OV"),
    "ucec": ("uterine corpus endometrial carcinoma", "endometrial cancer", "UCEC"),
    "cesc": ("cervical squamous cell carcinoma", "cervical cancer", "CESC"),
    # Skin
    "skcm": ("skin cutaneous melanoma", "melanoma", "SKCM"),
    "uvm": ("uveal melanoma", "UVM"),
    # Head and neck
    "hnscc": ("head and neck squamous cell carcinoma", "HNSCC"),
    # Thyroid
    "thca": ("thyroid carcinoma", "THCA", "thyroid cancer"),
    # Prostate
    "prad": ("prostate adenocarcinoma", "prostate cancer", "PRAD"),
    # Bladder
    "blca": ("bladder urothelial carcinoma", "bladder cancer", "BLCA"),
    "uc": ("urothelial carcinoma", "bladder cancer"),
    "urothelial carcinoma": ("urothelial carcinoma", "bladder cancer", "UC"),
    # Other
    "meso": ("mesothelioma", "MESO"),
    "sarc": ("sarcoma", "SARC"),
    "lcnec": ("large cell neuroendocrine carcinoma", "LCNEC"),
    "pcpg": ("pheochromocytoma", "paraganglioma", "PCPG"),
}


def _expand_tumor_type_terms(tumor_type: str) -> list[str]:
    """Return PubMed search phrases for tumor_type, expanding abbreviations/aliases."""
    aliases = _TUMOR_TYPE_ALIASES.get(tumor_type.strip().lower())
    return list(aliases) if aliases else [tumor_type.strip()]


def _tumor_type_query_fragment(tumor_type: str) -> str:
    """Build a quoted OR-clause covering tumor_type and all its known aliases."""
    terms = _expand_tumor_type_terms(tumor_type)
    if len(terms) == 1:
        return f'"{terms[0]}"'
    return "(" + " OR ".join(f'"{t}"' for t in terms) + ")"


def _tumor_type_prompt_note(tumor_type: str) -> str:
    """Human-readable tumor type hint for LLM prompts, listing known aliases."""
    expanded = _expand_tumor_type_terms(tumor_type)
    if len(expanded) > 1:
        return f"{tumor_type} (also search: {', '.join(expanded)})"
    return tumor_type


def _exclude_retracted_query(query: str) -> str:
    """Add a PubMed query clause excluding retracted papers and retraction notices."""
    if _RETRACTION_QUERY_EXCLUSION in query:
        return query
    return f"({query}) NOT {_RETRACTION_QUERY_EXCLUSION}"


def _is_retracted_publication(record: LiteratureRecord) -> bool:
    publication_types = {value.strip().lower() for value in record.publication_types}
    if publication_types.intersection(_RETRACTION_PUBLICATION_TYPES):
        return True
    title = record.title.strip().lower()
    return title.startswith(("retracted:", "retraction:"))


def _filter_retracted_records(records: List[LiteratureRecord]) -> List[LiteratureRecord]:
    filtered = [record for record in records if not _is_retracted_publication(record)]
    dropped = len(records) - len(filtered)
    if dropped:
        logger.info("Filtered %d retracted PubMed record(s)", dropped)
    return filtered


_NOVEL_FUSION_TERMS = (
    "novel fusion",
    "novel gene fusion",
    "first report",
    "first case",
    "case report",
    "previously undescribed",
    "previously unreported",
)


def _fusion_query_variants(fusion: str) -> List[str]:
    five_prime, three_prime = split_fusion(fusion)
    if not five_prime or not three_prime:
        return []
    variants = [
        f"{five_prime}::{three_prime}",
        f"{five_prime}-{three_prime}",
        f"{five_prime}/{three_prime}",
        f"{five_prime} {three_prime}",
    ]
    return list(dict.fromkeys(variants))


def _fusion_evidence_queries(fusion: str, tumor_type: Optional[str] = None) -> List[str]:
    variants = _fusion_query_variants(fusion)
    if not variants:
        return []
    variant_fragment = " OR ".join(f'"{variant}"' for variant in variants)
    queries = [
        f"({variant_fragment}) AND (fusion OR rearrangement OR translocation) AND "
        "(cancer OR tumor OR carcinoma OR neoplasm)",
    ]
    if tumor_type:
        queries.insert(
            0,
            f"({variant_fragment}) AND {_tumor_type_query_fragment(tumor_type)}",
        )
    return list(dict.fromkeys(queries))


def _novelty_record_count(records: List[LiteratureRecord]) -> int:
    count = 0
    for record in records:
        text = f"{record.title} {record.abstract}".lower()
        if any(term in text for term in _NOVEL_FUSION_TERMS):
            count += 1
    return count


def _fusion_evidence_type(record: LiteratureRecord) -> str:
    combined = f"{record.title} {record.abstract}".lower()
    publication_types = {value.lower() for value in record.publication_types}
    if any("clinical trial" in value for value in publication_types):
        return "clinical"
    if any(term in combined for term in ("patient", "cohort", "survival", "response", "therapy")):
        return "clinical"
    if any(term in combined for term in ("cell line", "xenograft", "knockdown", "inhibitor")):
        return "functional"
    if any(term in combined for term in ("case report", "first report", "novel fusion")):
        return "case_report"
    return "fusion"


def _record_quote(record: LiteratureRecord) -> Optional[str]:
    text = record.abstract.strip()
    if not text:
        return None
    sentence = text.split(". ", 1)[0].strip()
    if sentence and not sentence.endswith("."):
        sentence += "."
    return sentence[:320] or None


def _build_fusion_evidence_cards(
    fusion: str,
    records: List[LiteratureRecord],
    limit: int = 5,
) -> List[FusionEvidenceCard]:
    cards: List[FusionEvidenceCard] = []
    for record in records[:limit]:
        evidence_type = _fusion_evidence_type(record)
        selected_reason = f"Matched an exact fusion-pair PubMed query as {evidence_type.replace('_', ' ')} evidence."
        if record.journal in _HIGH_IMPACT_JOURNALS:
            selected_reason += " High-impact journal signal."
        cards.append(
            FusionEvidenceCard(
                fusion=fusion,
                pmid=record.pmid,
                title=record.title,
                journal=record.journal,
                evidence_type=evidence_type,
                selected_reason=selected_reason,
                quote=_record_quote(record),
            )
        )
    return cards


def _fusion_evidence_interpretation(
    *,
    fusion: str,
    tumor_type: Optional[str],
    records: List[LiteratureRecord],
    well_supported: bool,
) -> str:
    tumor_note = f" in {tumor_type}" if tumor_type else ""
    novelty_count = _novelty_record_count(records)
    if well_supported:
        return (
            f"{fusion}{tumor_note} has {len(records)} non-retracted PubMed record(s) from exact "
            "fusion-pair searches, suggesting this is a studied fusion event rather than a "
            "single novelty-only report."
        )
    if records:
        novelty_note = " Most retrieved records look novelty or case-report driven." if novelty_count else ""
        return (
            f"{fusion}{tumor_note} has {len(records)} non-retracted PubMed record(s) from exact "
            f"fusion-pair searches, below the well-supported threshold.{novelty_note}"
        )
    return f"No non-retracted PubMed records were found for exact {fusion}{tumor_note} fusion-pair searches."


async def retrieve_fusion_evidence(
    fusion: str,
    tumor_type: Optional[str] = None,
    max_results: Optional[int] = None,
) -> FusionEvidenceResult:
    """Return deterministic fusion-level PubMed evidence for exact gene-pair queries."""
    max_results = max_results or settings.fusion_evidence_max_results
    cache_key = "fusion_evidence:" + json.dumps(
        {
            "fusion": fusion.strip(),
            "tumor_type": " ".join((tumor_type or "").strip().lower().split()),
            "max_results": max_results,
            "min_papers": settings.min_papers_for_strong_association,
        },
        sort_keys=True,
    )

    async def _compute() -> dict:
        result = await _retrieve_fusion_evidence_uncached(
            fusion,
            tumor_type=tumor_type,
            max_results=max_results,
        )
        return result.model_dump()

    payload = await cached_call(
        cache_key,
        _compute,
        ttl_seconds=settings.fusion_evidence_cache_ttl_seconds,
    )
    return FusionEvidenceResult(**payload)


async def _retrieve_fusion_evidence_uncached(
    fusion: str,
    tumor_type: Optional[str],
    max_results: int,
) -> FusionEvidenceResult:
    queries = _fusion_evidence_queries(fusion, tumor_type)
    if not queries:
        return FusionEvidenceResult(
            fusion=fusion,
            tumor_type=tumor_type,
            interpretation="Fusion evidence is only available for two-partner fusion inputs.",
        )

    async with httpx.AsyncClient() as client:
        pmid_lists = await asyncio.gather(*[_esearch(query, max_results, client) for query in queries])
        seen_pmids: Set[str] = set()
        pmids: List[str] = []
        for pmid_list in pmid_lists:
            for pmid in pmid_list:
                if pmid not in seen_pmids:
                    seen_pmids.add(pmid)
                    pmids.append(pmid)
        records = await _efetch(pmids, client)

    records = _rank_records(_filter_retracted_records(records))
    novelty_count = _novelty_record_count(records)
    well_supported = (
        len(records) >= settings.min_papers_for_strong_association
        and novelty_count < len(records)
    )
    return FusionEvidenceResult(
        fusion=fusion,
        tumor_type=tumor_type,
        well_supported=well_supported,
        retrieved_count=len(records),
        pmids=[record.pmid for record in records],
        interpretation=_fusion_evidence_interpretation(
            fusion=fusion,
            tumor_type=tumor_type,
            records=records,
            well_supported=well_supported,
        ),
        evidence_cards=_build_fusion_evidence_cards(fusion, records),
    )


# ---------------------------------------------------------------------------
# Fusion partner precedent lookup (on-demand, per gene)
#
# Different question from retrieve_fusion_evidence above: that checks an exact
# fusion pair (e.g. "EML4::ALK"). This checks whether a single partner gene has
# precedent in ANY reported oncogenic fusion — used as a curator-triggered
# follow-up when a gene comes back insufficient_evidence, to see whether a
# fusion partner has known fusion precedent even though this exact pair doesn't
# have direct literature yet.
# ---------------------------------------------------------------------------

def _fusion_partner_precedent_queries(
    gene: str, tumor_type: Optional[str], agnostic: bool
) -> List[str]:
    """Queries biased toward 'has this gene been reported in oncogenic fusions', not
    general cancer relevance.

    When a tumor_type is supplied and agnostic=False, queries are scoped to that tumor
    type only — the prioritized first pass, most relevant to the case at hand. Pass
    agnostic=True (typically as a follow-up once the scoped pass has been reviewed) to
    broaden to any tumor type.
    """
    fusion_terms = '(fusion OR "gene fusion" OR chimeric OR rearrangement OR translocation)'
    if tumor_type and not agnostic:
        tt = _tumor_type_query_fragment(tumor_type)
        return [
            f'"{gene}" AND {fusion_terms} AND {tt}',
            f'"{gene}"[Gene Name] AND {fusion_terms} AND {tt}',
        ]
    return [
        f'"{gene}"[Gene Name] AND {fusion_terms} AND cancer[MeSH Terms]',
        f'"{gene}" AND {fusion_terms} AND (cancer OR tumor OR oncogenic OR malignancy)',
    ]


def _fusion_partner_evidence_cards(records: List[LiteratureRecord], limit: int = 5) -> List[EvidenceCard]:
    cards: List[EvidenceCard] = []
    for record in records[:limit]:
        evidence_type = _fusion_evidence_type(record)
        selected_reason = (
            f"Matched a fusion-precedent PubMed query as {evidence_type.replace('_', ' ')} evidence."
        )
        if record.journal in _HIGH_IMPACT_JOURNALS:
            selected_reason += " High-impact journal signal."
        cards.append(
            EvidenceCard(
                pmid=record.pmid,
                title=record.title,
                journal=record.journal,
                evidence_type=evidence_type,
                selected_reason=selected_reason,
                quote=_record_quote(record),
            )
        )
    return cards


def _fusion_partner_interpretation(
    gene: str, tumor_type: Optional[str], agnostic: bool, records: List[LiteratureRecord]
) -> str:
    if tumor_type and not agnostic:
        scope_note = f" in {tumor_type}"
    elif agnostic:
        scope_note = " across other tumor types"
    else:
        scope_note = ""
    if records:
        return (
            f"{gene} has {len(records)} non-retracted PubMed record(s) discussing oncogenic "
            f"fusions{scope_note}, suggesting precedent as a fusion partner."
        )
    return f"No non-retracted PubMed records were found for {gene} in oncogenic fusions{scope_note}."


async def _retrieve_fusion_partner_records(
    gene: str,
    tumor_type: Optional[str],
    agnostic: bool,
    max_results: int,
) -> List[LiteratureRecord]:
    """Cached, Tier-1-only retrieval — no Tier 2 agentic fallback, since this is an
    optional curator-triggered follow-up rather than part of the core annotation."""
    cache_key = "fusion_partner_evidence:" + json.dumps(
        {
            "gene": gene.strip().upper(),
            "tumor_type": (
                " ".join((tumor_type or "").strip().lower().split()) if tumor_type and not agnostic else None
            ),
            "agnostic": agnostic,
            "max_results": max_results,
        },
        sort_keys=True,
    )

    async def _compute() -> List[dict]:
        queries = _fusion_partner_precedent_queries(gene, tumor_type, agnostic)
        async with httpx.AsyncClient() as client:
            pmid_lists = await asyncio.gather(*[_esearch(q, max_results, client) for q in queries])
            seen_pmids: Set[str] = set()
            pmids: List[str] = []
            for pmid_list in pmid_lists:
                for pmid in pmid_list:
                    if pmid not in seen_pmids:
                        seen_pmids.add(pmid)
                        pmids.append(pmid)
            records = await _efetch(pmids, client)
        records = _rank_records(_filter_retracted_records(records))
        return [record.model_dump() for record in records]

    payload = await cached_call(
        cache_key, _compute, ttl_seconds=settings.fusion_partner_evidence_cache_ttl_seconds
    )
    return [LiteratureRecord(**record) for record in payload]


async def retrieve_fusion_partner_evidence(
    gene: str,
    tumor_type: Optional[str] = None,
    agnostic: bool = False,
    exclude_pmids: Optional[Set[str]] = None,
    max_results: Optional[int] = None,
) -> FusionPartnerEvidenceResult:
    """
    Return deterministic evidence for whether `gene` has precedent as an oncogenic
    fusion partner, independent of the exact fusion pair under review.

    exclude_pmids lets a follow-up agnostic call report only the PMIDs not already
    surfaced by an earlier tumor-type-scoped call for the same gene.
    """
    max_results = max_results or settings.fusion_partner_evidence_max_results
    exclude = exclude_pmids or set()
    records = await _retrieve_fusion_partner_records(gene, tumor_type, agnostic, max_results)
    records = [record for record in records if record.pmid not in exclude]

    scope = "tumor_type_scoped" if (tumor_type and not agnostic) else "all_tumor_types"
    return FusionPartnerEvidenceResult(
        gene=gene,
        tumor_type=tumor_type,
        scope=scope,
        has_precedent=bool(records),
        retrieved_count=len(records),
        pmids=[record.pmid for record in records],
        interpretation=_fusion_partner_interpretation(gene, tumor_type, agnostic, records),
        evidence_cards=_fusion_partner_evidence_cards(records),
    )


# ---------------------------------------------------------------------------
# Evidence priority ranking
# ---------------------------------------------------------------------------

_HIGH_IMPACT_JOURNALS: frozenset[str] = frozenset({
    "N Engl J Med", "Lancet", "JAMA", "BMJ", "Nat Med", "Nat Genet",
    "Nature", "Science", "Cell", "Nat Cancer", "Nat Commun",
    "Sci Transl Med", "EMBO J",
    "J Clin Oncol", "Cancer Cell", "Cancer Discov", "Cancer Res",
    "Clin Cancer Res", "Ann Oncol", "Lancet Oncol", "JAMA Oncol",
    "Mol Cancer Ther", "Mol Cancer", "Oncogene", "Cancer Lett",
    "Blood", "Leukemia", "J Hematol Oncol", "Haematologica",
    "Genome Res", "Genome Biol", "Nucleic Acids Res", "Nat Biotechnol",
})

_HUMAN_CLINICAL_TERMS: tuple[str, ...] = (
    "patient", "clinical trial", "cohort", "phase i", "phase ii", "phase iii",
    "randomized", "overall survival", "progression-free", "response rate",
    "tumor biopsy", "case report", "case series",
)
_CELL_LINE_TERMS: tuple[str, ...] = (
    "cell line", "in vitro", "xenograft", "knockdown", "knockout", "overexpression",
    "proliferation", "apoptosis", "transfection", "shRNA", "siRNA", "CRISPR",
)
_MOUSE_MODEL_TERMS: tuple[str, ...] = (
    "mouse model", "transgenic", "in vivo", "tumor formation", "tumor growth",
    "orthotopic", "syngeneic", "genetically engineered",
)


def _record_text(record: LiteratureRecord) -> str:
    return " ".join((record.title, record.abstract, " ".join(record.publication_types))).lower()


def _contains_phrase(text: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term.lower())}(?![a-z0-9])", text) is not None


def _publication_evidence_rank(record: LiteratureRecord) -> int:
    """0=human clinical, 1=cell line, 2=mouse model, 3=other."""
    text = _record_text(record)
    if any(_contains_phrase(text, t) for t in _HUMAN_CLINICAL_TERMS):
        return 0
    if any(_contains_phrase(text, t) for t in _CELL_LINE_TERMS):
        return 1
    if any(_contains_phrase(text, t) for t in _MOUSE_MODEL_TERMS):
        return 2
    return 3


def _is_high_impact(record: LiteratureRecord) -> bool:
    return record.journal in _HIGH_IMPACT_JOURNALS


def _rank_records(records: List[LiteratureRecord]) -> List[LiteratureRecord]:
    """Sort by evidence tier, then high-impact journal, then recency (descending PMID)."""
    return sorted(
        records,
        key=lambda r: (
            _publication_evidence_rank(r),
            0 if _is_high_impact(r) else 1,
            -(int(r.pmid) if r.pmid.isdigit() else 0),
        ),
    )


def _evidence_priority_queries(subject: str) -> list[str]:
    """Queries that surface human clinical > cell line > mouse model evidence."""
    clinical = " OR ".join(f'"{t}"' for t in _HUMAN_CLINICAL_TERMS[:6])
    cell = " OR ".join(f'"{t}"' for t in _CELL_LINE_TERMS[:6])
    mouse = " OR ".join(f'"{t}"' for t in _MOUSE_MODEL_TERMS[:4])
    return [
        f'"{subject}" AND ({clinical})',
        f'"{subject}" AND ({cell})',
        f'"{subject}" AND ({mouse})',
    ]


# ---------------------------------------------------------------------------
# Shared NCBI helpers (used by both tiers)
# ---------------------------------------------------------------------------

def _ncbi_params(extra: dict) -> dict:
    params = {"retmode": "json", **extra}
    if settings.ncbi_api_key:
        params["api_key"] = settings.ncbi_api_key
    return params


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _esearch(query: str, max_results: int, client: httpx.AsyncClient) -> List[str]:
    """Return PMIDs matching a PubMed query string."""

    async def _fetch() -> List[str]:
        search_query = _exclude_retracted_query(query)
        params = _ncbi_params(
            {"db": "pubmed", "term": search_query, "retmax": max_results, "sort": "relevance"}
        )
        async with _request_semaphore:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            resp = await client.get(ESEARCH_URL, params=params, timeout=15.0)
            resp.raise_for_status()
        return resp.json().get("esearchresult", {}).get("idlist", [])

    return await cached_call(f"pubmed:esearch:{_exclude_retracted_query(query)}:{max_results}", _fetch)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _esearch_since(
    query: str,
    since: datetime,
    max_results: int,
    client: httpx.AsyncClient,
) -> List[str]:
    """Return PMIDs published since a UTC timestamp for lightweight cache freshness checks."""
    since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
    search_query = _exclude_retracted_query(query)
    params = _ncbi_params(
        {
            "db": "pubmed",
            "term": search_query,
            "retmax": max_results,
            "sort": "pub date",
            "datetype": "pdat",
            "mindate": since_utc.strftime("%Y/%m/%d"),
        }
    )
    async with _request_semaphore:
        await asyncio.sleep(_RATE_LIMIT_DELAY)
        resp = await client.get(ESEARCH_URL, params=params, timeout=15.0)
        resp.raise_for_status()
    return resp.json().get("esearchresult", {}).get("idlist", [])


async def search_recent_pubmed_pmids(
    gene: str,
    fusions: Optional[List[str]],
    since: datetime,
    tumor_type: Optional[str] = None,
) -> List[str]:
    """
    Cheap PubMed freshness probe for cached gene annotations.

    This intentionally returns PMIDs only. A non-empty result means the cache may
    need full retrieval/synthesis; an empty result lets us reuse stale-but-checked
    annotations without spending LLM tokens.
    """
    queries = [f'"{gene}" AND (cancer OR tumor OR oncology OR carcinoma)']
    if tumor_type:
        queries.append(f'"{gene}" AND {_tumor_type_query_fragment(tumor_type)}')
    for partner in _fusion_partners(gene, fusions or [])[:2]:
        queries.append(f'"{gene}" AND "{partner}"')
        if tumor_type:
            queries.append(f'"{gene}" AND "{partner}" AND {_tumor_type_query_fragment(tumor_type)}')
    queries = list(dict.fromkeys(queries))

    async with httpx.AsyncClient() as client:
        pmid_lists = await asyncio.gather(
            *[
                _esearch_since(query, since, settings.gene_cache_freshness_pmids, client)
                for query in queries
            ]
        )

    seen: Set[str] = set()
    recent_pmids: List[str] = []
    for pmids in pmid_lists:
        for pmid in pmids:
            if pmid not in seen:
                seen.add(pmid)
                recent_pmids.append(pmid)
    return recent_pmids


def _parse_pub_year(pub_date_el: Optional[ET.Element]) -> Optional[int]:
    """Extract a publication year from a PubDate element. Falls back to pulling the
    first 4-digit year out of MedlineDate (e.g. "2020 Jan-Feb") when Year is absent."""
    if pub_date_el is None:
        return None
    year_el = pub_date_el.find("Year")
    if year_el is not None and year_el.text and year_el.text.strip().isdigit():
        return int(year_el.text.strip())
    medline_date_el = pub_date_el.find("MedlineDate")
    if medline_date_el is not None and medline_date_el.text:
        match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", medline_date_el.text)
        if match:
            return int(match.group(1))
    return None


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
async def _efetch(pmids: List[str], client: httpx.AsyncClient) -> List[LiteratureRecord]:
    """Fetch abstracts for a list of PMIDs, including journal and publication type metadata."""
    if not pmids:
        return []

    async def _fetch() -> List[dict]:
        params = {"db": "pubmed", "id": ",".join(pmids), "rettype": "abstract", "retmode": "xml"}
        if settings.ncbi_api_key:
            params["api_key"] = settings.ncbi_api_key

        async with _request_semaphore:
            await asyncio.sleep(_RATE_LIMIT_DELAY)
            resp = await client.get(EFETCH_URL, params=params, timeout=30.0)
            resp.raise_for_status()

        records: List[dict] = []
        try:
            root = ET.fromstring(resp.text)
            for article in root.findall(".//PubmedArticle"):
                pmid_el = article.find(".//PMID")
                pmid = pmid_el.text if pmid_el is not None else None
                title_el = article.find(".//ArticleTitle")
                title = (title_el.text or "").strip() if title_el is not None else ""
                abstract_parts = article.findall(".//AbstractText")
                abstract = " ".join(
                    (el.text or "").strip() for el in abstract_parts if el.text
                ).strip()
                journal_el = article.find(".//MedlineTA")
                journal = journal_el.text.strip() if journal_el is not None and journal_el.text else ""
                pub_types = [
                    el.text.strip()
                    for el in article.findall(".//PublicationType")
                    if el.text
                ]
                pub_date_el = article.find(".//Article/Journal/JournalIssue/PubDate")
                year = _parse_pub_year(pub_date_el)
                if pmid and abstract:
                    records.append({
                        "pmid": pmid, "title": title, "abstract": abstract,
                        "journal": journal, "publication_types": pub_types,
                        "publication_year": year,
                    })
        except ET.ParseError as exc:
            logger.warning("XML parse error in efetch: %s", exc)
        return records

    cache_key = f"pubmed:efetch:{','.join(sorted(pmids))}"
    records = await cached_call(cache_key, _fetch)
    return _filter_retracted_records([LiteratureRecord(**r) for r in records])


async def _search_and_fetch(
    query: str,
    max_results: int,
    client: httpx.AsyncClient,
    already_seen: Set[str],
) -> List[LiteratureRecord]:
    """Run esearch + efetch, skipping PMIDs already in the accumulator."""
    pmids = await _esearch(query, max_results, client)
    new_pmids = [p for p in pmids if p not in already_seen]
    if not new_pmids:
        return []
    return await _efetch(new_pmids, client)


# ---------------------------------------------------------------------------
# Tier 1: parallel multi-query retrieval
# ---------------------------------------------------------------------------

def _fusion_partners(gene: str, fusions: List[str]) -> List[str]:
    """Extract partner symbols from fusion strings, excluding the gene itself."""
    partners: List[str] = []
    for fusion in fusions:
        for sep in ("::", "--", "/"):
            if sep in fusion:
                for part in fusion.split(sep):
                    part = part.strip()
                    if part and part != gene:
                        partners.append(part)
                break
    return list(dict.fromkeys(partners))


async def _tier1_retrieve(
    gene: str,
    fusions: Optional[List[str]] = None,
    tumor_type: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Primary retrieval: runs multiple PubMed queries in parallel to maximise
    coverage before falling through to the agentic Tier 2.

    Query families (run concurrently):
      1. Gene Name MeSH field query (high precision)
      2. Free-text broadening query
      3. Evidence-priority queries (clinical > cell line > mouse model)
      4. Tumor-type-specific query with alias expansion when tumor_type is supplied
      5. Co-query with each fusion partner
    """
    evidence_queries = _evidence_priority_queries(gene)
    family_by_query: Dict[str, str] = {}

    def _tag(query: str, family: str) -> str:
        # First family assigned to a query string wins; collisions are rare
        # (would require two distinct query builders producing identical text).
        family_by_query.setdefault(query, family)
        return query

    stage_1 = [
        _tag(f'"{gene}"[Gene Name] AND cancer[MeSH Terms]', "mesh_gene_name"),
    ]
    if tumor_type:
        tt = _tumor_type_query_fragment(tumor_type)
        stage_1.insert(0, _tag(f'"{gene}" AND {tt}', "tumor_type"))

    stage_2 = [
        _tag(f'"{gene}" AND (cancer OR tumor OR oncology OR carcinoma)', "free_text"),
        _tag(evidence_queries[0], "evidence_priority"),
    ]
    stage_3 = [_tag(q, "evidence_priority") for q in evidence_queries[1:]]
    for partner in _fusion_partners(gene, fusions or [])[:2]:
        stage_3.append(_tag(f'"{gene}" AND "{partner}"', "fusion_partner"))
        if tumor_type:
            stage_3.append(
                _tag(
                    f'"{gene}" AND "{partner}" AND {_tumor_type_query_fragment(tumor_type)}',
                    "fusion_partner",
                )
            )

    stages = [
        list(dict.fromkeys(stage))
        for stage in (stage_1, stage_2, stage_3)
        if stage
    ]
    queries = list(dict.fromkeys(query for stage in stages for query in stage))

    def _tag_matched_query_tiers(records: List[LiteratureRecord], matched: Dict[str, Set[str]]) -> None:
        for record in records:
            families = matched.get(record.pmid)
            if families:
                record.matched_query_tiers = list(set(record.matched_query_tiers) | families)

    if not settings.pubmed_staged_retrieval:
        matched: Dict[str, Set[str]] = {}
        async with httpx.AsyncClient() as client:
            pmid_lists = await asyncio.gather(
                *[_esearch(q, settings.pubmed_max_results, client) for q in queries]
            )
            seen: Set[str] = set()
            merged: List[str] = []
            for query, pmids in zip(queries, pmid_lists):
                family = family_by_query.get(query, "free_text")
                for pmid in pmids:
                    matched.setdefault(pmid, set()).add(family)
                    if pmid not in seen:
                        seen.add(pmid)
                        merged.append(pmid)
            fetch_cap = settings.pubmed_max_results * len(queries)
            records = await _efetch(merged[:fetch_cap], client)

        _tag_matched_query_tiers(records, matched)
        records = _rank_records(records)
        logger.info(
            "Tier 1: %d abstracts for %s (%d queries, %d unique PMIDs before cap)",
            len(records), gene, len(queries), len(merged),
        )
        return records

    stop_target = max(settings.min_papers_for_strong_association, settings.max_papers_for_synthesis)
    all_records: Dict[str, LiteratureRecord] = {}
    matched: Dict[str, Set[str]] = {}
    seen_pmids: Set[str] = set()
    searched_queries = 0

    async with httpx.AsyncClient() as client:
        for stage in stages:
            searched_queries += len(stage)
            pmid_lists = await asyncio.gather(
                *[_esearch(q, settings.pubmed_max_results, client) for q in stage]
            )
            stage_pmids: List[str] = []
            for query, pmids in zip(stage, pmid_lists):
                family = family_by_query.get(query, "free_text")
                for pmid in pmids:
                    matched.setdefault(pmid, set()).add(family)
                    if pmid not in seen_pmids:
                        seen_pmids.add(pmid)
                        stage_pmids.append(pmid)
            stage_records = await _efetch(stage_pmids, client)
            for record in stage_records:
                all_records[record.pmid] = record

            _tag_matched_query_tiers(list(all_records.values()), matched)
            ranked = _rank_records(list(all_records.values()))
            if len(ranked) >= stop_target:
                logger.info(
                    "Tier 1 staged: %d abstracts for %s after %d/%d queries",
                    len(ranked), gene, searched_queries, len(queries),
                )
                return ranked

    records = _rank_records(list(all_records.values()))
    logger.info(
        "Tier 1 staged: %d abstracts for %s after all %d queries",
        len(records), gene, searched_queries,
    )
    return records


# ---------------------------------------------------------------------------
# Tier 2: Claude agentic retrieval
# ---------------------------------------------------------------------------

_SEARCH_PUBMED_TOOL: anthropic.types.ToolParam = {
    "name": "search_pubmed",
    "description": (
        "Search PubMed for papers about a gene or topic in a cancer context. "
        "You may call this multiple times with different queries — aliases, pathway names, "
        "fusion-specific terms, disease contexts — to collect enough evidence. "
        "Call done() when you have sufficient abstracts or have exhausted useful queries."
    ),
    "input_schema": {
        "type": "object",
        "required": ["query"],
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "PubMed query string. Examples: '\"DDEFL1\" AND cancer', "
                    "'\"EML4-ALK\" fusion lung adenocarcinoma', "
                    "'\"TRARG1\" OR \"TUSC5\" tumor suppressor'"
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Max abstracts to retrieve (default 10, max 20).",
                "default": 10,
            },
        },
    },
}

_DONE_TOOL: anthropic.types.ToolParam = {
    "name": "done",
    "description": "Signal that you have retrieved sufficient literature and are ready for synthesis.",
    "input_schema": {"type": "object", "properties": {}},
}

_SUGGEST_QUERIES_TOOL: dict = {
    "name": "suggest_pubmed_queries",
    "description": (
        "Return PubMed queries that are likely to find cancer-relevant evidence for this gene."
    ),
    "input_schema": {
        "type": "object",
        "required": ["queries"],
        "properties": {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "PubMed query strings, ordered from most to least promising.",
            }
        },
    },
}

_AGENTIC_SYSTEM = """\
You are a cancer genomics literature specialist. Your job is to find PubMed papers
that establish or refute the cancer relevance of a given gene.

You have a search_pubmed tool. Use it to retrieve abstracts by trying different angles:
- The canonical HUGO symbol and known aliases
- Fusion-specific terms (e.g. "EML4-ALK fusion")
- Associated pathways or protein family terms
- Specific cancer types where this gene is suspected to be relevant

Prioritize queries likely to surface human clinical evidence first (patient cohorts,
clinical trials, survival data), then cell-line functional studies, then mouse models.

Aim for at least 4 high-quality abstracts with direct cancer relevance. If a gene has
no cancer literature after 3–4 different search attempts, call done() — "insufficient evidence"
is a valid outcome and should not be papered over with loosely-related papers.

Do not fabricate PMIDs or cite papers you did not retrieve via search_pubmed.
"""


def _format_initial_records(records: List[LiteratureRecord]) -> str:
    if not records:
        return "No papers retrieved yet."
    lines = [f"Already retrieved {len(records)} paper(s) from an initial query (insufficient):"]
    for r in records:
        lines.append(f"  PMID {r.pmid}: {r.title[:80]}")
    return "\n".join(lines)


async def _tier2_agentic_retrieve(
    gene: str,
    fusions: List[str],
    initial_records: List[LiteratureRecord],
    tumor_type: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Fallback retrieval: Claude decides what queries to run.
    Runs an agentic tool-use loop, accumulating unique records across all searches.
    """
    accumulated: Dict[str, LiteratureRecord] = {
        r.pmid: r for r in _filter_retracted_records(initial_records)
    }

    fusion_context = f"Associated fusions: {', '.join(fusions)}" if fusions else ""
    tumor_note = f"Tumor type: {_tumor_type_prompt_note(tumor_type)}" if tumor_type else ""
    initial_summary = _format_initial_records(initial_records)

    user_message = (
        f"Gene: {gene}\n"
        f"{fusion_context}\n"
        f"{tumor_note}\n\n"
        f"{initial_summary}\n\n"
        f"Please search for additional cancer-relevant literature for {gene}. "
        f"Try different queries including aliases, fusion-specific terms, and the supplied tumor type "
        f"(and its aliases listed above) when relevant. "
        f"Prioritize queries that surface human clinical evidence first, then cell-line studies, "
        f"then mouse models. "
        f"Call done() when satisfied or after exhausting useful approaches."
    )

    messages = [{"role": "user", "content": user_message}]
    tool_calls_made = 0
    client = make_async_sdk_client()

    async with httpx.AsyncClient() as http_client:
        while tool_calls_made < MAX_AGENTIC_TOOL_CALLS:
            response = await client.messages.create(
                model=resolve_sdk_model(settings.synthesis_model, "synthesis"),
                max_tokens=1024,
                system=_AGENTIC_SYSTEM,
                tools=[_SEARCH_PUBMED_TOOL, _DONE_TOOL],
                messages=messages,
            )

            # Accumulate assistant turn
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                logger.info("Claude ended agentic retrieval for %s without calling done()", gene)
                break

            # Process all tool_use blocks in this turn
            tool_results = []
            called_done = False

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "done":
                    called_done = True
                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": "Acknowledged."}
                    )
                    continue

                if block.name == "search_pubmed":
                    query = block.input.get("query", "")
                    max_res = min(int(block.input.get("max_results", 10)), 20)
                    tool_calls_made += 1
                    logger.info(
                        "Claude search_pubmed [%d/%d] for %s: %s",
                        tool_calls_made, MAX_AGENTIC_TOOL_CALLS, gene, query,
                    )

                    try:
                        new_records = await _search_and_fetch(
                            query, max_res, http_client, set(accumulated.keys())
                        )
                        for r in new_records:
                            r.matched_query_tiers = list(set(r.matched_query_tiers) | {"tier2_agentic"})
                            accumulated[r.pmid] = r
                        result_text = (
                            f"Found {len(new_records)} new abstracts.\n"
                            + "\n".join(
                                f"PMID {r.pmid}: {r.title[:80]}\n{r.abstract[:200]}..."
                                for r in new_records
                            )
                        ) if new_records else "No new results for this query."
                    except httpx.HTTPError as exc:
                        result_text = f"Search failed: {exc}"

                    tool_results.append(
                        {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                    )

            # Feed all tool results back in a single user turn
            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if called_done or response.stop_reason != "tool_use":
                break

    total = len(accumulated)
    logger.info(
        "Tier 2 complete for %s: %d total abstracts (%d from agentic, %d tool calls)",
        gene, total, total - len(initial_records), tool_calls_made,
    )
    return _rank_records(_filter_retracted_records(list(accumulated.values())))


async def _tier2_local_retrieve(
    gene: str,
    fusions: List[str],
    initial_records: List[LiteratureRecord],
    local_backend: Optional[str],
    tumor_type: Optional[str] = None,
) -> List[LiteratureRecord]:
    """
    Local fallback retrieval for Claude Code mode.

    Claude Code cannot participate in Anthropic SDK tool-use loops, so ask it for
    concrete PubMed query strings, then execute those queries locally.
    """
    accumulated: Dict[str, LiteratureRecord] = {
        r.pmid: r for r in _filter_retracted_records(initial_records)
    }
    fusion_context = f"Associated fusions: {', '.join(fusions)}" if fusions else "Associated fusions: none"
    tumor_note = f"Tumor type: {_tumor_type_prompt_note(tumor_type)}" if tumor_type else ""
    initial_summary = _format_initial_records(initial_records)
    user_prompt = (
        f"Gene: {gene}\n"
        f"{fusion_context}\n"
        f"{tumor_note}\n\n"
        f"{initial_summary}\n\n"
        "Suggest up to 6 PubMed queries to find direct cancer-relevant evidence for this gene. "
        "Include aliases, fusion partner context, the supplied tumor type (and its aliases listed above), "
        "and pathway or protein-family terms when useful. "
        "Prioritize queries likely to surface human clinical evidence first, then cell-line studies, "
        "then mouse models. Return only queries that should be run against PubMed."
    )

    result = await complete_with_tool(
        model=settings.synthesis_model,
        system=_AGENTIC_SYSTEM,
        user=user_prompt,
        tool=_SUGGEST_QUERIES_TOOL,
        max_tokens=1024,
        local_mode=True,
        local_backend=local_backend,
        model_purpose="synthesis",
    )
    queries = [q for q in result.get("queries", []) if isinstance(q, str) and q.strip()]
    queries = list(dict.fromkeys(q.strip() for q in queries))[:MAX_AGENTIC_TOOL_CALLS]

    async with httpx.AsyncClient() as http_client:
        for i, query in enumerate(queries, start=1):
            logger.info(
                "Local agent suggested PubMed query [%d/%d] for %s: %s",
                i,
                len(queries),
                gene,
                query,
            )
            try:
                new_records = await _search_and_fetch(
                    query, 20, http_client, set(accumulated.keys())
                )
            except httpx.HTTPError as exc:
                logger.warning("Local Tier 2 PubMed query failed for %s: %s", gene, exc)
                continue
            for record in new_records:
                record.matched_query_tiers = list(set(record.matched_query_tiers) | {"tier2_agentic"})
                accumulated[record.pmid] = record

    logger.info(
        "Local Tier 2 complete for %s: %d total abstracts (%d from suggested queries)",
        gene,
        len(accumulated),
        len(accumulated) - len(initial_records),
    )
    return _rank_records(_filter_retracted_records(list(accumulated.values())))


# ---------------------------------------------------------------------------
# Composite pre-ranking heuristic
#
# Applied to the deduplicated retrieval pool between Tier 1/2 retrieval and the
# Haiku citation selection pass, so Haiku sees the strongest candidates first
# and there's a better deterministic fallback than recency alone on failure.
# Four signals, each normalized to [0, 1], combined into a weighted average
# (weights renormalized per-paper over whichever signals actually apply — e.g.
# fusion co-occurrence doesn't apply to standalone, non-fusion gene lookups).
# ---------------------------------------------------------------------------

_QUERY_TIER_PRECISION: Dict[str, float] = {
    "mesh_gene_name": 1.0,
    "tumor_type": 0.9,
    "evidence_priority": 0.75,
    "fusion_partner": 0.75,
    "tier2_agentic": 0.5,
    "free_text": 0.5,
}

_PUBTYPE_CATEGORY_PATTERNS: Dict[str, tuple[str, ...]] = {
    "trial": ("clinical trial", "randomized controlled trial"),
    "comparative": ("comparative study",),
    "meta_analysis": ("meta-analysis",),
    "review": ("review", "systematic review"),
    "case_report": ("case reports",),
    "editorial": ("letter", "comment", "editorial", "news"),
}


def _query_tier_score(record: LiteratureRecord) -> float:
    """Highest precision weight among the query families that surfaced this PMID.
    Untagged records (e.g. constructed outside the retrieval pipeline) get a
    neutral score rather than being penalized for missing provenance."""
    if not record.matched_query_tiers:
        return 0.5
    return max(_QUERY_TIER_PRECISION.get(family, 0.5) for family in record.matched_query_tiers)


def _fusion_cooccurrence_score(record: LiteratureRecord, partner_genes: List[str]) -> Optional[float]:
    """Fraction of the fusion's other partner gene(s) mentioned in title+abstract.
    None (not zero) for standalone, non-fusion annotations — this signal doesn't
    apply, and is excluded from the composite rather than counted as a miss."""
    if not partner_genes:
        return None
    text = f"{record.title} {record.abstract}".lower()
    mentioned = sum(
        1
        for partner in partner_genes
        if re.search(rf"(?<![a-z0-9]){re.escape(partner.lower())}(?![a-z0-9])", text)
    )
    return mentioned / len(partner_genes)


def _recency_score(record: LiteratureRecord) -> float:
    """Exponential decay by publication age; missing year gets a neutral score
    rather than being penalized as if it were maximally stale."""
    if not record.publication_year:
        return 0.5
    half_life = max(settings.citation_score_recency_half_life_years, 0.1)
    age_years = max(0, datetime.now(timezone.utc).year - record.publication_year)
    return 0.5 ** (age_years / half_life)


def _matched_pubtype_categories(record: LiteratureRecord) -> List[str]:
    types_lower = [t.lower() for t in record.publication_types]
    matched = [
        category
        for category, patterns in _PUBTYPE_CATEGORY_PATTERNS.items()
        if any(pattern in t for t in types_lower for pattern in patterns)
    ]
    return matched or ["original_research"]


def _pubtype_weight_table(profile: str) -> Dict[str, float]:
    if profile == "context":
        return {
            "trial": settings.context_score_pubtype_trial_weight,
            "comparative": settings.context_score_pubtype_comparative_weight,
            "meta_analysis": settings.context_score_pubtype_meta_analysis_weight,
            "review": settings.context_score_pubtype_review_weight,
            "case_report": settings.context_score_pubtype_case_report_weight,
            "editorial": settings.context_score_pubtype_editorial_weight,
            "original_research": settings.context_score_pubtype_original_research_weight,
        }
    return {
        "trial": settings.citation_score_pubtype_trial_weight,
        "comparative": settings.citation_score_pubtype_comparative_weight,
        "meta_analysis": settings.citation_score_pubtype_meta_analysis_weight,
        "review": settings.citation_score_pubtype_review_weight,
        "case_report": settings.citation_score_pubtype_case_report_weight,
        "editorial": settings.citation_score_pubtype_editorial_weight,
        "original_research": settings.citation_score_pubtype_original_research_weight,
    }


def _pubtype_score(record: LiteratureRecord, profile: str) -> float:
    """profile is 'citation' (favors original research/trials) or 'context' (favors
    reviews/meta-analyses). A paper can match multiple categories; take the max."""
    table = _pubtype_weight_table(profile)
    categories = _matched_pubtype_categories(record)
    return max(table.get(category, table["original_research"]) for category in categories)


def _weighted_composite(signals: Dict[str, Optional[float]], weights: Dict[str, float]) -> float:
    applicable = {name: value for name, value in signals.items() if value is not None}
    weight_total = sum(weights[name] for name in applicable)
    if weight_total <= 0:
        return 0.0
    return sum(weights[name] * value for name, value in applicable.items()) / weight_total


def score_literature_records(
    records: List[LiteratureRecord],
    gene: str,
    fusions: Optional[List[str]] = None,
) -> List[LiteraturePaperScore]:
    """Compute the composite pre-ranking score (and its component signals) for every
    record in a retrieval pool. Pure/deterministic — no network calls — so it's cheap
    to recompute wherever the breakdown is needed for auditability."""
    partner_genes = _fusion_partners(gene, fusions or [])
    weights = {
        "query_tier": settings.citation_score_query_tier_weight,
        "fusion": settings.citation_score_fusion_cooccurrence_weight,
        "recency": settings.citation_score_recency_weight,
        "pubtype": settings.citation_score_publication_type_weight,
    }

    scores: List[LiteraturePaperScore] = []
    for record in records:
        query_tier = _query_tier_score(record)
        fusion = _fusion_cooccurrence_score(record, partner_genes)
        recency = _recency_score(record)
        pubtype_citation = _pubtype_score(record, "citation")
        pubtype_context = _pubtype_score(record, "context")

        citation_composite = _weighted_composite(
            {"query_tier": query_tier, "fusion": fusion, "recency": recency, "pubtype": pubtype_citation},
            weights,
        )
        context_composite = _weighted_composite(
            {"query_tier": query_tier, "fusion": fusion, "recency": recency, "pubtype": pubtype_context},
            weights,
        )

        scores.append(
            LiteraturePaperScore(
                pmid=record.pmid,
                citation_composite_score=round(citation_composite, 4),
                context_composite_score=round(context_composite, 4),
                query_tier_score=round(query_tier, 4),
                fusion_cooccurrence_score=None if fusion is None else round(fusion, 4),
                recency_score=round(recency, 4),
                publication_type_citation_score=round(pubtype_citation, 4),
                publication_type_context_score=round(pubtype_context, 4),
                matched_query_tiers=list(record.matched_query_tiers),
                publication_year=record.publication_year,
            )
        )
    return scores


def rank_literature_for_synthesis(
    records: List[LiteratureRecord],
    gene: str,
    fusions: Optional[List[str]],
    max_papers: int,
) -> tuple[List[LiteratureRecord], List[LiteraturePaperScore]]:
    """
    Build the ranked candidate pool handed to the Haiku selection pass (and used
    as its algorithmic fallback on failure): the top `max_papers` by citation-
    weighted composite score (favors original research/trials — what synthesis
    draws verified PMID citations from), plus a small supplement of review-leaning
    papers pulled from the context-weighted ranking for summary framing only.

    Returns (candidate_pool, scores) — scores cover the FULL input pool (for
    retrieval-level auditability), with `pool` set on the subset that made the
    candidate cut.
    """
    scores = score_literature_records(records, gene, fusions)
    scores_by_pmid = {score.pmid: score for score in scores}
    records_by_pmid = {record.pmid: record for record in records}

    citation_ranked = sorted(
        records, key=lambda r: scores_by_pmid[r.pmid].citation_composite_score, reverse=True
    )
    context_ranked = sorted(
        records, key=lambda r: scores_by_pmid[r.pmid].context_composite_score, reverse=True
    )

    primary = citation_ranked[: max(max_papers, 0)]
    primary_pmids = {record.pmid for record in primary}
    supplement_count = max(settings.citation_score_review_supplement_count, 0)
    supplement = [r for r in context_ranked if r.pmid not in primary_pmids][:supplement_count]

    for record in primary:
        scores_by_pmid[record.pmid].pool = "citation"
    for record in supplement:
        scores_by_pmid[record.pmid].pool = "context_supplement"

    candidate_pool = [records_by_pmid[r.pmid] for r in primary + supplement]
    return candidate_pool, scores


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def retrieve_literature(
    gene: str,
    fusions: Optional[List[str]] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
    tumor_type: Optional[str] = None,
) -> tuple:
    """
    Two-tier retrieval with automatic fallthrough.

    Tier 1: direct NCBI query (always runs first, cheap).
    Tier 2: Claude agentic retrieval (only when Tier 1 is insufficient).

    The threshold is settings.min_papers_for_strong_association (default 4).
    Records are ranked by evidence tier (clinical > cell line > mouse > other),
    then high-impact journal, then recency before being passed downstream.

    Returns (records, tier) where tier is 1 or 2.
    """
    try:
        records = _filter_retracted_records(await _tier1_retrieve(gene, fusions, tumor_type))
    except httpx.HTTPError as exc:
        logger.error("Tier 1 NCBI call failed for %s: %s", gene, exc)
        records = []

    if len(records) >= settings.min_papers_for_strong_association:
        logger.info(
            "Tier 1 sufficient for %s (%d papers) — skipping Claude retrieval",
            gene, len(records),
        )
        return records, 1

    logger.info(
        "Tier 1 insufficient for %s (%d < %d papers) — falling through to Claude",
        gene, len(records), settings.min_papers_for_strong_association,
    )
    if local_mode:
        records = await _tier2_local_retrieve(gene, fusions or [], records, local_backend, tumor_type)
    else:
        records = await _tier2_agentic_retrieve(gene, fusions or [], records, tumor_type)
    return _filter_retracted_records(records), 2
