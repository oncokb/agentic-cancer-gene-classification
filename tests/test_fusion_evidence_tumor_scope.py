from __future__ import annotations

import asyncio
import json

import pytest

from src import main
from src.models.schema import (
    AnnotationResult,
    FusionEvidenceCard,
    FusionEvidenceResult,
    FusionInput,
    LiteratureRecord,
    ResolvedGene,
)
from src.pipeline import literature, result_sanitizer
from src.pipeline.literature import (
    _fusion_evidence_cache_key,
    requested_tumor_terms_in_record,
    retrieve_fusion_evidence,
)
from src.pipeline.result_sanitizer import sanitize_annotation_result, sanitize_fusion_evidence_result


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


async def _resolve_no_aliases(symbol, client):
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=True)


def _record(pmid: str, title: str, abstract: str = "") -> LiteratureRecord:
    return LiteratureRecord(
        pmid=pmid, title=title, abstract=abstract, journal="Oncotarget",
        publication_types=["Journal Article"],
    )


def _patch_retrieval(monkeypatch, records, queries_seen=None):
    async def fake_esearch(query, max_results, client):
        if queries_seen is not None:
            queries_seen.append(query)
        return [record.pmid for record in records]

    async def fake_efetch(pmids, client):
        return [record for record in records if record.pmid in pmids]

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "resolve_gene", _resolve_no_aliases)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 3)


# ---------------------------------------------------------------------------
# Matcher
# ---------------------------------------------------------------------------

def _terms(tumor_type, title="", abstract=""):
    return requested_tumor_terms_in_record(_record("1", title, abstract), tumor_type)


def test_short_abbreviations_match_case_sensitively():
    assert _terms("ALL", abstract="KMT2A-AFF1 is common in infant ALL.") == ["ALL"]
    assert _terms("ALL", abstract="KMT2A-AFF1 was found in all patients.") == []
    assert _terms("AML", abstract="Recurrent in pediatric AMLs.") == ["AML"]
    assert _terms("AML", abstract="aml cohort") == []


def test_two_letter_abbreviations_are_title_only():
    assert _terms("MM", abstract="A tumor of 5 mm carried the fusion.") == []
    assert _terms("MM", abstract="The fusion was found in MM patients.") == []
    assert _terms("MM", title="IGH::CCND1 in MM") == ["MM"]
    assert _terms("MM", abstract="Seen in multiple myeloma.") == ["multiple myeloma"]
    assert _terms("FL", title="5 fl of sample") == []


def test_phrases_match_on_word_boundaries_with_plural_and_hyphen_variants():
    assert _terms("LUAD", abstract="Three lung adenocarcinomas harbored it.") == ["lung adenocarcinoma"]
    assert _terms("NSCLC", abstract="in non small cell lung cancer") == ["non-small cell lung cancer"]
    assert _terms("NSCLC", abstract="in nsclc") == []
    assert _terms("SARC", abstract="Found in an osteosarcoma.") == []
    assert _terms("SARC", abstract="Found in a soft tissue sarcoma.") == ["sarcoma"]
    assert _terms(None, abstract="lung adenocarcinoma") == []
    # A tumor type outside the alias table is matched as given.
    assert _terms("Infantile fibrosarcoma", abstract="ETV6-NTRK3 infantile fibrosarcomas") == [
        "Infantile fibrosarcoma"
    ]


UTROSCT_ABSTRACT = (
    "Uterine tumor resembling ovarian sex cord tumor (UTROSCT) is a rare uterine neoplasm. "
    "We describe an ESR1::NCOA3 fusion in a uterine tumour resembling ovarian sex-cord tumours."
)


@pytest.mark.parametrize("tumor_type", ["OV", "ovarian", "Ovarian sex cord tumor"])
def test_utrosct_abstract_does_not_count_as_ovarian(tumor_type):
    assert _terms(tumor_type, title="ESR1::NCOA3 in UTROSCT", abstract=UTROSCT_ABSTRACT) == []


def test_utrosct_abstract_matches_a_utrosct_request():
    assert _terms("UTROSCT", abstract=UTROSCT_ABSTRACT) == [
        "uterine tumor resembling ovarian sex cord tumor",
        "uterine tumor resembling ovarian sex-cord tumor",
        "UTROSCT",
    ]


def test_ovarian_mention_outside_utrosct_still_counts():
    abstract = UTROSCT_ABSTRACT + " The same fusion was reported in ovarian cancer."
    assert _terms("OV", abstract=abstract) == ["ovarian cancer"]


# ---------------------------------------------------------------------------
# Classification and interpretation
# ---------------------------------------------------------------------------

async def test_requested_tumor_counts_a_match_outside_the_top_five_cards(monkeypatch):
    # Same tier/journal: ranked by descending PMID, so PMID 1 ranks 7th.
    records = [_record(str(pmid), f"EML4-ALK fusion study {pmid}", "EML4-ALK in lung cancer.")
               for pmid in range(2, 8)]
    records.append(_record("1", "EML4-ALK fusion", "EML4-ALK in lung adenocarcinoma."))
    queries = []
    _patch_retrieval(monkeypatch, records, queries)

    result = await retrieve_fusion_evidence("EML4::ALK", tumor_type="LUAD", max_results=10)

    assert len(queries) == 2  # no extra PubMed calls
    assert result.pmids == ["7", "6", "5", "4", "3", "2", "1"]
    assert [card.pmid for card in result.evidence_cards] == ["7", "6", "5", "4", "3"]
    assert all(card.requested_tumor_terms == [] for card in result.evidence_cards)
    assert result.literature_scope == "requested_tumor"
    assert result.requested_tumor_pmids == ["1"]
    assert result.retrieval_status == "ok"
    assert result.interpretation == (
        "EML4::ALK has 7 non-retracted PubMed record(s) from exact fusion-pair searches, "
        "suggesting this is a studied fusion event rather than a single novelty-only report. "
        "Papers mentioning LUAD in the title or abstract: 1 of 7."
    )


async def test_requested_tumor_terms_are_set_on_matching_cards(monkeypatch):
    _patch_retrieval(monkeypatch, [
        _record("2", "EML4-ALK fusion in lung adenocarcinoma", "EML4-ALK positive LUAD."),
        _record("1", "EML4-ALK fusion", "EML4-ALK in lung cancer."),
    ])

    result = await retrieve_fusion_evidence("EML4::ALK", tumor_type="LUAD", max_results=10)

    assert result.requested_tumor_pmids == ["2"]
    cards = {card.pmid: card for card in result.evidence_cards}
    assert cards["2"].requested_tumor_terms == ["lung adenocarcinoma", "LUAD"]
    assert cards["1"].requested_tumor_terms == []
    assert "in LUAD" not in result.interpretation
    assert result.interpretation.endswith("Papers mentioning LUAD in the title or abstract: 1 of 2.")


async def test_other_or_unspecified_when_no_paper_mentions_the_tumor(monkeypatch):
    _patch_retrieval(monkeypatch, [
        _record("2", "EML4-ALK fusion", "EML4-ALK in lung cancer."),
        _record("1", "EML4-ALK fusion review", "EML4-ALK in all patients."),
    ])

    result = await retrieve_fusion_evidence("EML4::ALK", tumor_type="ALL", max_results=10)

    assert result.literature_scope == "other_or_unspecified"
    assert result.requested_tumor_pmids == []
    assert result.interpretation == (
        "EML4::ALK has 2 non-retracted PubMed record(s) from exact fusion-pair searches, "
        "below the well-supported threshold. None of the retrieved papers mention ALL."
    )
    assert " in ALL" not in result.interpretation


async def test_any_tumor_when_no_tumor_type_requested(monkeypatch):
    _patch_retrieval(monkeypatch, [_record("1", "EML4-ALK fusion", "EML4-ALK in lung adenocarcinoma.")])

    result = await retrieve_fusion_evidence("EML4::ALK", max_results=10)

    assert result.literature_scope == "any_tumor"
    assert result.requested_tumor_pmids == []
    assert result.evidence_cards[0].requested_tumor_terms == []
    assert result.interpretation == (
        "EML4::ALK has 1 non-retracted PubMed record(s) from exact fusion-pair searches, "
        "below the well-supported threshold."
    )


@pytest.mark.parametrize("tumor_type", [None, "LUAD"])
async def test_no_literature_when_nothing_is_retained(monkeypatch, tumor_type):
    # Retrieved, but does not discuss the exact pair, so nothing is retained.
    _patch_retrieval(monkeypatch, [_record("1", "ALK in lung adenocarcinoma", "EML4 and ALK.")])

    result = await retrieve_fusion_evidence("EML4::ALK", tumor_type=tumor_type, max_results=10)

    assert result.pmids == []
    assert result.literature_scope == "no_literature"
    assert result.retrieval_status == "ok"
    assert result.interpretation == (
        "No non-retracted PubMed records were found for exact EML4::ALK fusion-pair searches."
    )


async def test_non_pair_input_is_not_classified(monkeypatch):
    _patch_retrieval(monkeypatch, [])

    result = await retrieve_fusion_evidence("ALK", tumor_type="LUAD", max_results=10)

    assert result.literature_scope is None
    assert result.retrieval_status == "ok"


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

async def test_retrieval_exception_is_failed_not_no_literature(monkeypatch):
    async def boom(fusion, tumor_type=None):
        raise RuntimeError("PubMed unavailable")

    monkeypatch.setattr(main, "retrieve_fusion_evidence", boom)

    result = await main._retrieve_fusion_evidence_input(
        FusionInput(fusion="EML4::ALK", tumor_type="LUAD"), asyncio.Semaphore(1)
    )

    assert result.retrieval_status == "failed"
    assert result.literature_scope is None
    assert result.pmids == []
    assert "PubMed unavailable" in result.interpretation


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def test_cache_key_is_bumped_for_tumor_scope(monkeypatch):
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 3)
    key = _fusion_evidence_cache_key("EML4::ALK", " LUAD ", 10)
    pre_scope_key = "fusion_evidence:" + json.dumps(
        {
            "fusion": "EML4::ALK",
            "tumor_type": "luad",
            "max_results": 10,
            "min_papers": 3,
            "exact_fusion_filter_version": 2,
            "retraction_filter_version": 2,
            "alias_expansion_version": 1,
        },
        sort_keys=True,
    )

    assert key != pre_scope_key
    assert json.loads(key.removeprefix("fusion_evidence:"))["tumor_scope_version"] == 1


async def test_retrieve_fusion_evidence_uses_the_bumped_cache_key(monkeypatch):
    keys = []

    async def fake_cached_call(key, compute, ttl_seconds=None):
        keys.append(key)
        return await compute()

    _patch_retrieval(monkeypatch, [])
    monkeypatch.setattr(literature, "cached_call", fake_cached_call)

    await retrieve_fusion_evidence("EML4::ALK", tumor_type="LUAD", max_results=10)

    assert keys == [_fusion_evidence_cache_key("EML4::ALK", "LUAD", 10)]


# ---------------------------------------------------------------------------
# Saved-run sanitizer
# ---------------------------------------------------------------------------

def _card(pmid, title, terms=()):
    return FusionEvidenceCard(
        fusion="EML4::ALK", pmid=pmid, title=title, abstract=f"{title}.",
        requested_tumor_terms=list(terms),
    )


def _scoped_result():
    return FusionEvidenceResult(
        fusion="EML4::ALK",
        tumor_type="LUAD",
        well_supported=True,
        retrieved_count=7,
        pmids=["7", "6", "5", "4", "3", "2", "1"],
        interpretation="live interpretation",
        evidence_cards=[_card(pmid, "EML4-ALK fusion in lung cancer") for pmid in "76543"],
        literature_scope="requested_tumor",
        requested_tumor_pmids=["1"],
    )


@pytest.fixture
def _sanitizer_offline(monkeypatch):
    retracted: set[str] = set()

    async def fake_find_retracted_pmids(_pmids):
        return set(retracted)

    async def no_aliases(five_prime, three_prime):
        return [], []

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", fake_find_retracted_pmids)
    monkeypatch.setattr(result_sanitizer, "resolve_fusion_partner_aliases", no_aliases)
    return retracted


async def test_sanitizer_round_trip_preserves_scope_fields(_sanitizer_offline):
    original = _scoped_result()
    original.evidence_cards[0].requested_tumor_terms = ["LUAD"]
    stored = AnnotationResult(
        run_id="run-scope", timestamp="2026-10-26", fusions_processed=1, genes_annotated=0, annotations=[],
        fusion_evidence=[original],
    ).model_dump()

    sanitized, changed = await sanitize_annotation_result(AnnotationResult(**stored))

    assert changed is False
    reopened = sanitized.fusion_evidence[0]
    assert reopened == original
    assert reopened.pmids == ["7", "6", "5", "4", "3", "2", "1"]
    assert reopened.literature_scope == "requested_tumor"
    assert reopened.requested_tumor_pmids == ["1"]
    assert reopened.retrieval_status == "ok"
    assert reopened.evidence_cards[0].requested_tumor_terms == ["LUAD"]
    assert reopened.well_supported is True


async def test_sanitizer_keeps_scope_consistent_when_requested_pmid_is_retracted(_sanitizer_offline):
    _sanitizer_offline.add("1")
    result = _scoped_result()

    changed = await sanitize_fusion_evidence_result(result, {"1"})

    assert changed is True
    assert result.pmids == ["7", "6", "5", "4", "3", "2"]
    assert result.requested_tumor_pmids == []
    assert result.literature_scope == "other_or_unspecified"
    assert result.interpretation.endswith("None of the retrieved papers mention LUAD.")


async def test_sanitizer_drops_pmid_whose_card_fails_reverification(_sanitizer_offline):
    result = _scoped_result()
    result.evidence_cards[0] = _card("7", "BCR-ABL1 fusion in leukemia")
    result.requested_tumor_pmids = ["7"]

    changed = await sanitize_fusion_evidence_result(result, set())

    assert changed is True
    assert result.pmids == ["6", "5", "4", "3", "2", "1"]
    assert result.literature_scope == "other_or_unspecified"


async def test_sanitizer_does_not_reclassify_old_runs(_sanitizer_offline):
    old = {
        "fusion": "EML4::ALK",
        "tumor_type": "LUAD",
        "well_supported": False,
        "retrieved_count": 1,
        "pmids": ["7"],
        "interpretation": "EML4::ALK in LUAD has 1 non-retracted PubMed record(s).",
        "evidence_cards": [_card("7", "EML4-ALK fusion in lung adenocarcinoma").model_dump(
            exclude={"requested_tumor_terms"}
        )],
    }
    result = FusionEvidenceResult(**old)

    changed = await sanitize_fusion_evidence_result(result, set())

    assert changed is False
    assert result.literature_scope is None
    assert result.requested_tumor_pmids == []
    assert result.retrieval_status == "ok"
    assert result.evidence_cards[0].requested_tumor_terms == []
