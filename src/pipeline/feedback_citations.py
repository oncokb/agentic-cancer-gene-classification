"""
Automated citation checks for curator feedback that cites PMIDs.

When feedback names one or more PubMed papers, fetch each paper's title and
abstract through the existing NCBI E-utilities helper (shared client, rate
limiter, and cache) and report whether the paper actually mentions the
feedback's subject gene/fusion and tumor type. The result is appended to the
GitHub issue, never used to block or fail feedback submission: any timeout or
error degrades to a "check unavailable" line.

All-letter gene terms (any length) match case-sensitively; only terms with a
digit, a hyphen, or mixed case (C11orf95, c-Kit, Notch1) match
case-insensitively. Tumor abbreviations are always case-sensitive, and full
tumor names are matched as explicit alias phrases with dash/space and plural
normalization. There is no generic organ-equivalence rule: a missed match is
preferable to a wrong "found".

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
# upper bound. Shorter numbers are too likely to be dates, counts, or IDs, and
# the lookarounds reject long numbers instead of truncating them.
_PMID_ATOM = r"(?<!\d)\d{6,9}(?!\d)"
_PMID_DIGITS = re.compile(r"(?<!\d)\d{6,9}(?!\d)")
_STRUCTURED_PMID_PREFERRED = re.compile(r"(?<!\d)\d{6,9}(?!\d)")
_STRUCTURED_PMID_FALLBACK = re.compile(r"(?<!\d)\d{1,10}(?!\d)")
_PMCID = re.compile(r"(?i)\bpmc\s*\d+|\bpmcid\b")
_DOI_PREFIX = re.compile(r"(?i)^\s*(?:doi\b|https?://(?:dx\.)?doi\.org/)")
_PMID_SEPARATOR = r"\s*(?:,|;|and|&)\s*"
# A singular label only accepts explicit separators; a plural label also
# accepts whitespace-separated lists ("PMIDs 11111111 22222222").
_PMID_LABEL_SINGULAR = re.compile(
    r"\b(?:pmid|pubmed(?:\s+id)?)\s*(?:number|no\.?)?\s*[:#]?\s*"
    rf"({_PMID_ATOM}(?:{_PMID_SEPARATOR}{_PMID_ATOM})*)",
    re.IGNORECASE,
)
_PMID_LABEL_PLURAL = re.compile(
    r"\b(?:pmids|pubmed\s+ids)\s*(?:numbers|nos?\.?)?\s*[:#]?\s*"
    rf"({_PMID_ATOM}(?:(?:{_PMID_SEPARATOR}|\s+){_PMID_ATOM})*)",
    re.IGNORECASE,
)
_PMID_URL = re.compile(
    rf"pubmed\.ncbi\.nlm\.nih\.gov/({_PMID_ATOM})",
    re.IGNORECASE,
)
# Gene-like tokens are validated by _is_gene_symbol after matching; the regex
# stays broad so digit-bearing mixed-case symbols (C11orf95, Notch1) survive.
_GENE_TOKEN = r"[A-Za-z][A-Za-z0-9]{1,15}"
_FUSION_SUBJECT = re.compile(
    rf"(?<![A-Za-z0-9])({_GENE_TOKEN})\s*(::+|--|/|-)\s*"
    rf"({_GENE_TOKEN})(?![A-Za-z0-9])"
)
_NAMED_GENE_SUBJECT = re.compile(
    r"\b(?:gene|symbol)\s+([A-Za-z][A-Za-z0-9-]{1,9})\b",
    re.IGNORECASE,
)
_GENERIC_TUMOR = re.compile(
    r"\b((?:[A-Za-z][A-Za-z-]+\s+){1,3}"
    r"(?:cancer|carcinoma|sarcoma|leukemia|lymphoma|melanoma|glioma|mesothelioma|"
    r"myeloma|tumou?r))\b",
    re.IGNORECASE,
)
# Organ/site adjectives allowed to form a previously-unseen generic tumor phrase
# (e.g. "biliary tract cancer"). Without this, "the annotation for this tumor"
# would be extracted as a tumor type.
_ORGAN_SITE_WORDS = frozenset({
    "adrenal", "anal", "appendix", "bile", "biliary", "bladder", "blood", "bone",
    "brain", "breast", "bronchial", "cervical", "colon", "colorectal", "duodenal",
    "endometrial", "esophageal", "gastric", "gastroesophageal", "head", "hepatic",
    "kidney", "laryngeal", "liver", "lung", "mediastinal", "melanocytic", "neck",
    "oral", "oropharyngeal", "ovarian", "pancreatic", "pelvic", "penile",
    "peritoneal", "pleural", "prostate", "rectal", "renal", "salivary", "skin",
    "stomach", "testicular", "thymic", "thyroid", "tongue", "ureteral",
    "urethral", "uterine", "vaginal", "vulvar",
})
_COMMON_ENGLISH_WORDS = frozenset({
    "all", "amp", "and", "are", "art", "ask", "bad", "bag", "bar", "bat", "bed",
    "big", "bit", "box", "boy", "bus", "but", "can", "cap", "car", "cat", "cop",
    "cow", "cry", "cup", "cut", "dad", "day", "did", "die", "dog", "dry", "ear",
    "eat", "egg", "end", "eye", "far", "fat", "few", "fig", "fit", "fix", "fly",
    "for", "fox", "fun", "gap", "gas", "gel", "get", "god", "got", "gun", "guy",
    "had", "has", "hat", "her", "him", "his", "hit", "hot", "how", "ice", "ill",
    "its", "jam", "job", "jog", "key", "kid", "kit", "lab", "lap", "law", "leg",
    "let", "lie", "lip", "log", "lot", "low", "mad", "man", "map", "mat", "men",
    "met", "mix", "mom", "mud", "net", "new", "not", "now", "nut", "odd", "off",
    "oil", "old", "one", "our", "out", "own", "pan", "pay", "pen", "pet", "pie",
    "pig", "pin", "pit", "pot", "put", "ran", "rat", "raw", "red", "rid", "rob",
    "row", "rub", "run", "sad", "saw", "say", "sea", "see", "set", "she", "shy",
    "sin", "sir", "sit", "six", "ski", "sky", "son", "sun", "tab", "tag", "tan",
    "tap", "tax", "tea", "ten", "the", "tie", "tin", "tip", "toe", "ton", "too",
    "top", "toy", "try", "two", "use", "van", "war", "was", "way", "web", "wet",
    "who", "why", "win", "yes", "yet", "you", "zoo",
})
_ASSAY_NAMES = frozenset({
    "CDNA", "CNV", "CR", "CT", "DNA", "ELISA", "EMA", "ER", "FDA", "FISH",
    "FFPE", "ICI", "IHC", "ISH", "MRI", "MRNA", "MSI", "NGS", "ORR", "OS",
    "PCR", "PD", "PD-L1", "PDL1", "PET", "PFS", "PMCID", "PMID", "PR", "RNA",
    "RNASEQ", "RRNA", "RTPCR", "SANGER", "SEQ", "SNP", "TKI", "TMB", "TRNA",
    "WB", "WES", "WGS", "WT",
})
# Prefixes of hyphenated gene symbols (HLA-DRB1, MT-CO1) that must not be read
# as fusions just because they contain a hyphen.
_HYPHENATED_GENE_PREFIXES = frozenset({"HLA", "MT", "TRB", "TRA", "IGH", "IGK", "IGL"})
_VARIANT_PREFIXES = ("EXON", "DEL", "INS", "MUT")
_FUSION_KEYWORD = (
    r"(?:fusion|fusions|fused|chimeric|rearrangement|rearranged|translocation)"
)
# A "/" or "-" pair is only a fusion when one of the fusion words is directly
# adjacent to it, or when it is explicitly "-positive" / " positive". Arbitrary
# fusion wording elsewhere in the message (e.g. after an unrelated
# "TP53/EGFR co-mutation") must not turn every pair into a fusion.
_FUSION_BEFORE_PAIR = re.compile(
    rf"{_FUSION_KEYWORD}"
    r"(?:\s*[-/]?\s*(?:positive|driven|associated))?"
    r"[\s:,(]*$",
    re.IGNORECASE,
)
_FUSION_AFTER_PAIR = re.compile(
    rf"^\s*-?\s*(?:positive|driven|associated)?\s*{_FUSION_KEYWORD}\b",
    re.IGNORECASE,
)
# A "/" pair is not a fusion just because it is "rearranged" (double-hit
# lymphoma: MYC/BCL2 rearranged); keep that wording for "-" pairs only.
_FUSION_AFTER_PAIR_SLASH = re.compile(
    r"^\s*-?\s*(?:positive|driven|associated)?\s*"
    r"(?:fusion|fusions|fused|chimeric|rearrangement|translocation)\b",
    re.IGNORECASE,
)
# "BCR-ABL1-positive"/"BCR-ABL1 positive CML" is a common fusion notation even
# without the word "fusion"; both sides still have to be valid gene symbols.
# Only "-" pairs qualify: "TP53/KRAS-positive" is co-expression, not a fusion.
_POSITIVE_AFTER_PAIR = re.compile(r"^\s*-?\s*positive\b", re.IGNORECASE)
_NEGATIVE_QUALIFIERS = r"(?:negative|wild[\s-]*type|absent)"
# Negation next to the pair always wins: "fusion-negative EGFR/ALK",
# "EGFR/ALK fusion-negative", "wild-type BCR/ABL1", "no BCR/ABL1 fusion",
# "no evidence of BCR-ABL1 fusion", "BCR-ABL1 fusion not detected".
_NEGATIVE_BEFORE_PAIR = re.compile(
    rf"(?:\bno\s+(?:evidence\s+of\s+)?(?:{_FUSION_KEYWORD}\s*)?$)"
    rf"|(?:(?:{_FUSION_KEYWORD})\s*[-:]?\s*)?(?:{_NEGATIVE_QUALIFIERS})\s*$",
    re.IGNORECASE,
)
_NEGATIVE_AFTER_PAIR = re.compile(
    rf"^\s*(?:(?:{_FUSION_KEYWORD})\s*[-:]?\s*)?"
    rf"(?:{_NEGATIVE_QUALIFIERS}|no|not\s+detected)\b",
    re.IGNORECASE,
)
# Co-mutation/co-occurrence wording directly after a pair means the pair is not
# a fusion, even when a fusion word sits before it.
_COOCCURRENCE_AFTER_PAIR = re.compile(
    r"^\s*[- ]?\s*(?:co-(?:mut|occur|alter)\w*|mutat\w*)\b",
    re.IGNORECASE,
)
_TUMOR_DASHES = str.maketrans({
    "-": " ", "\u2010": " ", "\u2011": " ", "\u2012": " ", "\u2013": " ",
    "\u2014": " ", "\u2212": " ",
})
# Explicit, curated histology phrases for common tumor types. These are kept
# deliberately narrow: a wrong "found" is worse than a missed match.
_EXTRA_TUMOR_ALIASES: dict[str, Tuple[str, ...]] = {
    "ovarian cancer": (
        "ovarian carcinoma",
        "serous ovarian carcinoma",
        "high-grade serous ovarian carcinoma",
        "epithelial ovarian cancer",
    ),
}

_SECTION_HEADING = "## Automated citation check"
_ABSTRACT_CAVEAT = "abstract checked; full text not checked"


def _tumor_groups() -> dict[str, Tuple[str, ...]]:
    groups: dict[str, Tuple[str, ...]] = {}
    for key, aliases in _TUMOR_TYPE_ALIASES.items():
        # Lowercase lookup keys ("read", "meso", "sarc") are English words and
        # must never be match terms. Short abbreviation keys contribute their
        # uppercase form instead; multi-word keys are valid phrases.
        if " " in key or len(key) > 5:
            key_terms = [key]
        else:
            key_terms = [key.upper()]
        group = [*key_terms, *aliases]
        for canonical, extra in _EXTRA_TUMOR_ALIASES.items():
            if canonical in group:
                group.extend(extra)
        deduped = tuple(dict.fromkeys(group))
        for term in deduped:
            groups.setdefault(term.casefold(), deduped)
    return groups


_TUMOR_GROUPS = _tumor_groups()
_TUMOR_TERMS = sorted(
    {term for group in _TUMOR_GROUPS.values() for term in group},
    key=len,
    reverse=True,
)
_TUMOR_ABBREVIATIONS = frozenset(
    term
    for group in _TUMOR_GROUPS.values()
    for term in group
    if term.isupper() and 2 <= len(term) <= 6
)


def _parse_structured_pmid(raw: str) -> Tuple[Optional[str], Optional[str]]:
    """Return (pmid, None) or (None, reason) for one structured PMID value.

    A 6-9 digit run is preferred; a single 1-10 digit number is accepted as a
    fallback. Anything with several candidate numbers (or an unsupported ID
    type) is reported instead of guessed, because a wrong PMID would check the
    wrong paper.
    """
    text = (raw or "").strip()
    if not text:
        return None, None
    if _PMCID.search(text):
        return None, "PMCID not supported"
    if _DOI_PREFIX.match(text):
        return None, "DOI not supported"
    preferred = _STRUCTURED_PMID_PREFERRED.findall(text)
    if len(preferred) == 1:
        return preferred[0], None
    if len(preferred) > 1:
        return None, "multiple candidate numbers"
    fallback = _STRUCTURED_PMID_FALLBACK.findall(text)
    if len(fallback) == 1:
        return fallback[0], None
    if len(fallback) > 1:
        return None, "multiple candidate numbers"
    return None, "no PMID found"


def _collect_cited_pmids(
    text: Optional[str],
    structured_pmids: Optional[Iterable[str]] = None,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    found: List[str] = []
    unparseable: List[Tuple[str, str]] = []

    def _add(value: str) -> None:
        if value and value not in found:
            found.append(value)

    for value in structured_pmids or ():
        raw = str(value).strip()
        if not raw:
            continue
        pmid, reason = _parse_structured_pmid(raw)
        if pmid:
            _add(pmid)
        elif reason:
            unparseable.append((raw, reason))
    source = text or ""
    for pattern in (_PMID_LABEL_PLURAL, _PMID_LABEL_SINGULAR, _PMID_URL):
        for match in pattern.finditer(source):
            for pmid in _PMID_DIGITS.findall(match.group(1)):
                _add(pmid)
    return found, unparseable


def parse_cited_pmids(
    text: Optional[str],
    structured_pmids: Optional[Iterable[str]] = None,
    *,
    max_pmids: Optional[int] = None,
) -> List[str]:
    """Return unique PMIDs cited in free text and/or a structured field, in order."""
    found, _ = _collect_cited_pmids(text, structured_pmids)
    limit = max_pmids if max_pmids is not None else settings.feedback_citation_max_pmids
    return found[: max(0, limit)]


def _is_gene_symbol(token: str) -> bool:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,15}", token):
        return False
    # Gene symbols are all-uppercase (letters/digits). Mixed case is only
    # accepted when the token contains a digit (C11orf95, Notch1); this keeps
    # ordinary words such as "Kras" or "Fusion" out of fusion detection.
    return bool(re.fullmatch(r"[A-Z][A-Z0-9]+", token) or any(c.isdigit() for c in token))


def _is_variant(token: str) -> bool:
    return bool(
        re.fullmatch(r"[A-Z]\d{2,4}[A-Z]", token)
        or re.fullmatch(r"\d+[A-Z]+", token)
        or token.startswith(_VARIANT_PREFIXES)
    )


def _excluded_symbol(token: str) -> bool:
    upper = token.upper()
    return upper in _ASSAY_NAMES or upper in _TUMOR_ABBREVIATIONS


def _valid_gene_token(token: str) -> bool:
    return _is_gene_symbol(token) and not _excluded_symbol(token) and not _is_variant(token)


def _valid_fusion_pair(left: str, right: str, separator: str) -> bool:
    if not (_valid_gene_token(left) and _valid_gene_token(right)):
        return False
    if separator == "-" and (
        left.upper() in _HYPHENATED_GENE_PREFIXES
        or right.upper() in _HYPHENATED_GENE_PREFIXES
    ):
        return False
    return True


_IMMUNE_MARKER_PREFIXES = ("CD", "CK")


def _is_immune_marker(token: str) -> bool:
    upper = token.upper()
    return any(
        upper.startswith(prefix)
        and len(upper) > len(prefix)
        and upper[len(prefix)].isdigit()
        for prefix in _IMMUNE_MARKER_PREFIXES
    )


def _positive_pair_notation(
    separator: str, left: str, right: str, after: str
) -> bool:
    """Return True for the hyphen-pair "-positive"/" positive" shorthand."""
    if separator != "-" or _is_immune_marker(left) or _is_immune_marker(right):
        return False
    return bool(_POSITIVE_AFTER_PAIR.match(after))


def _fusion_keyword_adjacent(
    message: str, start: int, end: int, separator: str, left: str, right: str
) -> bool:
    """Return True when fusion wording directly supports this pair.

    A negative qualifier or co-mutation/co-occurrence wording next to the pair
    always rejects it, even if a fusion word is also adjacent.
    """
    before = message[:start]
    after = message[end:]
    if _NEGATIVE_BEFORE_PAIR.search(before) or _NEGATIVE_AFTER_PAIR.match(after):
        return False
    if _COOCCURRENCE_AFTER_PAIR.match(after):
        return False
    fusion_after = (
        _FUSION_AFTER_PAIR_SLASH if separator == "/" else _FUSION_AFTER_PAIR
    )
    return bool(
        _FUSION_BEFORE_PAIR.search(before)
        or fusion_after.match(after)
        or _positive_pair_notation(separator, left, right, after)
    )


def _fusion_matches(message: str) -> List[Tuple[str, str, str]]:
    matches = []
    for match in _FUSION_SUBJECT.finditer(message or ""):
        left, separator, right = match.group(1), match.group(2), match.group(3)
        if not _valid_fusion_pair(left, right, separator):
            continue
        if separator in ("-", "--", "/") and not _fusion_keyword_adjacent(
            message, match.start(), match.end(), separator, left, right
        ):
            continue
        matches.append((left, separator, right))
    return matches


def extract_subject(message: str, gene: Optional[str] = None) -> Tuple[Optional[str], bool]:
    """Return (subject, is_fusion) following the documented precedence.

    The structured field (kept in its submitted case) wins over the message,
    then an explicit "::" anywhere in the message wins over a "-" or "/" pair,
    then "-"/"/" pairs only when a fusion word is directly adjacent to them.
    """
    if gene and gene.strip():
        subject = gene.strip()
        _, right = _split_subject(subject)
        return subject, right is not None
    matches = _fusion_matches(message)
    explicit = [item for item in matches if ":" in item[1]]
    for left, _, right in explicit or matches:
        return f"{left}::{right}", True
    match = _NAMED_GENE_SUBJECT.search(message or "")
    if match:
        token = match.group(1)
        if _valid_gene_token(token):
            return token, False
    return None, False


def _normalize_tumor_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_TUMOR_DASHES).lower()).strip()


def _tumor_negative_guard(normalized_text: str, normalized_term: str, start: int) -> bool:
    # "tumor resembling ovarian ..." must not count as an ovarian tumor, and
    # "non small cell lung cancer" must not satisfy "small cell lung cancer".
    clause = re.split(r"[.;:]", normalized_text[max(0, start - 80):start])[-1]
    if re.search(r"\bresembling\b", clause):
        return True
    if normalized_term.startswith("small cell ") and normalized_text[
        max(0, start - 4):start
    ] == "non ":
        return True
    return False


def _abbreviation_blocked(text: str, term: str, end: int) -> bool:
    if term == "GC":
        return bool(
            re.match(r"\s*-?\s*(?:content|rich)\b", text[end:end + 12], re.IGNORECASE)
        )
    return False


def _term_match(text: str, normalized: str, term: str) -> bool:
    if term.isupper():
        pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
        for match in re.finditer(pattern, text or ""):
            if not _abbreviation_blocked(text, term, match.end()):
                return True
        return False
    normalized_term = _normalize_tumor_text(term)
    pattern = rf"(?<![a-z0-9]){re.escape(normalized_term)}(?:s|es)?(?![a-z0-9])"
    for match in re.finditer(pattern, normalized):
        if not _tumor_negative_guard(normalized, normalized_term, match.start()):
            return True
    return False


def extract_tumor_type(message: str) -> Optional[str]:
    """Return the first recognizable tumor type mentioned in the feedback."""
    source = message or ""
    normalized = _normalize_tumor_text(source)
    for term in _TUMOR_TERMS:
        if _term_match(source, normalized, term):
            return term
    match = _GENERIC_TUMOR.search(source)
    if not match:
        return None
    words = match.group(1).split()
    site_index = next(
        (index for index, word in enumerate(words[:-1]) if word.casefold() in _ORGAN_SITE_WORDS),
        None,
    )
    if site_index is None:
        return None
    return " ".join(words[site_index:])


def _split_subject(subject: str) -> Tuple[str, Optional[str]]:
    match = re.match(r"^(.+?)\s*(::+|--|/|-)\s*(.+)$", subject.strip())
    if not match:
        return subject.strip(), None
    left, separator, right = match.group(1).strip(), match.group(2), match.group(3).strip()
    if not _valid_fusion_pair(left, right, separator):
        return subject.strip(), None
    return left, right


def _contains(text: str, term: str, *, case_sensitive: bool = False) -> bool:
    if not term:
        return False
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])"
    return bool(re.search(pattern, text or "", flags))


def _usable_aliases(aliases: Sequence[str]) -> List[str]:
    return [
        alias
        for alias in aliases
        if len(alias) >= 3 and alias.casefold() not in _COMMON_ENGLISH_WORDS
    ]


def _gene_case_sensitive(term: str) -> bool:
    # All-letter terms (any length) are case-sensitive. A digit, a hyphen, or
    # deliberate mixed case (C11orf95, c-Kit, Notch1) means the term is
    # specific enough to match case-insensitively.
    has_lower = any(character.islower() for character in term)
    has_upper = any(character.isupper() for character in term)
    return not (any(character.isdigit() for character in term) or "-" in term or (has_lower and has_upper))


def _gene_mentioned(text: str, gene: str, aliases: Sequence[str]) -> bool:
    terms = list(dict.fromkeys([gene, *_usable_aliases(aliases)]))
    return any(_contains(text, term, case_sensitive=_gene_case_sensitive(term)) for term in terms)


def _tumor_aliases(tumor: str) -> Tuple[str, ...]:
    group = _TUMOR_GROUPS.get(tumor.casefold(), (tumor,))
    # Two-letter abbreviations (GC, UC, OV) are only for detecting the tumor in
    # the feedback text; they are too ambiguous to search for in abstracts.
    # Also drop lowercase 3-letter keys, which are English words.
    terms = tuple(
        term
        for term in group
        if len(term) > 2 and not (len(term) == 3 and not term.isupper())
    )
    if terms:
        return terms
    return (tumor,) if len(tumor) > 2 else ()


def _tumor_match(text: str, tumor: Optional[str]) -> Optional[str]:
    """Return the alias term found in text, or None. No organ-equivalence rules."""
    if not tumor:
        return None
    normalized = _normalize_tumor_text(text)
    for term in _tumor_aliases(tumor):
        if _term_match(text, normalized, term):
            return term
    return None


def _tumor_detail(text: str, tumor: str) -> str:
    matched = _tumor_match(text, tumor)
    if matched:
        return f"{tumor}: found: {matched}"
    searched = ", ".join(_tumor_aliases(tumor)) or "no unambiguous aliases"
    return f"{tumor}: not found (searched: {searched})"


def _yes(flag: bool) -> str:
    return "yes" if flag else "NO"


def _safe_code(value: str) -> str:
    """Make untrusted text safe inside a Markdown code span."""
    cleaned = re.sub(r"[`\r\n\t]", "", str(value)).strip()
    return cleaned[:64] or "?"


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


def _retraction_kind(record) -> Optional[str]:
    types = {value.strip().lower() for value in record.publication_types}
    refs = {
        value.strip().lower().replace(" ", "")
        for value in record.pubmed_comment_ref_types
    }
    if "retracted publication" in types or "retractionin" in refs:
        return "retracted publication"
    if "retraction of publication" in types or "retractionof" in refs:
        return "retraction notice"
    title = (record.title or "").strip().lower()
    if re.match(r"^\[?retraction\b", title):
        return "retraction notice"
    if re.match(r"^\[?retracted\b", title):
        return "retracted publication"
    return None


def _record_line(
    pmid: str,
    record,
    five: str,
    three: Optional[str],
    aliases_five: Sequence[str],
    aliases_three: Sequence[str],
    tumor: Optional[str],
) -> str:
    retraction_kind = _retraction_kind(record)
    if retraction_kind:
        return (
            f"- PMID {pmid}: RETRACTED — {retraction_kind} "
            "(citation is retracted; mention check skipped)"
        )
    text = f"{record.title} {record.abstract}"
    five_hit = _gene_mentioned(text, five, aliases_five)
    if three:
        three_hit = _gene_mentioned(text, three, aliases_three)
        detail = (
            f"mentions `{_safe_code(five)}` — {_yes(five_hit)}; "
            f"`{_safe_code(three)}` — {_yes(three_hit)}"
        )
    else:
        detail = f"mentions `{_safe_code(five)}` — {_yes(five_hit)}"
    if tumor:
        detail += f"; {_tumor_detail(text, tumor)}"
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
    (aliases_five, aliases_three), records = await asyncio.gather(
        _resolve_aliases(five, three),
        literature._efetch(
            list(pmids), literature._get_ncbi_client(), filter_retracted=False
        ),
    )
    by_pmid = {record.pmid: record for record in records}
    lines: List[str] = []
    for pmid in pmids:
        record = by_pmid.get(pmid)
        if record is None:
            lines.append(f"- PMID {pmid}: check unavailable (abstract not retrieved)")
            continue
        lines.append(_record_line(pmid, record, five, three, aliases_five, aliases_three, tumor))

    intro: List[str] = [
        "Automated, informational only; title/abstract were checked, not full text. "
        "This reports mentions, not diagnoses; a differential diagnosis still counts as a mention."
    ]
    if not tumor:
        intro.append("Tumor type not detected in feedback; tumor mention was not checked.")
    return "\n".join([_SECTION_HEADING, "", *intro, "", *lines])


def _unparseable_lines(values: Sequence[Tuple[str, str]]) -> List[str]:
    return [
        f"- Structured PMID value `{_safe_code(value)}` not checked: {reason}."
        for value, reason in values
    ]


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
    all_pmids, unparseable = _collect_cited_pmids(message, structured_pmids)
    if not all_pmids:
        if not unparseable:
            return None
        return "\n".join(
            [
                _SECTION_HEADING,
                "",
                "No parseable PMIDs were cited; citation check skipped.",
                *_unparseable_lines(unparseable),
            ]
        )
    cap = max(0, settings.feedback_citation_max_pmids)
    pmids = all_pmids[:cap]
    dropped = len(all_pmids) - len(pmids)
    try:
        section = await asyncio.wait_for(
            _build_section(pmids, message, gene),
            timeout=settings.feedback_citation_check_timeout_seconds,
        )
        if not section:
            return _unavailable("no subject gene or fusion detected")
        extra: List[str] = []
        if dropped:
            extra.append(
                f"- {dropped} additional cited PMID(s) were not checked (cap {cap})."
            )
        if unparseable:
            extra.extend(_unparseable_lines(unparseable))
        if extra:
            section = "\n".join([section, *extra])
        return section
    except asyncio.TimeoutError:
        logger.warning("Feedback citation check timed out for %d PMID(s)", len(pmids))
        return _unavailable("PubMed request timed out")
    except Exception:  # noqa: BLE001 - feedback submission must never fail here
        logger.exception("Feedback citation check failed")
        return _unavailable("PubMed request failed")
