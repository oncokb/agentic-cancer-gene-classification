"""Deterministic safety guards for stored and cached annotation payloads."""

from __future__ import annotations

import logging
from typing import Iterable, List, Set

from src.config import settings
from src.models.schema import (
    AnnotationResult,
    FusionEvidenceResult,
    GeneAnnotation,
    LiteratureRecord,
    QualityFlag,
)
from src.pipeline.literature import find_retracted_pmids, record_discusses_exact_fusion

logger = logging.getLogger(__name__)


def _dedupe(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def annotation_reference_pmids(annotation: GeneAnnotation) -> List[str]:
    pmids: List[str] = []
    pmids.extend(annotation.citations)
    pmids.extend(annotation.retrieved_pmids)
    pmids.extend(card.pmid for card in annotation.evidence_cards)
    pmids.extend(quote.pmid for quote in annotation.supporting_quotes)
    if annotation.clinical_actionability:
        pmids.extend(annotation.clinical_actionability.pmids)
        for component in annotation.clinical_actionability.score_components:
            pmids.extend(component.pmids)
        pmids.extend(evidence.pmid for evidence in annotation.clinical_actionability.evidence)
    return _dedupe(pmids)


async def find_retracted_annotation_pmids(annotation: GeneAnnotation) -> Set[str]:
    return await find_retracted_pmids(annotation_reference_pmids(annotation))


def strip_retracted_pmids_from_annotation(
    annotation: GeneAnnotation,
    retracted_pmids: Set[str],
) -> bool:
    if not retracted_pmids:
        return False

    original_citations = list(annotation.citations)
    annotation.citations = [pmid for pmid in annotation.citations if pmid not in retracted_pmids]
    annotation.retrieved_pmids = [
        pmid for pmid in annotation.retrieved_pmids if pmid not in retracted_pmids
    ]
    annotation.supporting_quotes = [
        quote for quote in annotation.supporting_quotes if quote.pmid not in retracted_pmids
    ]
    annotation.evidence_cards = [
        card for card in annotation.evidence_cards if card.pmid not in retracted_pmids
    ]
    annotation.retrieval_ranking = [
        score for score in annotation.retrieval_ranking if score.pmid not in retracted_pmids
    ]

    if annotation.clinical_actionability:
        actionability = annotation.clinical_actionability
        actionability.pmids = [pmid for pmid in actionability.pmids if pmid not in retracted_pmids]
        actionability.score_components = [
            component
            for component in actionability.score_components
            if not set(component.pmids).intersection(retracted_pmids)
        ]
        actionability.evidence = [
            evidence for evidence in actionability.evidence if evidence.pmid not in retracted_pmids
        ]

    dropped = sorted(set(original_citations) - set(annotation.citations))
    if dropped and not any(flag.code == "retracted_citations_removed" for flag in annotation.quality_flags):
        annotation.quality_flags.append(
            QualityFlag(
                code="retracted_citations_removed",
                label="Retracted citations removed",
                severity="critical",
                detail=f"Removed retracted PMID(s): {', '.join(dropped)}.",
            )
        )
    if original_citations and not annotation.citations:
        annotation.insufficient_evidence = True
        if not any(flag.code == "no_verified_citations" for flag in annotation.quality_flags):
            annotation.quality_flags.append(
                QualityFlag(
                    code="no_verified_citations",
                    label="No verified citations",
                    severity="critical",
                    detail="All previously cited PMIDs were removed by the retraction guard.",
                )
            )
    return True


def _fusion_card_as_record(card) -> LiteratureRecord:
    return LiteratureRecord(
        pmid=card.pmid,
        title=card.title,
        abstract=card.quote or "",
        journal=card.journal,
        publication_types=[],
    )


def sanitize_fusion_evidence_result(
    result: FusionEvidenceResult,
    retracted_pmids: Set[str],
) -> bool:
    original_pmids = list(result.pmids)
    cards_by_pmid = {card.pmid: card for card in result.evidence_cards}
    kept_cards = []
    for card in result.evidence_cards:
        if card.pmid in retracted_pmids:
            continue
        if not record_discusses_exact_fusion(_fusion_card_as_record(card), result.fusion):
            continue
        kept_cards.append(card)

    kept_pmids = {card.pmid for card in kept_cards}
    result.evidence_cards = kept_cards
    result.pmids = [
        pmid
        for pmid in result.pmids
        if pmid in kept_pmids
        and pmid not in retracted_pmids
        and pmid in cards_by_pmid
    ]
    result.retrieved_count = len(result.pmids)
    result.well_supported = result.retrieved_count >= settings.min_papers_for_strong_association
    if result.retrieved_count == 0 and original_pmids:
        result.well_supported = False
        result.interpretation = (
            f"No non-retracted PubMed records were found that explicitly discuss "
            f"the exact {result.fusion} fusion pair."
        )
    elif result.pmids != original_pmids:
        result.interpretation = (
            f"{result.fusion} has {result.retrieved_count} non-retracted PubMed record(s) "
            "that explicitly discuss the exact fusion pair."
        )
    return result.pmids != original_pmids or len(result.evidence_cards) != len(cards_by_pmid)


async def sanitize_annotation_result(result: AnnotationResult) -> tuple[AnnotationResult, bool]:
    pmids = _dedupe(
        pmid
        for annotation in result.annotations
        for pmid in annotation_reference_pmids(annotation)
    )
    pmids.extend(
        pmid
        for fusion_result in result.fusion_evidence
        for pmid in fusion_result.pmids
    )
    retracted_pmids = await find_retracted_pmids(_dedupe(pmids))

    changed = False
    for annotation in result.annotations:
        changed = strip_retracted_pmids_from_annotation(annotation, retracted_pmids) or changed
    for fusion_result in result.fusion_evidence:
        changed = sanitize_fusion_evidence_result(fusion_result, retracted_pmids) or changed

    if changed:
        logger.info("Sanitized stored annotation result %s", result.run_id)
    return result, changed
