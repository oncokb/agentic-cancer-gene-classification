"""Deterministic safety guards for stored and cached annotation payloads."""

from __future__ import annotations

import logging
from typing import Iterable, List, Set

from src.config import settings
from src.models.schema import (
    AliasMatch,
    AnnotationResult,
    FusionEvidenceResult,
    GeneAnnotation,
    LiteratureRecord,
    QualityFlag,
)
from src.pipeline.literature import (
    find_retracted_pmids,
    fusion_evidence_alias_match,
    resolve_fusion_partner_aliases,
)
from src.pipeline.normalization import split_fusion

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
        abstract=card.abstract or card.quote or "",
        journal=card.journal,
        publication_types=[],
    )


async def sanitize_fusion_evidence_result(
    result: FusionEvidenceResult,
    retracted_pmids: Set[str],
) -> bool:
    original_pmids = list(result.pmids)
    original_card_count = len(result.evidence_cards)

    # Re-resolve HGNC aliases fresh rather than trusting whatever alias_matches
    # a card claims. A card's stored alias_matches is not verified ground
    # truth — it could be malformed or wrong (bad data, a bug elsewhere, a
    # future code path) — and trusting it would let a forged/incorrect entry
    # (e.g. {"gene": "KAT6A", "alias": "BCR"}) make a card describing a
    # completely unrelated fusion (e.g. BCR-ABL1) survive this safety net
    # undetected. Re-verification always uses the genuinely-resolved alias
    # set for result.fusion, independent of anything the card says.
    five_prime, three_prime = split_fusion(result.fusion)
    five_aliases: List[str] = []
    three_aliases: List[str] = []
    non_retracted_cards = [card for card in result.evidence_cards if card.pmid not in retracted_pmids]
    if non_retracted_cards and five_prime and three_prime:
        five_aliases, three_aliases = await resolve_fusion_partner_aliases(five_prime, three_prime)

    # Verify each card against its OWN title/abstract text individually
    # (fusion_evidence_alias_match, not the PMID-keyed batch helper) — the
    # schema does not guarantee PMIDs are unique across cards, and a
    # PMID-keyed lookup would let one card's match result leak onto a
    # different card that happens to share its PMID but has unrelated text.
    kept_cards = []
    label_changed = False
    for card in non_retracted_cards:
        match = fusion_evidence_alias_match(
            _fusion_card_as_record(card), result.fusion, five_aliases, three_aliases
        )
        if match is None:
            continue
        # Recompute matched_via_alias/alias_matches from this fresh
        # re-verification rather than leaving whatever the card previously
        # stored — covers both a stale matched_via_alias=False on a genuine
        # alias-only match (so the UI badge is never silently missing) and a
        # forged/incorrect claim (so the UI badge is never wrong).
        new_matched_via_alias = bool(match)
        new_alias_matches = [AliasMatch(gene=gene, alias=alias) for gene, alias in match]
        if card.matched_via_alias != new_matched_via_alias or card.alias_matches != new_alias_matches:
            label_changed = True
        card.matched_via_alias = new_matched_via_alias
        card.alias_matches = new_alias_matches
        kept_cards.append(card)

    kept_pmids = {card.pmid for card in kept_cards}
    result.evidence_cards = kept_cards
    result.pmids = [
        pmid
        for pmid in result.pmids
        if pmid in kept_pmids and pmid not in retracted_pmids
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
    return (
        result.pmids != original_pmids
        or len(result.evidence_cards) != original_card_count
        or label_changed
    )


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
        changed = await sanitize_fusion_evidence_result(fusion_result, retracted_pmids) or changed

    if changed:
        logger.info("Sanitized stored annotation result %s", result.run_id)
    return result, changed
