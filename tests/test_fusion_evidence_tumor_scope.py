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


async def _uncached(_key, compute, ttl_seconds=None, force_refresh=False):
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
    # A tumor type outside the alias table is matched by its normalized form.
    assert _terms("Infantile  Fibrosarcoma", abstract="ETV6-NTRK3 infantile fibrosarcomas") == [
        "infantile fibrosarcoma"
    ]
    # Hyphens inside an alias are part of it.
    assert _terms("NSCLC", abstract="in non-small-cell lung cancer") == ["non-small cell lung cancer"]


@pytest.mark.parametrize("tumor_type, text", [
    ("breast", "Infants were exposed through breast-feeding."),
    ("lung", "Patients received lung-protective ventilation."),
    ("liver", "We used liver-derived organoids."),
    ("ALL", "Detected in ALL-like cell lines."),
    # "small cell lung cancer" (SCLC) must not match inside "non-small cell lung cancer".
    ("SCLC", "Seen in non-small cell lung cancer."),
])
def test_hyphenated_neighbour_ends_a_match(tumor_type, text):
    assert _terms(tumor_type, abstract=text) == []


@pytest.mark.parametrize("tumor_type, text, expected", [
    ("breast", "A cohort of breast cancer patients.", ["breast"]),
    ("breast", "Two breast carcinomas carried the fusion.", ["breast"]),
    ("breast cancer", "Two breast cancers carried the fusion.", ["breast cancer"]),
    ("lung", "Seen in lung, breast-feeding aside.", ["lung"]),
    ("SCLC", "Seen in small-cell lung cancer.", ["small cell lung cancer"]),
])
def test_unhyphenated_mentions_still_match(tumor_type, text, expected):
    assert _terms(tumor_type, abstract=text) == expected


# En dash, em dash, non-breaking hyphen (U+2011), minus sign (U+2212), the
# compatibility dashes U+FE58, U+FE63, U+FF0D, hyphen bullet U+2043 and soft hyphen.
UNICODE_DASHES = pytest.mark.parametrize(
    "dash",
    ["\u2013", "\u2014", "\u2011", "\u2212", "\ufe58", "\ufe63", "\uff0d", "\u2043", "\u00ad"],
    ids=[
        "en", "em", "nb-hyphen", "minus", "small-em", "small-hyphen", "fullwidth", "hyphen-bullet",
        "soft-hyphen",
    ],
)


@UNICODE_DASHES
@pytest.mark.parametrize("tumor_type, text", [
    ("SCLC", "Seen in non{d}small cell lung cancer."),
    ("breast", "Infants were exposed through breast{d}feeding."),
    ("ALL", "Detected in ALL{d}like cell lines."),
    ("liver", "We used liver{d}derived organoids."),
    ("ovarian", "Uterine tumor resembling ovarian sex{d}cord tumor with ESR1::NCOA3."),
    ("ovarian", "Uterine tumor resembling ovarian sex cord tumor{d}like lesion."),
])
def test_unicode_dashes_end_a_match_like_hyphens(dash, tumor_type, text):
    assert _terms(tumor_type, abstract=text.format(d=dash)) == []


@UNICODE_DASHES
def test_unicode_dash_inside_an_alias_still_matches(dash):
    abstract = f"Seen in non{dash}small cell lung cancer."
    assert _terms("NSCLC", abstract=abstract) == ["non-small cell lung cancer"]


@pytest.mark.parametrize("tumor_type, text, expected", [
    # Folded to "-", not deleted: deleting would give "sexcord" and escape the UTROSCT exclusion.
    ("ovarian", "Uterine tumor resembling ovarian sex\u00adcord tumor.", []),
    ("breast", "Exposure through breast\u00adfeeding.", []),
    ("SCLC", "Seen in small\u00adcell lung cancer.", ["small cell lung cancer"]),
    ("ALL", "Recurrent fusions in B\u00adALL.", ["B-ALL"]),
])
def test_soft_hyphen_behaves_like_a_hyphen(tumor_type, text, expected):
    assert _terms(tumor_type, abstract=text) == expected


@pytest.mark.parametrize("text", [
    "Seen in non-\nsmall cell lung cancer.",
    "Seen in non- small cell lung cancer.",
    "Seen in non\u2013 small cell lung cancer.",
])
def test_line_broken_non_prefix_ends_a_match(text):
    assert _terms("SCLC", abstract=text) == []


@pytest.mark.parametrize("ascii_form, dashed_form", [
    ("non-small cell lung cancer", "non\u2013small cell lung cancer"),
    ("non-small cell lung cancer", "Non\uff0dSmall  Cell Lung Cancer"),
    ("uterine tumor resembling ovarian sex-cord tumor", "uterine tumor resembling ovarian sex\u2013cord tumor"),
])
def test_dashed_tumor_type_shares_identity_expansion_and_matching(ascii_form, dashed_form):
    assert literature.normalize_tumor_identity(dashed_form) == literature.normalize_tumor_identity(ascii_form)
    assert literature._expand_tumor_type_terms(dashed_form) == literature._expand_tumor_type_terms(ascii_form)
    assert len(literature._expand_tumor_type_terms(dashed_form)) > 1  # alias-expanded
    assert literature._fusion_evidence_cache_key("A::B", dashed_form, 10) == (
        literature._fusion_evidence_cache_key("A::B", ascii_form, 10)
    )
    for title, abstract in [("NSCLC cohort", ""), ("ESR1::NCOA3 in UTROSCT", UTROSCT_ABSTRACT)]:
        assert _terms(dashed_form, title=title, abstract=abstract) == (
            _terms(ascii_form, title=title, abstract=abstract)
        )


def test_dashed_utrosct_request_is_not_masked_by_its_own_exclusion():
    dashed = "uterine tumor resembling ovarian sex\u2013cord tumor"
    assert _terms(dashed, abstract=UTROSCT_ABSTRACT) == _terms("UTROSCT", abstract=UTROSCT_ABSTRACT)


@pytest.mark.parametrize("tumor_type, text", [
    ("SCLC", "Seen in non small cell lung cancer."),
    ("SCLC", "Seen in Non  small cell lung cancer."),
    ("HL", "Seen in non Hodgkin lymphoma."),
])
def test_non_prefix_with_a_space_ends_a_match(tumor_type, text):
    assert _terms(tumor_type, abstract=text) == []


def test_non_prefixed_alias_still_matches_its_own_request():
    assert _terms("NSCLC", abstract="Seen in non small cell lung cancer.") == [
        "non-small cell lung cancer",
    ]


@pytest.mark.parametrize("text, expected", [
    ("Recurrent fusions in pediatric B-ALL.", ["B-ALL"]),
    ("Recurrent fusions in T\u2013ALL.", ["T-ALL"]),
    ("Recurrent fusions in B-ALL and T-ALL.", ["B-ALL", "T-ALL"]),
    ("Recurrent fusions in ALL.", ["ALL"]),
])
def test_b_and_t_all_count_for_an_all_request(text, expected):
    assert _terms("ALL", abstract=text) == expected


OS_ABSTRACT = "The fusion was detected in 12 patients; median OS was 12 months."


@pytest.mark.parametrize("tumor_type", ["os", "OS", "Os", " oS "])
def test_unknown_short_code_is_an_abbreviation_whatever_its_case(tumor_type):
    assert _terms(tumor_type, abstract=OS_ABSTRACT) == []
    assert _terms(tumor_type, abstract="os and Os in the abstract") == []
    assert _terms(tumor_type, title="EWSR1 fusion in OS") == ["OS"]


async def test_os_and_OS_requests_classify_identically(monkeypatch):
    records = [
        _record("2", "EWSR1-FLI1 fusion cohort", f"EWSR1-FLI1 fusion. {OS_ABSTRACT}"),
        _record("1", "EWSR1-FLI1 fusion", "EWSR1-FLI1 in Os and os."),
    ]
    _patch_retrieval(monkeypatch, records)

    lower = await retrieve_fusion_evidence("EWSR1::FLI1", tumor_type="os", max_results=10)
    upper = await retrieve_fusion_evidence("EWSR1::FLI1", tumor_type="OS", max_results=10)

    assert _fusion_evidence_cache_key("EWSR1::FLI1", "os", 10) == _fusion_evidence_cache_key(
        "EWSR1::FLI1", "OS", 10
    )
    for result in (lower, upper):
        assert result.literature_scope == "other_or_unspecified"
        assert result.requested_tumor_pmids == []
    assert [card.requested_tumor_terms for card in lower.evidence_cards] == [
        card.requested_tumor_terms for card in upper.evidence_cards
    ]


@pytest.mark.parametrize("tumor_type", ["dsrct", "DSRCT", "Dsrct"])
def test_unknown_lowercase_code_matches_its_uppercase_token(tumor_type):
    assert _terms(tumor_type, abstract="EWSR1-WT1 defines DSRCT.") == ["DSRCT"]
    assert _terms(tumor_type, abstract="EWSR1-WT1 defines dsrct.") == []


@pytest.mark.parametrize("tumor_type", ["pecoma", "PEComa", "PECOMA"])
def test_unknown_longer_name_matches_as_a_word(tumor_type):
    # Six characters: a name, matched case-insensitively.
    assert _terms(tumor_type, abstract="TFE3-rearranged PEComas") == ["pecoma"]


def test_short_tumor_words_are_not_treated_as_codes():
    assert _terms("Lung", abstract="Found in lung tumors.") == ["lung"]
    assert _terms("LUNG", abstract="Found in lung tumors.") == ["lung"]


def test_known_lowercase_codes_still_expand():
    assert _terms("luad", abstract="EML4-ALK in LUAD.") == ["LUAD"]
    assert _terms("luad", abstract="EML4-ALK in luad.") == []
    assert _terms("luad", abstract="in lung adenocarcinoma") == ["lung adenocarcinoma"]


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
    assert result.retrieval_status == "failed"


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------

async def test_retrieval_exception_is_failed_not_no_literature(monkeypatch):
    async def boom(fusion, tumor_type=None, force_refresh=False):
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
    assert json.loads(key.removeprefix("fusion_evidence:"))["tumor_scope_version"] == 6


async def test_retrieve_fusion_evidence_uses_the_bumped_cache_key(monkeypatch):
    keys = []

    async def fake_cached_call(key, compute, ttl_seconds=None, force_refresh=False):
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


@pytest.mark.parametrize("tumor_type, text, expected", [
    ("HCC", "A hepato\u00adcellular carcinoma cohort.", ["hepatocellular carcinoma"]),
    ("hepatocellular carcinoma", "A hepato\u00adcellular carcinoma cohort.", ["hepatocellular carcinoma"]),
    ("LUAD", "A lung adeno\u00adcarcinoma cohort.", ["lung adenocarcinoma"]),
    ("HCC", "A hepato\u00adcellular carcinoma-like lesion.", []),
    ("LUAD", "Non-lung adeno\u00adcarcinoma tissue.", []),
    ("ovarian", "Uterine tumor resembling ovarian sex\u00adcord tumor; hepato\u00adcellular carcinoma.", []),
    ("breast", "Breast\u00adfeeding; lung adeno\u00adcarcinoma.", []),
    ("breast", "Breast\u00adfeeding; breast cancer.", ["breast"]),
    ("ovarian", "Uterine tumor resembling ovar\u00adian sex cord tumor.", []),
])
@pytest.mark.parametrize("field", ["title", "abstract"])
def test_soft_hyphens_inside_words_preserve_guards(tumor_type, text, expected, field):
    assert _terms(tumor_type, **{field: text}) == expected


def test_free_text_query_fallback_uses_cache_identity():
    forms = ["breast-cancer", "breast–cancer", "  Breast－Cancer  "]
    assert len({literature._fusion_evidence_cache_key("A::B", form, 10) for form in forms}) == 1
    queries = [literature._fusion_evidence_queries("A::B", form, [], []) for form in forms]
    assert queries[0] == queries[1] == queries[2]
    assert all(literature._expand_tumor_type_terms(form) == ["breast-cancer"] for form in forms)


@pytest.mark.parametrize("tumor_type, text, expected", [
    ("ovarian", "Uterine tumor resembling ovar\u00adian sex\u00adcord tumor.", []),
    ("ovarian", "Uterine tu\u00admor resembling ovarian sex cord tumor.", []),
    ("ovarian", "Uterine tumor resem\u00adbling ovarian sex cord tumor.", []),
    ("ovarian", "Uterine tumor resembling ovarian sexcord tumor.", []),
    ("SCLC", "Seen in no\u00adn small cell lung cancer.", []),
    ("SCLC", "Seen in non-\u00adsmall cell lung cancer.", []),
    ("SCLC", "Seen in non\u00ad-small cell lung cancer.", []),
    ("ALL", "Recurrent fusions in B-ALL.", ["B-ALL"]),
    ("ALL", "Recurrent fusions in B\u00adALL.", ["B-ALL"]),
    ("breast", "Exposure through breastfeeding.", []),
    ("breast", "Exposure through breast\u00adfeeding.", []),
    # A veto applies only to its own span, preserving independent true mentions.
    ("ovarian", "Uterine tu\u00admor resembling ovarian sexcord tumor; ovarian carcinoma.", ["ovarian"]),
    ("SCLC", "No\u00adn small cell lung cancer; small cell lung cancer.", ["small cell lung cancer"]),
])
@pytest.mark.parametrize("field", ["title", "abstract"])
def test_either_normalization_can_veto_a_tumor_span(tumor_type, text, expected, field):
    assert _terms(tumor_type, **{field: text}) == expected
