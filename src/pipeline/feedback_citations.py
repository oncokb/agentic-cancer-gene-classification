"""
Automated citation checks for curator feedback that cites PMIDs.

When feedback names one or more PubMed papers, fetch each paper's title and
abstract through the existing NCBI E-utilities helper (shared client, rate
limiter, and cache) and report whether the paper actually mentions the
feedback's subject gene/fusion and tumor type. The result is appended to the
GitHub issue, never used to block or fail feedback submission: any timeout or
error degrades to a "check unavailable" line.

Only title/abstract are checked. There is no full-text retrieval helper in
this codebase, so the rendered section says so explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Iterable, List, Optional, Sequence, Tuple

from src.config import settings
from src.pipeline import literature
from src.pipeline.literature import _TUMOR_TYPE_ALIASES

logger = logging.getLogger(__name__)

# Six digits covers every PMID assigned since the 1970s; nine is the practical
# upper bound. Shorter numbers are too likely to be dates, counts, or IDs.
_PMID_LABEL = re.compile(
    r"\b(?:pmids?|pubmed(?:\s+ids?)?)\s*(?:numbers?|nos?\.?)?\s*[:#]?\s*"
    r"((?:\d{6,9})(?:(?:\s*(?:,|;|and|&)\s*|\s+)\d{6,9})*)",
    re.IGNORECASE,
)
_PMID_DIGITS = re.compile(r"\d{6,9}")
_PMID_URL = re.compile(
    r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{6,9})",
    re.IGNORECASE,
)
_FUSION_SUBJECT = re.compile(
    r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{1,9})\s*(?:::+|--|/|-)\s*([A-Z][A-Z0-9]{1,9})(?![A-Za-z0-9])"
)
_NAMED_GENE_SUBJECT = re.compile(
    r"\b(?:gene|symbol)\s+([A-Z][A-Z0-9-]{1,9})\b",
    re.IGNORECASE,
)
_GENERIC_TUMOR = re.compile(
    r"\b((?:[A-Za-z][A-Za-z-]+\s+){0,3}"
    r"(?:cancer|carcinoma|sarcoma|leukemia|lymphoma|melanoma|glioma|mesothelioma|"
    r"myeloma|tumou?r))\b",
    re.IGNORECASE,
)
_LEADING_STOPWORDS = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on",
    "or", "the", "to", "with",
})
_GENERIC_TUMOR_WORDS = frozenset({
    "cancer", "carcinoma", "sarcoma", "leukemia", "lymphoma", "melanoma", "glioma",
    "mesothelioma", "myeloma", "tumor", "tumour",
})

_SECTION_HEADING = "## Automated citation check"
_ABSTRACT_CAVEAT = "abstract checked; full text not checked"


def parse_cited_pmids(
    text: Optional[str],
    structured_pmids: Optional[Iterable[str]] = None,
    *,
    max_pmids: Optional[int] = None,
) -> List[str]:
    """Return unique PMIDs cited in free text and/or a structured field, in order."""
    found: List[str] = []

    def _add(value: str) -> None:
        if value and value not in found:
            found.append(value)

    for value in structured_pmids or ():
        match = re.search(r"\b(\d{6,9})\b", str(value))
        if match:
            _add(match.group(1))
    source = text or ""
    for pattern in (_PMID_LABEL, _PMID_URL):
        for match in pattern.finditer(source):
            for pmid in _PMID_DIGITS.findall(match.group(1)):
                _add(pmid)

    limit = max_pmids if max_pmids is not None else settings.feedback_citation_max_pmids
    return found[: max(0, limit)]


def extract_subject(message: str, gene: Optional[str] = None) -> Tuple[Optional[str], bool]:
    """Return (subject, is_fusion) from the structured gene field or the message."""
    if gene and gene.strip():
        subject = gene.strip()
        return subject, bool(_split_subject(subject)[1])
    match = _FUSION_SUBJECT.search(message or "")
    if match:
        return f"{match.group(1)}::{match.group(2)}", True
    match = _NAMED_GENE_SUBJECT.search(message or "")
    if match:
        return match.group(1).upper(), False
    return None, False


def extract_tumor_type(message: str) -> Optional[str]:
    """Return the first recognizable tumor type mentioned in the feedback."""
    source = message or ""
    terms = {
        value
        for key, aliases in _TUMOR_TYPE_ALIASES.items()
        for value in (key, *aliases)
    }
    for term in sorted(terms, key=len, reverse=True):
        if len(term) < 4 or term.isdigit():
            continue
        case_sensitive = term.isupper()
        if _contains(source, term, case_sensitive=case_sensitive):
            return term
    match = _GENERIC_TUMOR.search(source)
    if not match:
        return None
    words = match.group(1).split()
    while words and words[0].lower() in _LEADING_STOPWORDS:
        words.pop(0)
    return " ".join(words) or None


def _split_subject(subject: str) -> Tuple[str, Optional[str]]:
    match = re.match(r"^(.+?)\s*(::+|--|/|-)\s*(.+)$", subject.strip())
    if not match:
        return subject.strip(), None
    left, separator, right = match.group(1).strip(), match.group(2), match.group(3).strip()
    if not left or not right:
        return subject.strip(), None
    # A single hyphen is ambiguous: "GREB1-BCL9L" is a fusion, but "HLA-A" is
    # one gene symbol. Require two multi-character uppercase symbols for a
    # hyphenated fusion; "::", "--", and "/" are unambiguous.
    if separator == "-" and (
        len(left) < 2
        or len(right) < 2
        or not re.fullmatch(r"[A-Z][A-Z0-9]+", left)
        or not re.fullmatch(r"[A-Z][A-Z0-9]+", right)
    ):
        return subject.strip(), None
    return left, right


def _contains(text: str, term: str, *, case_sensitive: bool = False) -> bool:
    if not term:
        return False
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
    return bool(re.search(pattern, text or "", flags))


def _tumor_mentioned(text: str, tumor: Optional[str]) -> bool:
    if not tumor:
        return False
    if _contains(text, tumor):
        return True
    distinctive = [
        word
        for word in re.findall(r"[A-Za-z0-9-]+", tumor)
        if len(word) >= 4 and word.casefold() not in _GENERIC_TUMOR_WORDS
    ]
    return any(_contains(text, word) for word in distinctive)


def _yes(flag: bool) -> str:
    return "yes" if flag else "NO"


async def _resolve_aliases(five: str, three: Optional[str]) -> Tuple[List[str], List[str]]:
    """Best-effort HGNC alias lookup; a failure just means canonical-only matching."""
    try:
        timeout = settings.feedback_citation_alias_timeout_seconds
        if three:
            return await asyncio.wait_for(
                literature.resolve_fusion_partner_aliases(five, three), timeout=timeout
            )
        aliases = await asyncio.wait_for(literature._resolve_one_partner_aliases(five), timeout=timeout)
        return aliases, []
    except Exception as exc:  # noqa: BLE001 - alias data is optional, never fatal
        logger.info("Feedback citation alias lookup unavailable: %s", exc)
        return [], []


def _record_line(
    pmid: str,
    record,
    five: str,
    three: Optional[str],
    aliases_five: Sequence[str],
    aliases_three: Sequence[str],
    tumor: Optional[str],
) -> str:
    text = f"{record.title} {record.abstract}"
    five_terms = list(dict.fromkeys([five, *aliases_five]))
    five_hit = any(_contains(text, term) for term in five_terms)
    if three:
        three_terms = list(dict.fromkeys([three, *aliases_three]))
        three_hit = any(_contains(text, term) for term in three_terms)
        detail = f"mentions {five} — {_yes(five_hit)}; {three} — {_yes(three_hit)}"
    else:
        detail = f"mentions {five} — {_yes(five_hit)}"
    if tumor:
        detail += f"; {tumor} — {_yes(_tumor_mentioned(text, tumor))}"
    return f"- PMID {pmid}: {detail} ({_ABSTRACT_CAVEAT})"


async def _build_section(
    pmids: Sequence[str],
    message: str,
    gene: Optional[str],
) -> Optional[str]:
    subject, _ = extract_subject(message, gene)
    tumor = extract_tumor_type(message)
    if not subject:
        return None
    five, three = _split_subject(subject)
    aliases_five, aliases_three = await _resolve_aliases(five, three)

    records = await literature._efetch(list(pmids), literature._get_ncbi_client())
    by_pmid = {record.pmid: record for record in records}
    lines: List[str] = []
    for pmid in pmids:
        record = by_pmid.get(pmid)
        if record is None:
            lines.append(f"- PMID {pmid}: check unavailable (abstract not retrieved)")
            continue
        lines.append(_record_line(pmid, record, five, three, aliases_five, aliases_three, tumor))

    return "\n".join(
        [
            _SECTION_HEADING,
            "",
            "Automated, informational only; title/abstract were checked, not full text.",
            "",
            *lines,
        ]
    )


def _unavailable(reason: str) -> str:
    return "\n".join([_SECTION_HEADING, "", f"check unavailable ({reason})."])


async def build_citation_check_section(
    message: str,
    *,
    gene: Optional[str] = None,
    structured_pmids: Optional[Iterable[str]] = None,
) -> Optional[str]:
    """Render the citation-check Markdown section, or None when no PMIDs are cited.

    Never raises: any failure or timeout becomes a short "check unavailable"
    section so feedback submission and issue creation always continue.
    """
    if not settings.feedback_citation_check_enabled:
        return None
    pmids = parse_cited_pmids(message, structured_pmids)
    if not pmids:
        return None
    try:
        section = await asyncio.wait_for(
            _build_section(pmids, message, gene),
            timeout=settings.feedback_citation_check_timeout_seconds,
        )
        return section or _unavailable("no subject gene or fusion detected")
    except asyncio.TimeoutError:
        logger.warning("Feedback citation check timed out for %d PMID(s)", len(pmids))
        return _unavailable("PubMed request timed out")
    except Exception:  # noqa: BLE001 - feedback submission must never fail here
        logger.exception("Feedback citation check failed")
        return _unavailable("PubMed request failed")
