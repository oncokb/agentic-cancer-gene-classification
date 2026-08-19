"""Conservative clinical-actionability confidence scoring.

The scorer is intentionally deterministic and citation-bound. It only evaluates
PMIDs that already passed synthesis citation verification, and it returns a
curator-visible result only when high-confidence therapeutic precedent is present.
"""

from __future__ import annotations

import re
from typing import Iterable, Optional

from src.models.schema import (
    ClinicalActionability,
    ClinicalActionabilityEvidence,
    ClinicalActionabilityScoreComponent,
    LiteratureRecord,
)

HIGH_CONFIDENCE_THRESHOLD = 0.85

_THERAPY_TERMS = (
    "inhibitor",
    "inhibitors",
    "therapy",
    "therapies",
    "therapeutic",
    "treatment",
    "treated",
    "sensitivity",
    "sensitive",
    "resistance",
    "resistant",
    "response",
    "responded",
    "targeted",
)
_CLINICAL_TERMS = (
    "patient",
    "patients",
    "cohort",
    "clinical trial",
    "phase i",
    "phase ii",
    "phase iii",
    "objective response",
    "progression-free",
    "overall survival",
    "survival",
)
_PRECLINICAL_TERMS = (
    "cell line",
    "cell lines",
    "xenograft",
    "mouse",
    "murine",
    "in vitro",
    "in vivo",
    "knockdown",
    "knockout",
)
_REVIEW_TERMS = ("review", "systematic review", "meta-analysis")
_DOMAIN_TERMS = (
    "domain",
    "kinase domain",
    "activation loop",
    "ligand-binding domain",
    "dna-binding domain",
    "retained",
    "retains",
    "disrupted",
)
_DOMAIN_STATUS_TERMS = ("retained", "retains", "disrupted")
_ALTERATION_TERMS = (
    "fusion",
    "rearrangement",
    "translocation",
    "mutation",
    "mutant",
    "amplification",
    "deletion",
    "variant",
)
_CONFLICT_TERMS = (
    "conflicting",
    "contradictory",
    "inconsistent",
    "mixed evidence",
    "did not respond",
    "no response",
    "lack of response",
)

# Common oncology drug suffixes plus all-caps trial drug codes. This is not
# intended as a drug database; it only helps produce a readable summary after
# direct therapeutic language has already been detected.
_DRUG_PATTERN = re.compile(
    r"\b([A-Z]{2,}[- ]?\d{2,}|[A-Za-z]+(?:nib|tinib|ciclib|parib|lisib|rafenib|metinib|mab|zumab|ximab))\b"
)


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _record_text(record: LiteratureRecord) -> str:
    return f"{record.title} {record.abstract}"


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(term in text for term in terms)


def _publication_type_text(record: LiteratureRecord) -> str:
    return " ".join(record.publication_types).lower()


def _evidence_type(record: LiteratureRecord) -> str:
    pubtypes = _publication_type_text(record)
    text = _norm(_record_text(record))
    if "clinical trial" in pubtypes or _contains_any(text, ("clinical trial", "phase i", "phase ii", "phase iii")):
        return "clinical"
    if "case reports" in pubtypes or "case report" in text:
        return "case_report"
    if any(term in pubtypes for term in _REVIEW_TERMS):
        return "review"
    if _contains_any(text, _CLINICAL_TERMS):
        return "clinical"
    if _contains_any(text, _PRECLINICAL_TERMS):
        return "preclinical"
    if _contains_any(text, _REVIEW_TERMS):
        return "review"
    return "other"


def _extract_therapies(text: str) -> list[str]:
    therapies = []
    seen = set()
    for match in _DRUG_PATTERN.finditer(text):
        value = match.group(1).strip(" ,.;:()[]")
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        therapies.append(value)
    return therapies[:5]


def _extract_domains(text: str) -> list[str]:
    normalized = _norm(text)
    domains = []
    for match in re.finditer(r"\b([a-z0-9-]+(?: [a-z0-9-]+){0,2} domain)\b", normalized):
        value = re.sub(r"^(the|a|an) ", "", match.group(1))
        value = re.sub(r"^(retained|retains|disrupted) (the )?", "", value)
        if value not in domains:
            domains.append(value)
    for term in ("activation loop", "ligand-binding domain", "dna-binding domain", "kinase domain"):
        if term in normalized and term not in domains:
            domains.append(term)
    return domains[:5]


def _fallback_quote(record: LiteratureRecord, gene: str) -> Optional[str]:
    sentences = re.split(r"(?<=[.!?])\s+", record.abstract.strip())
    gene_upper = gene.upper()
    for sentence in sentences:
        lower = sentence.lower()
        if gene_upper in sentence.upper() and (
            _contains_any(lower, _THERAPY_TERMS) or _contains_any(lower, _DOMAIN_TERMS)
        ):
            return sentence[:360]
    return sentences[0][:360] if sentences and sentences[0] else None


def _direct_therapeutic_signal(record: LiteratureRecord, gene: str) -> tuple[bool, list[str], list[str], list[str]]:
    raw_text = _record_text(record)
    text = _norm(raw_text)
    gene_seen = gene.upper() in raw_text.upper()
    therapy_seen = _contains_any(text, _THERAPY_TERMS)
    domains = _extract_domains(raw_text)
    domain_seen = bool(domains) or _contains_any(text, _DOMAIN_STATUS_TERMS)
    alteration_seen = _contains_any(text, _ALTERATION_TERMS)
    therapies = _extract_therapies(raw_text)
    matched_terms = []
    if therapy_seen:
        matched_terms.append("therapeutic_language")
    if domain_seen:
        matched_terms.append("domain_language")
    if alteration_seen:
        matched_terms.append("alteration_language")
    if therapies:
        matched_terms.append("named_drug")
    return gene_seen and therapy_seen and (domain_seen or alteration_seen or bool(therapies)), therapies, domains, matched_terms


def _same_tumor_context(records: list[LiteratureRecord], tumor_type: Optional[str]) -> bool:
    if not tumor_type:
        return False
    tumor = _norm(tumor_type)
    if not tumor:
        return False
    return any(tumor in _norm(_record_text(record)) for record in records)


def _has_conflict(records: list[LiteratureRecord]) -> bool:
    return any(_contains_any(_norm(_record_text(record)), _CONFLICT_TERMS) for record in records)


def _component(
    code: str,
    label: str,
    delta: float,
    records: list[LiteratureRecord],
    detail: str,
) -> ClinicalActionabilityScoreComponent:
    return ClinicalActionabilityScoreComponent(
        code=code,
        label=label,
        delta=delta,
        pmids=[record.pmid for record in records],
        detail=detail,
    )


def assess_clinical_actionability(
    *,
    gene: str,
    citations: list[str],
    records: list[LiteratureRecord],
    tumor_type: Optional[str] = None,
    in_oncokb: Optional[bool] = None,
    insufficient_evidence: bool = False,
) -> Optional[ClinicalActionability]:
    """Return high-confidence actionability only, otherwise ``None``.

    Confidence score rubric:
    - +0.35 direct human clinical evidence signal
    - +0.25 drug/domain/actionability language tied to the queried gene
    - +0.15 same tumor-type context
    - +0.15 two or more independent supporting PMIDs
    - +0.05 high-impact journal signal
    - +0.20 OncoKB corroboration
    - -0.25 preclinical-only support
    - -0.15 case-report-only support
    - -0.30 conflicting/negative-response language

    The field is surfaced only when the final score is at least 0.85.
    """
    if insufficient_evidence or not citations:
        return None

    cited = [record for record in records if record.pmid in set(citations)]
    if not cited:
        return None

    actionable_records = []
    evidence_entries = []
    therapies_seen: list[str] = []
    domains_seen: list[str] = []
    for record in cited:
        has_signal, therapies, domains, matched_terms = _direct_therapeutic_signal(record, gene)
        if not has_signal:
            continue
        evidence_type = _evidence_type(record)
        actionable_records.append(record)
        for therapy in therapies:
            if therapy.lower() not in {value.lower() for value in therapies_seen}:
                therapies_seen.append(therapy)
        for domain in domains:
            if domain not in domains_seen:
                domains_seen.append(domain)
        evidence_entries.append(
            ClinicalActionabilityEvidence(
                pmid=record.pmid,
                title=record.title,
                evidence_type=evidence_type,  # type: ignore[arg-type]
                therapies=therapies,
                domains=domains,
                matched_terms=matched_terms,
                quote=_fallback_quote(record, gene),
            )
        )

    if not actionable_records:
        return None

    evidence_types = {_evidence_type(record) for record in actionable_records}
    components: list[ClinicalActionabilityScoreComponent] = []

    if "clinical" in evidence_types:
        clinical_records = [record for record in actionable_records if _evidence_type(record) == "clinical"]
        components.append(
            _component(
                "clinical_evidence",
                "Direct human clinical evidence",
                0.35,
                clinical_records,
                "At least one verified cited abstract has patient/cohort/trial language.",
            )
        )
    components.append(
        _component(
            "direct_actionability_language",
            "Drug/domain/actionability language tied to gene",
            0.25,
            actionable_records,
            (
                "Matched therapies: "
                + (", ".join(therapies_seen) if therapies_seen else "none detected")
                + "; matched domains: "
                + (", ".join(domains_seen) if domains_seen else "none detected")
                + "."
            ),
        )
    )

    same_tumor = _same_tumor_context(actionable_records, tumor_type)
    if same_tumor:
        components.append(
            _component(
                "same_tumor_type",
                "Same tumor-type context",
                0.15,
                actionable_records,
                f"Supporting abstract text contains the query tumor type: {tumor_type}.",
            )
        )
    if len({record.pmid for record in actionable_records}) >= 2:
        components.append(
            _component(
                "multiple_pmids",
                "Two or more independent supporting PMIDs",
                0.15,
                actionable_records,
                "Therapeutic precedent is supported by multiple verified cited abstracts.",
            )
        )
    if any(record.journal in {"N Engl J Med", "Lancet", "Nature", "Cell", "J Clin Oncol", "Cancer Cell"} for record in actionable_records):
        high_impact_records = [
            record
            for record in actionable_records
            if record.journal in {"N Engl J Med", "Lancet", "Nature", "Cell", "J Clin Oncol", "Cancer Cell"}
        ]
        components.append(
            _component(
                "high_impact_journal",
                "High-impact journal signal",
                0.05,
                high_impact_records,
                "At least one supporting PMID is from a configured high-impact journal.",
            )
        )
    if in_oncokb:
        components.append(
            ClinicalActionabilityScoreComponent(
                code="oncokb_corroboration",
                label="OncoKB membership corroboration",
                delta=0.20,
                pmids=[],
                detail="The queried gene is present in OncoKB; this corroborates gene context, not therapy selection.",
            )
        )

    if evidence_types == {"preclinical"}:
        components.append(
            _component(
                "preclinical_only_penalty",
                "Preclinical-only support penalty",
                -0.25,
                actionable_records,
                "All direct supporting PMIDs are preclinical.",
            )
        )
    if evidence_types == {"case_report"}:
        components.append(
            _component(
                "case_report_only_penalty",
                "Case-report-only support penalty",
                -0.15,
                actionable_records,
                "All direct supporting PMIDs are case reports.",
            )
        )
    if _has_conflict(actionable_records):
        components.append(
            _component(
                "conflict_penalty",
                "Conflicting or negative-response language penalty",
                -0.30,
                actionable_records,
                "Supporting abstracts include conflicting or negative-response language.",
            )
        )

    score = sum(component.delta for component in components)
    score = round(min(1.0, max(0.0, score)), 2)
    if score < HIGH_CONFIDENCE_THRESHOLD:
        return None

    therapies = ", ".join(therapies_seen) if therapies_seen else "targeted therapy or inhibitor precedent"
    tumor = f" in {tumor_type}" if same_tumor and tumor_type else ""
    pmids = [record.pmid for record in actionable_records]
    return ClinicalActionability(
        confidence_score=score,
        summary=(
            f"High-confidence literature-derived therapeutic precedent for {gene}{tumor}: "
            f"{therapies}. This is not a treatment recommendation."
        ),
        confidence_explanation=(
            f"Clinical actionability confidence {score:.2f}: "
            + "; ".join(component.label for component in components)
            + ". Score is deterministic, citation-bound, and only shown at >= "
            f"{HIGH_CONFIDENCE_THRESHOLD:.2f}."
        ),
        pmids=pmids,
        score_components=components,
        evidence=evidence_entries,
    )
