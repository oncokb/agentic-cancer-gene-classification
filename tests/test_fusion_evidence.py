from __future__ import annotations

import httpx

from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import literature
from src.pipeline.literature import (
    _filter_exact_fusion_records,
    _fusion_evidence_queries,
    _fusion_query_variants,
    record_discusses_exact_fusion,
    retrieve_fusion_evidence,
)

# HGNC aliases actually returned for these genes today — used to exercise the
# alias-expansion path without hitting the network. KAT6A's legacy names
# (MOZ, MYST3, ZNF220) are the motivating real-world false negative this
# feature fixes: older fusion literature calls KAT6A::CREBBP "MOZ-CBP".
_KAT6A_ALIASES = ["MOZ", "MYST3", "ZNF220"]
_CREBBP_ALIASES = ["CBP", "RSTS"]


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


def _fake_resolve_gene(aliases_by_symbol):
    async def _resolve(symbol, client):
        return ResolvedGene(
            input_symbol=symbol,
            canonical_symbol=symbol,
            resolved=True,
            alias_symbols=list(aliases_by_symbol.get(symbol, [])),
        )

    return _resolve


_resolve_gene_no_aliases = _fake_resolve_gene({})


def test_fusion_evidence_queries_use_exact_pair_variants_and_tumor_type():
    queries = _fusion_evidence_queries("EML4::ALK", tumor_type="LUAD")

    assert queries[0].startswith('("EML4::ALK" OR "EML4-ALK"')
    assert "lung adenocarcinoma" in queries[0]
    assert "fusion OR rearrangement OR translocation" in queries[1]


def test_record_discusses_exact_fusion_rejects_broad_cooccurrence():
    exact = LiteratureRecord(
        pmid="1",
        title="Recurrent PLAGL1-MYB fusion in leukemia",
        abstract="The PLAGL1-MYB fusion was detected by RNA sequencing.",
        publication_types=["Journal Article"],
    )
    contextual = LiteratureRecord(
        pmid="2",
        title="Gene rearrangements in leukemia",
        abstract="We identified a fusion involving PLAGL1 and MYB in a patient sample.",
        publication_types=["Journal Article"],
    )
    false_positive = LiteratureRecord(
        pmid="3",
        title="Studies of genomic imbalances and the MYB-NFIB gene fusion",
        abstract="PLAGL1 methylation was also evaluated in the same cohort.",
        publication_types=["Journal Article"],
    )

    assert record_discusses_exact_fusion(exact, "PLAGL1::MYB") is True
    assert record_discusses_exact_fusion(contextual, "PLAGL1::MYB") is True
    assert record_discusses_exact_fusion(false_positive, "PLAGL1::MYB") is False
    assert [record.pmid for record in _filter_exact_fusion_records(
        [exact, contextual, false_positive], "PLAGL1::MYB"
    )] == ["1", "2"]


def test_fusion_query_variants_unaffected_when_no_aliases_supplied():
    """No regression: identical output to the pre-alias-expansion behavior."""
    assert _fusion_query_variants("EML4::ALK") == [
        "EML4::ALK", "EML4-ALK", "EML4/ALK", "EML4 ALK",
    ]


def test_fusion_query_variants_expand_hgnc_aliases_for_both_partners():
    variants = _fusion_query_variants(
        "KAT6A::CREBBP", five_aliases=_KAT6A_ALIASES, three_aliases=_CREBBP_ALIASES
    )

    # Literal forms are still present and still first.
    assert variants[:4] == [
        "KAT6A::CREBBP", "KAT6A-CREBBP", "KAT6A/CREBBP", "KAT6A CREBBP",
    ]
    # Legacy nomenclature for either partner is covered, in every notation form.
    assert "MOZ-CBP" in variants
    assert "MOZ::CBP" in variants
    assert "KAT6A-CBP" in variants
    assert "MOZ-CREBBP" in variants


def test_record_discusses_exact_fusion_resolves_kat6a_legacy_aliases():
    """Reproduces the real false negative: a paper phrased only as 'MOZ-CBP'
    (KAT6A's and CREBBP's legacy names) for a submitted KAT6A::CREBBP query."""
    moz_cbp_paper = LiteratureRecord(
        pmid="555",
        title="A recurrent MOZ-CBP fusion identified in pediatric AML",
        abstract=(
            "We report a novel MOZ-CBP fusion transcript detected by RT-PCR "
            "in a pediatric AML patient."
        ),
        publication_types=["Case Reports"],
    )

    # Before this fix (no alias knowledge), this record is silently missed.
    assert record_discusses_exact_fusion(moz_cbp_paper, "KAT6A::CREBBP") is False

    # With HGNC aliases supplied, the alias phrasing is now matched.
    assert record_discusses_exact_fusion(
        moz_cbp_paper,
        "KAT6A::CREBBP",
        five_aliases=_KAT6A_ALIASES,
        three_aliases=_CREBBP_ALIASES,
    ) is True


def test_record_discusses_exact_fusion_alias_guard_rejects_different_fusion():
    """An alias symbol appearing in an unrelated, explicitly different fusion
    notation must still be rejected, exactly like the literal-symbol guard."""
    different_fusion_paper = LiteratureRecord(
        pmid="777",
        title="Characterization of the recurrent MOZ-TIF2 fusion in AML",
        abstract="MOZ-TIF2 fusion transcripts were confirmed in all cases. CREBBP status was not assessed.",
        publication_types=["Journal Article"],
    )

    assert record_discusses_exact_fusion(
        different_fusion_paper,
        "KAT6A::CREBBP",
        five_aliases=_KAT6A_ALIASES,
        three_aliases=_CREBBP_ALIASES,
    ) is False


def test_record_discusses_exact_fusion_literal_contextual_match_unaffected_by_alias_expansion():
    """Regression: the false-positive guard must be computed over ONLY the
    literal requested pair for the literal path, exactly like the original
    literal-only matcher — never over the alias-expanded symbol universe.
    An unrelated fusion notation that overlaps an ALIAS (not the literal
    symbol) of one partner must not retroactively reject an otherwise-valid
    literal contextual match of the submitted pair."""
    record = LiteratureRecord(
        pmid="888",
        title="KAT6A rearrangements in AML",
        abstract=(
            "We describe a case with a KAT6A and CREBBP fusion detected by FISH. "
            "In an unrelated cohort a distinct MOZ-TIF2 fusion was also characterized."
        ),
        publication_types=["Journal Article"],
    )

    # Literal-only baseline: matches via the contextual pattern.
    assert record_discusses_exact_fusion(record, "KAT6A::CREBBP") is True

    # With aliases supplied, the literal path must behave identically. Before
    # the fix, MOZ (an alias of KAT6A) co-occurring with the unrelated TIF2
    # in "MOZ-TIF2" made the false-positive guard fire against the *literal*
    # KAT6A::CREBBP match too, even though MOZ was never a literal symbol and
    # the original guard would never have looked at it.
    assert record_discusses_exact_fusion(
        record,
        "KAT6A::CREBBP",
        five_aliases=_KAT6A_ALIASES,
        three_aliases=_CREBBP_ALIASES,
    ) is True


def test_fusion_query_variants_caps_alias_cross_product():
    """A gene with an unusually long HGNC alias history must not blow up the
    query's alias cross-product — variants are capped per partner."""
    many_aliases = [f"ALIAS{i}" for i in range(20)]

    variants = _fusion_query_variants(
        "KAT6A::CREBBP", five_aliases=many_aliases, three_aliases=[]
    )

    # 1 canonical + capped aliases on the five-prime side, times 1 (no
    # aliases) on the three-prime side, times 4 notation forms.
    capped_five_options = 1 + literature._MAX_QUERY_ALIASES_PER_PARTNER
    assert len(variants) == capped_five_options * 4
    assert "ALIAS19-CREBBP" not in variants
    assert "ALIAS0-CREBBP" in variants


async def test_resolve_fusion_partner_aliases_is_independent_per_partner(monkeypatch):
    """One partner's HGNC lookup failure must not discard the other,
    still-resolvable partner's alias coverage."""

    async def flaky_resolve_gene(symbol, client):
        if symbol == "KAT6A":
            raise httpx.ConnectError("HGNC unreachable", request=None)
        return ResolvedGene(
            input_symbol=symbol,
            canonical_symbol=symbol,
            resolved=True,
            alias_symbols=_CREBBP_ALIASES if symbol == "CREBBP" else [],
        )

    monkeypatch.setattr(literature, "resolve_gene", flaky_resolve_gene)

    five_aliases, three_aliases = await literature.resolve_fusion_partner_aliases(
        "KAT6A", "CREBBP"
    )

    assert five_aliases == []
    assert three_aliases == _CREBBP_ALIASES


async def test_retrieve_fusion_evidence_uses_cache_and_marks_supported(monkeypatch):
    cache_keys = []

    async def fake_cached_call(key, compute, ttl_seconds=None):
        cache_keys.append((key, ttl_seconds))
        return await compute()

    async def fake_esearch(query, max_results, client):
        assert '"Retracted Publication"[Publication Type]' not in query
        return ["1", "2"]

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(
                pmid="1",
                title="EML4-ALK fusion in lung cancer",
                abstract="Patients with EML4-ALK lung cancer respond to kinase inhibitors.",
                journal="J Clin Oncol",
                publication_types=["Journal Article"],
            ),
            LiteratureRecord(
                pmid="2",
                title="Additional EML4-ALK fusion cohort",
                abstract="The EML4-ALK fusion was observed in a second lung cancer cohort.",
                journal="Cancer Res",
                publication_types=["Journal Article"],
            ),
        ]

    monkeypatch.setattr(literature, "cached_call", fake_cached_call)
    monkeypatch.setattr(literature, "resolve_gene", _resolve_gene_no_aliases)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 2)
    monkeypatch.setattr(literature.settings, "fusion_evidence_cache_ttl_seconds", 123)

    result = await retrieve_fusion_evidence("EML4::ALK", tumor_type="LUAD", max_results=10)

    assert result.well_supported is True
    assert result.retrieved_count == 2
    assert set(result.pmids) == {"1", "2"}
    assert result.evidence_cards[0].fusion == "EML4::ALK"
    cards_by_pmid = {card.pmid: card for card in result.evidence_cards}
    assert (
        cards_by_pmid["1"].abstract
        == "Patients with EML4-ALK lung cancer respond to kinase inhibitors."
    )
    # No aliases involved: literal-match cards are unaffected by the new field.
    assert cards_by_pmid["1"].matched_via_alias is False
    assert cards_by_pmid["1"].alias_matches == []
    assert cache_keys[0][0].startswith("fusion_evidence:")
    assert cache_keys[0][1] == 123


async def test_retrieve_fusion_evidence_marks_novelty_only_as_exploratory(monkeypatch):
    async def fake_esearch(query, max_results, client):
        return ["1", "2"]

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(
                pmid="1",
                title="First report of GENE1-GENE2",
                abstract="This first case describes a novel fusion.",
                publication_types=["Case Reports"],
            ),
            LiteratureRecord(
                pmid="2",
                title="Novel GENE1-GENE2 fusion",
                abstract="A previously unreported fusion was found.",
                publication_types=["Case Reports"],
            ),
        ]

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "resolve_gene", _resolve_gene_no_aliases)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 2)

    result = await retrieve_fusion_evidence("GENE1::GENE2", max_results=10)

    assert result.well_supported is False
    assert "novelty" in result.interpretation


async def test_retrieve_fusion_evidence_finds_and_labels_kat6a_alias_only_match(monkeypatch):
    """End-to-end reproduction of the real false negative: KAT6A::CREBBP is
    submitted, but the only paper in PubMed phrases it as 'MOZ-CBP' (KAT6A's
    legacy name). This must now be retrieved via the alias-expanded query and
    the resulting evidence card must be clearly labeled as alias-matched."""

    async def fake_esearch(query, max_results, client):
        # The alias-expanded query must cover the legacy MOZ-CBP phrasing —
        # a purely literal KAT6A::CREBBP query would never surface pmid 555.
        assert "MOZ" in query
        assert "CBP" in query
        return ["555"]

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(
                pmid="555",
                title="A recurrent MOZ-CBP fusion identified in pediatric AML",
                abstract=(
                    "We report a novel MOZ-CBP fusion transcript detected by RT-PCR "
                    "in a pediatric AML patient, expanding the spectrum of MOZ-related "
                    "leukemia rearrangements."
                ),
                journal="Blood",
                publication_types=["Case Reports"],
            )
        ]

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(
        literature,
        "resolve_gene",
        _fake_resolve_gene({"KAT6A": _KAT6A_ALIASES, "CREBBP": _CREBBP_ALIASES}),
    )
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 4)

    result = await retrieve_fusion_evidence("KAT6A::CREBBP", max_results=10)

    assert result.retrieved_count == 1
    assert result.pmids == ["555"]
    assert len(result.evidence_cards) == 1

    card = result.evidence_cards[0]
    assert card.matched_via_alias is True
    alias_symbols_used = {match.alias for match in card.alias_matches}
    assert alias_symbols_used == {"MOZ", "CBP"}
    genes_covered = {match.gene for match in card.alias_matches}
    assert genes_covered == {"KAT6A", "CREBBP"}
    assert "alias" in card.selected_reason.lower()
