"""Deterministic citation precision filters."""

from __future__ import annotations

import re
from typing import Iterable, List

from src.models.schema import LiteratureRecord

CANCER_TERMS = (
    "cancer",
    "tumor",
    "tumour",
    "carcinoma",
    "neoplasm",
    "malignan",
    "leukemia",
    "leukaemia",
    "lymphoma",
    "sarcoma",
    "glioma",
    "melanoma",
)

EVIDENCE_TERMS = (
    "knockdown",
    "overexpression",
    "overexpressed",
    "mutation",
    "mutant",
    "methylation",
    "copy number",
    "amplification",
    "deletion",
    "xenograft",
    "proliferation",
    "invasion",
    "migration",
    "survival",
    "prognosis",
    "resistance",
    "sensitivity",
    "fusion",
    "rearrangement",
    "translocation",
)

WEAK_CONTEXT_TERMS = (
    "signature",
    "screen",
    "gene set",
    "panel",
    "biomarker panel",
    "differentially expressed genes",
)

NONCODING_ENTITY_PATTERNS = (
    "lncrna",
    "long non-coding rna",
    "long noncoding rna",
    "circular rna",
    "circrna",
    "mirna",
    "microrna",
)


def _contains_word(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", text))


def _identity_is_noncoding(gene: str, gene_identity: str | None) -> bool:
    identity = (gene_identity or "").lower()
    return (
        gene.startswith("LINC")
        or "non-coding rna" in identity
        or "noncoding rna" in identity
        or "rna gene" in identity
        or "pseudogene" in identity
    )


def _looks_like_different_noncoding_entity(
    gene: str,
    record: LiteratureRecord,
    gene_identity: str | None,
) -> bool:
    if _identity_is_noncoding(gene, gene_identity):
        return False

    text = f"{record.title} {record.abstract}".lower()
    gene_lower = gene.lower()
    for pattern in NONCODING_ENTITY_PATTERNS:
        if re.search(rf"\b{re.escape(pattern)}\s+{re.escape(gene_lower)}\b", text):
            return True
        if re.search(rf"\b{re.escape(gene_lower)}\s+{re.escape(pattern)}\b", text):
            return True
    return False


def citation_support_score(
    gene: str,
    record: LiteratureRecord,
    gene_identity: str | None = None,
) -> int:
    """Score how directly a retrieved abstract supports citing this HGNC gene."""
    if _looks_like_different_noncoding_entity(gene, record, gene_identity):
        return -100

    title = record.title.lower()
    abstract = record.abstract.lower()
    gene_lower = gene.lower()
    score = 0

    if _contains_word(title, gene_lower):
        score += 5
    if _contains_word(abstract, gene_lower):
        score += 2

    combined = f"{title} {abstract}"
    if any(term in title for term in CANCER_TERMS):
        score += 3
    elif any(term in combined for term in CANCER_TERMS):
        score += 2

    score += min(3, sum(1 for term in EVIDENCE_TERMS if term in combined))

    if any(term in combined for term in WEAK_CONTEXT_TERMS):
        score -= 2
    if "review" in title:
        score -= 1

    return score


def filter_and_rank_citations(
    gene: str,
    emitted_citations: Iterable[str],
    records: List[LiteratureRecord],
    max_citations: int,
    gene_identity: str | None = None,
    min_score: int = 4,
) -> List[str]:
    """
    Deduplicate, verify, identity-filter, score, and cap emitted PMIDs.

    The LLM still decides which papers it used. This function only removes
    weak or ambiguous citations and reorders the survivors by direct support.
    """
    records_by_pmid = {record.pmid: record for record in records}
    ranked: list[tuple[int, int, str]] = []

    for position, pmid in enumerate(dict.fromkeys(emitted_citations)):
        if not isinstance(pmid, str) or pmid not in records_by_pmid:
            continue
        score = citation_support_score(gene, records_by_pmid[pmid], gene_identity)
        if score < min_score:
            continue
        ranked.append((score, -position, pmid))

    ranked.sort(reverse=True)
    return [pmid for _, _, pmid in ranked[:max_citations]]
