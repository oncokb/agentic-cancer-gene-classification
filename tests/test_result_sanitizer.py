from __future__ import annotations

from src.models.schema import (
    AliasMatch,
    AnnotationResult,
    EvidenceCard,
    FusionEvidenceCard,
    FusionEvidenceResult,
    GeneAnnotation,
    SupportingQuote,
)
from src.pipeline import result_sanitizer
from src.pipeline.result_sanitizer import sanitize_annotation_result


async def test_sanitize_annotation_result_removes_retracted_and_bad_fusion_evidence(monkeypatch):
    async def fake_find_retracted_pmids(_pmids):
        return {"12345"}

    async def fake_resolve_fusion_partner_aliases(five_prime, three_prime):
        return [], []

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", fake_find_retracted_pmids)
    monkeypatch.setattr(
        result_sanitizer, "resolve_fusion_partner_aliases", fake_resolve_fusion_partner_aliases
    )

    result = AnnotationResult(
        run_id="run-1",
        timestamp="2026-08-31T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=1,
        annotations=[
            GeneAnnotation(
                gene="GENE",
                citations=["12345", "67890"],
                retrieved_pmids=["12345", "67890"],
                supporting_quotes=[
                    SupportingQuote(pmid="12345", quote="Retracted support."),
                    SupportingQuote(pmid="67890", quote="Valid support."),
                ],
                evidence_cards=[
                    EvidenceCard(pmid="12345", title="Retracted paper"),
                    EvidenceCard(pmid="67890", title="Valid paper"),
                ],
            )
        ],
        fusion_evidence=[
            FusionEvidenceResult(
                fusion="PLAGL1::MYB",
                retrieved_count=2,
                pmids=["21901247", "222"],
                evidence_cards=[
                    FusionEvidenceCard(
                        fusion="PLAGL1::MYB",
                        pmid="21901247",
                        title="Studies of genomic imbalances and the MYB-NFIB gene fusion",
                        quote="PLAGL1 methylation was also evaluated.",
                    ),
                    FusionEvidenceCard(
                        fusion="PLAGL1::MYB",
                        pmid="222",
                        title="Recurrent PLAGL1-MYB fusion in leukemia",
                        quote="The PLAGL1-MYB fusion was detected by RNA sequencing.",
                    ),
                ],
            )
        ],
    )

    sanitized, changed = await sanitize_annotation_result(result)

    assert changed is True
    assert sanitized.annotations[0].citations == ["67890"]
    assert sanitized.annotations[0].retrieved_pmids == ["67890"]
    assert [quote.pmid for quote in sanitized.annotations[0].supporting_quotes] == ["67890"]
    assert [card.pmid for card in sanitized.annotations[0].evidence_cards] == ["67890"]
    assert sanitized.annotations[0].quality_flags[0].code == "retracted_citations_removed"
    assert sanitized.fusion_evidence[0].pmids == ["222"]
    assert [card.pmid for card in sanitized.fusion_evidence[0].evidence_cards] == ["222"]


async def test_sanitize_fusion_evidence_keeps_alias_matched_card(monkeypatch):
    """An evidence card found only via an HGNC alias (e.g. 'MOZ-CBP' for
    KAT6A::CREBBP) must survive re-verification. The sanitizer re-resolves
    HGNC aliases fresh (never trusting the card's own alias_matches as ground
    truth) and re-checks the card's text against that genuinely-resolved
    alias set."""

    async def fake_find_retracted_pmids(_pmids):
        return set()

    async def fake_resolve_fusion_partner_aliases(five_prime, three_prime):
        assert (five_prime, three_prime) == ("KAT6A", "CREBBP")
        return ["MOZ", "MYST3", "ZNF220"], ["CBP", "RSTS"]

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", fake_find_retracted_pmids)
    monkeypatch.setattr(
        result_sanitizer, "resolve_fusion_partner_aliases", fake_resolve_fusion_partner_aliases
    )

    result = AnnotationResult(
        run_id="run-2",
        timestamp="2026-08-31T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=0,
        annotations=[],
        fusion_evidence=[
            FusionEvidenceResult(
                fusion="KAT6A::CREBBP",
                retrieved_count=1,
                pmids=["555"],
                evidence_cards=[
                    FusionEvidenceCard(
                        fusion="KAT6A::CREBBP",
                        pmid="555",
                        title="A recurrent MOZ-CBP fusion identified in pediatric AML",
                        abstract=(
                            "We report a novel MOZ-CBP fusion transcript detected by "
                            "RT-PCR in a pediatric AML patient."
                        ),
                        matched_via_alias=True,
                        alias_matches=[
                            AliasMatch(gene="KAT6A", alias="MOZ"),
                            AliasMatch(gene="CREBBP", alias="CBP"),
                        ],
                    ),
                ],
            )
        ],
    )

    sanitized, changed = await sanitize_annotation_result(result)

    assert changed is False
    assert sanitized.fusion_evidence[0].pmids == ["555"]
    assert [card.pmid for card in sanitized.fusion_evidence[0].evidence_cards] == ["555"]


async def test_sanitize_fusion_evidence_ignores_forged_alias_matches(monkeypatch):
    """A card's stored alias_matches is NOT trusted as ground truth. If it
    falsely claims a match via symbols that aren't genuine HGNC aliases for
    the stated fusion (e.g. bad data, or a bug elsewhere), the card must
    still be dropped by re-verification, since re-verification always
    re-resolves the real alias set rather than trusting the card's claim."""

    async def fake_find_retracted_pmids(_pmids):
        return set()

    async def fake_resolve_fusion_partner_aliases(five_prime, three_prime):
        # Genuine HGNC aliases for KAT6A / CREBBP — BCR and ABL1 are not
        # among them, unlike what the forged card below claims.
        assert (five_prime, three_prime) == ("KAT6A", "CREBBP")
        return ["MOZ", "MYST3", "ZNF220"], ["CBP", "RSTS"]

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", fake_find_retracted_pmids)
    monkeypatch.setattr(
        result_sanitizer, "resolve_fusion_partner_aliases", fake_resolve_fusion_partner_aliases
    )

    result = AnnotationResult(
        run_id="run-3",
        timestamp="2026-08-31T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=0,
        annotations=[],
        fusion_evidence=[
            FusionEvidenceResult(
                fusion="KAT6A::CREBBP",
                retrieved_count=1,
                pmids=["999"],
                evidence_cards=[
                    FusionEvidenceCard(
                        fusion="KAT6A::CREBBP",
                        pmid="999",
                        title="Recurrent BCR-ABL1 fusion in chronic myeloid leukemia",
                        abstract=(
                            "BCR-ABL1 fusion transcripts were detected by RT-PCR in all cases."
                        ),
                        # Forged: BCR/ABL1 are not real HGNC aliases of
                        # KAT6A/CREBBP. A sanitizer that trusted this field
                        # would wrongly keep a card about an unrelated fusion.
                        matched_via_alias=True,
                        alias_matches=[
                            AliasMatch(gene="KAT6A", alias="BCR"),
                            AliasMatch(gene="CREBBP", alias="ABL1"),
                        ],
                    ),
                ],
            )
        ],
    )

    sanitized, changed = await sanitize_annotation_result(result)

    assert changed is True
    assert sanitized.fusion_evidence[0].pmids == []
    assert sanitized.fusion_evidence[0].evidence_cards == []


async def test_sanitize_fusion_evidence_recomputes_stale_alias_labels(monkeypatch):
    """Re-verification recomputes matched_via_alias/alias_matches on every
    surviving card from its own fresh HGNC-backed match detail, rather than
    only deciding keep/drop — covering both a genuine alias-only match
    stored with a stale matched_via_alias=False (so the UI badge is never
    silently missing) and a literal match stored with a stale/bogus
    matched_via_alias=True claim (so the UI badge is never wrong)."""

    async def fake_find_retracted_pmids(_pmids):
        return set()

    async def fake_resolve_fusion_partner_aliases(five_prime, three_prime):
        return ["MOZ", "MYST3", "ZNF220"], ["CBP", "RSTS"]

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", fake_find_retracted_pmids)
    monkeypatch.setattr(
        result_sanitizer, "resolve_fusion_partner_aliases", fake_resolve_fusion_partner_aliases
    )

    result = AnnotationResult(
        run_id="run-4",
        timestamp="2026-08-31T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=0,
        annotations=[],
        fusion_evidence=[
            FusionEvidenceResult(
                fusion="KAT6A::CREBBP",
                retrieved_count=2,
                pmids=["111", "222"],
                evidence_cards=[
                    # Genuinely matched only via aliases, but stored with a
                    # stale matched_via_alias=False and no alias_matches.
                    FusionEvidenceCard(
                        fusion="KAT6A::CREBBP",
                        pmid="111",
                        title="A recurrent MOZ-CBP fusion identified in pediatric AML",
                        abstract="A novel MOZ-CBP fusion transcript was detected by RT-PCR.",
                        matched_via_alias=False,
                        alias_matches=[],
                    ),
                    # Genuinely a literal match, but stored with a stale,
                    # bogus matched_via_alias=True claim.
                    FusionEvidenceCard(
                        fusion="KAT6A::CREBBP",
                        pmid="222",
                        title="KAT6A-CREBBP fusion in AML",
                        abstract="A KAT6A-CREBBP fusion transcript was detected by RT-PCR.",
                        matched_via_alias=True,
                        alias_matches=[AliasMatch(gene="KAT6A", alias="BOGUS")],
                    ),
                ],
            )
        ],
    )

    sanitized, _changed = await sanitize_annotation_result(result)

    cards_by_pmid = {card.pmid: card for card in sanitized.fusion_evidence[0].evidence_cards}
    assert cards_by_pmid["111"].matched_via_alias is True
    assert {m.alias for m in cards_by_pmid["111"].alias_matches} == {"MOZ", "CBP"}
    assert cards_by_pmid["222"].matched_via_alias is False
    assert cards_by_pmid["222"].alias_matches == []
