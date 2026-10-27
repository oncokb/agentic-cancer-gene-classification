import asyncio

import pytest
from fastapi.testclient import TestClient

from src import main
from src.models.schema import LiteratureRecord
from src.pipeline import feedback_citations
from src.pipeline.feedback_citations import (
    _gene_mentioned,
    _usable_aliases,
    build_citation_check_section,
    extract_subject,
    extract_tumor_type,
    parse_cited_pmids,
)

_REAL_RESOLVE_ALIASES = feedback_citations._resolve_aliases


def _record(pmid: str, title: str, abstract: str = "", **kwargs) -> LiteratureRecord:
    return LiteratureRecord(pmid=pmid, title=title, abstract=abstract, journal="Test", **kwargs)


@pytest.fixture
def pubmed(monkeypatch):
    monkeypatch.setattr(feedback_citations.literature, "_get_ncbi_client", lambda: object())
    calls = {"records": []}

    async def fake_efetch(pmids, client, *, filter_retracted=True):
        calls["pmids"] = list(pmids)
        calls["client"] = client
        calls["filter_retracted"] = filter_retracted
        return [record for record in calls["records"] if record.pmid in pmids]

    async def fake_aliases(five, three):
        return [], []

    monkeypatch.setattr(feedback_citations.literature, "_efetch", fake_efetch)
    monkeypatch.setattr(feedback_citations, "_resolve_aliases", fake_aliases)
    return calls


def test_parse_pmids_from_label_text_and_urls():
    text = "See PMID: 41424301 and pubmed.ncbi.nlm.nih.gov/41424302/ for details."
    assert parse_cited_pmids(text) == ["41424301", "41424302"]


def test_parse_plural_label_with_multiple_pmids():
    text = "PMIDs: 41424301, 41424302 and 41424303 are relevant."
    assert parse_cited_pmids(text) == ["41424301", "41424302", "41424303"]


def test_parse_plural_label_with_whitespace_separated_pmids():
    assert parse_cited_pmids("PMIDs 41424301 41424302 41424303") == [
        "41424301",
        "41424302",
        "41424303",
    ]


def test_parse_structured_pmids_accepts_common_pasted_forms():
    assert parse_cited_pmids(None, ["12345", "PMID 41424301"]) == ["12345", "41424301"]


def test_parse_structured_pmid_prefers_six_to_nine_digit_run():
    assert parse_cited_pmids(None, ["2025 paper 41424301"]) == ["41424301"]


def test_parse_structured_pmid_rejects_multiple_candidates():
    assert parse_cited_pmids(None, ["41424301 and 41424302"]) == []
    assert parse_cited_pmids(None, ["doi:10.1038/s41586-023-06123-4"]) == []


def test_parse_structured_pmid_rejects_pmcid():
    assert parse_cited_pmids(None, ["PMC1234567"]) == []


def test_parse_label_without_space():
    assert parse_cited_pmids("See PMID12345678 for details.") == ["12345678"]


def test_parse_pmids_from_structured_field_and_dedupe():
    text = "The cited paper is PMID 41424301."
    assert parse_cited_pmids(text, ["99999999", "41424301"]) == ["99999999", "41424301"]


def test_parse_pmids_respects_cap(monkeypatch):
    monkeypatch.setattr(feedback_citations.settings, "feedback_citation_max_pmids", 2)
    text = "PMID 11111111 PMID 22222222 PMID 33333333"
    assert parse_cited_pmids(text) == ["11111111", "22222222"]


def test_parse_rejects_long_numbers_instead_of_truncating():
    assert parse_cited_pmids("PMID 1234567890") == []
    assert parse_cited_pmids("pubmed.ncbi.nlm.nih.gov/1234567890/") == []


def test_parse_requires_separator_between_bare_numbers():
    assert parse_cited_pmids("PMID 11111111 22222222") == ["11111111"]
    assert parse_cited_pmids("PMID 11111111 PMID 22222222") == ["11111111", "22222222"]


def test_parse_pmids_ignores_non_pmid_numbers():
    assert parse_cited_pmids("Run 123 on version 2.5 with sample 1234567890") == []


@pytest.mark.parametrize(
    "message,expected,is_fusion",
    [
        ("Review GREB1::BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review GREB1-BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review GREB1/BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review GREB1 -- BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review EML4-ALK fusion annotation.", "EML4::ALK", True),
        ("Review the gene TP53 annotation.", "TP53", False),
    ],
)
def test_extract_subject(message, expected, is_fusion):
    subject, fusion = extract_subject(message)
    assert (subject, fusion) == (expected, is_fusion)


@pytest.mark.parametrize(
    "message",
    [
        "Review NSCLC/SCLC annotation.",
        "Review IHC/FISH results.",
        "Review KRAS-G12C variant.",
        "Review HLA-DRB1 annotation.",
        "Review MT-CO1 annotation.",
        "Review CDKN2A/B annotation.",
        "Review BCR-ABL1 annotation.",
    ],
)
def test_extract_subject_rejects_non_fusion_tokens(message):
    subject, is_fusion = extract_subject(message)
    assert is_fusion is False


@pytest.mark.parametrize(
    "message",
    [
        "RNA-Seq detected the fusion.",
        "Next-Generation sequencing.",
        "Fusion-Positive.",
        "Kras-Mutant.",
        "PMID/PMCID mismatch.",
        "Wild-Type allele; no fusion.",
    ],
)
def test_mixed_case_words_are_not_gene_tokens(message):
    assert extract_subject(message) == (None, False)


def test_explicit_gene_wins_over_earlier_mixed_case_words():
    assert extract_subject("Wild-Type allele; EML4-ALK fusion.") == ("EML4::ALK", True)
    assert extract_subject("Yes/No answer for GREB1::BCL9L") == ("GREB1::BCL9L", True)


def test_explicit_double_colon_fusion_is_preferred_over_slash():
    assert extract_subject("TP53/EGFR and EML4::ALK fusion.") == ("EML4::ALK", True)


def test_explicit_double_colon_wins_over_structured_gene():
    assert extract_subject("Review GREB1::BCL9L fusion.", "TP53") == ("GREB1::BCL9L", True)


def test_slash_pair_is_only_a_fusion_when_a_fusion_word_is_adjacent():
    assert extract_subject("TP53/EGFR co-mutation; GREB1-BCL9L fusion") == (
        "GREB1::BCL9L",
        True,
    )
    assert extract_subject("Review TP53/EGFR co-mutation.") == (None, False)
    assert extract_subject("BCR/ABL1 fusion") == ("BCR::ABL1", True)
    assert extract_subject("fusion BCR/ABL1") == ("BCR::ABL1", True)
    assert extract_subject("BCR/ABL1 rearrangement") == ("BCR::ABL1", True)
    assert extract_subject("BCR/ABL1 translocation") == ("BCR::ABL1", True)


def test_clinical_acronyms_are_not_fusion_partners():
    assert extract_subject("ER/PR status; GREB1-BCL9L fusion") == ("GREB1::BCL9L", True)


def test_structured_gene_wins_over_adjacent_slash_pair():
    assert extract_subject(
        "TP53/EGFR co-mutation; GREB1-BCL9L fusion", "TP53"
    ) == ("TP53", False)


def test_hyphenated_gene_symbol_is_not_mistaken_for_a_fusion():
    assert extract_subject("Review HLA-A annotation.", "HLA-A") == ("HLA-A", False)
    assert extract_subject("Review CDKN2A/B annotation.", "CDKN2A/B") == ("CDKN2A/B", False)
    assert extract_subject("Review BCR-ABL1 fusion.", "BCR-ABL1") == ("BCR-ABL1", True)


@pytest.mark.parametrize(
    "message,tumor",
    [
        ("Review GREB1::BCL9L fusion annotation for ovarian cancer.", "ovarian cancer"),
        ("This annotation is missing melanoma context.", "melanoma"),
        ("Reported in a patient with pancreatic cancer and liver metastases.", "pancreatic cancer"),
        ("Review TP53 in NSCLC.", "NSCLC"),
        ("Review TP53 in nsclc.", None),
        ("Review TP53 in ALL.", "ALL"),
        ("Review TP53 in MM.", "MM"),
        ("Review TP53 in mm.", None),
        ("Review TP53 in READ.", "READ"),
        ("Review TP53 in read.", None),
        ("Review TP53 in GC.", "GC"),
        ("Review TP53 in gc.", None),
        ("Review TP53 in UC.", "UC"),
        ("Review TP53 in uc.", None),
        ("Reported in biliary tract cancer.", "biliary tract cancer"),
        ("Reported in ovarian cancers.", "ovarian cancer"),
        ("Reported in non\u2013small cell lung cancer.", "non-small cell lung cancer"),
        ("Review in ovarian carcinoma.", "ovarian carcinoma"),
        ("Review in biliary carcinoma.", "biliary carcinoma"),
        ("Analyze GC content.", None),
        ("GC-rich regions were enriched.", None),
        ("The annotation for this tumor is wrong.", None),
        ("Please fix the export dropdown.", None),
    ],
)
def test_extract_tumor_type(message, tumor):
    assert extract_tumor_type(message) == tumor


@pytest.mark.parametrize(
    "gene,text,expected",
    [
        ("KIT", "The kit was used for staining.", False),
        ("KIT", "KIT mutations were detected.", True),
        ("MET", "We met to discuss the results.", False),
        ("MET", "MET amplification was detected.", True),
        ("SET", "Please set the option.", False),
        ("SET", "SET was lost in the tumor.", True),
    ],
)
def test_short_gene_symbols_match_case_sensitively(gene, text, expected):
    assert _gene_mentioned(text, gene, []) is expected


def test_alk_requires_word_boundaries():
    assert _gene_mentioned("CALK and ALKBH5 were measured", "ALK", []) is False
    assert _gene_mentioned("The EML4-ALK fusion was detected", "ALK", []) is True


def test_usable_aliases_drops_short_and_common_words():
    assert _usable_aliases(["K", "ABL", "SET", "met", "MOZ"]) == ["ABL", "MOZ"]


def test_long_and_hyphenated_genes_match_case_insensitively():
    assert _gene_mentioned("c-Kit driven leukemia", "KIT", ["c-Kit"]) is True
    assert _gene_mentioned("c11orf95 rearrangement", "C11orf95", []) is True
    assert _gene_mentioned("C11ORF95-RELA fusion", "C11orf95-RELA", []) is True
    assert _gene_mentioned("NOTCH1 driven leukemia", "Notch1", []) is True


@pytest.mark.parametrize(
    "gene,aliases,text",
    [
        ("REST", [], "The rest of the cohort recovered."),
        ("DDIT3", ["CHOP"], "We chop the tissue before staining."),
        ("TNFSF14", ["LIGHT"], "In light of these results."),
        ("DDIT3", ["STOP"], "A stop codon was introduced."),
    ],
)
def test_all_letter_gene_terms_match_case_sensitively(gene, aliases, text):
    assert _gene_mentioned(text, gene, aliases) is False


def test_structured_gene_keeps_submitted_case():
    assert extract_subject("Review this annotation.", "c-Kit") == ("c-Kit", False)


def test_free_text_detects_mixed_case_hyphenated_fusion():
    assert extract_subject("Review C11orf95-RELA fusion annotation.") == (
        "C11orf95::RELA",
        True,
    )


async def test_lowercase_gene_word_does_not_count_as_mention(pubmed):
    pubmed["records"] = [_record("41424301", "A receptor study", "The kit was used.")]
    section = await build_citation_check_section(
        "Review KIT annotation. PMID 41424301", gene="KIT"
    )
    assert "mentions `KIT` — NO" in section


async def test_positive_gene_mention_and_tumor(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "TP53 mutations in ovarian cancer",
            "TP53 was associated with ovarian cancer outcomes.",
        )
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "## Automated citation check" in section
    assert (
        "PMID 41424301: mentions `TP53` — yes; ovarian cancer: found: ovarian cancer"
        in section
    )
    assert "(abstract checked; full text not checked)" in section
    assert "reports mentions, not diagnoses" in section
    assert pubmed["pmids"] == ["41424301"]
    assert pubmed["filter_retracted"] is False


async def test_negative_fusion_partner_is_called_out(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "GREB1 rearrangements in ovarian cancer",
            "GREB1 fusion partners were characterized in ovarian cancer.",
        )
    ]
    section = await build_citation_check_section(
        "Review GREB1::BCL9L fusion annotation for ovarian cancer. PMID 41424301"
    )
    assert "mentions `GREB1` — yes; `BCL9L` — NO" in section
    assert "ovarian cancer: found: ovarian cancer" in section
    assert "(abstract checked; full text not checked)" in section


@pytest.mark.parametrize("spelling", ["GREB1::BCL9L", "GREB1-BCL9L", "GREB1/BCL9L"])
async def test_fusion_spellings_match_both_partners(pubmed, spelling):
    pubmed["records"] = [
        _record("41424301", f"The {spelling} fusion drives ovarian cancer", "")
    ]
    section = await build_citation_check_section(
        f"Review {spelling} fusion annotation for ovarian cancer. PMID 41424301"
    )
    assert "mentions `GREB1` — yes; `BCL9L` — yes" in section


async def test_tumor_alias_expansion_matches_full_name(pubmed):
    pubmed["records"] = [
        _record("41424301", "TP53 in non-small cell lung cancer", "TP53 cohort.")
    ]
    section = await build_citation_check_section(
        "Review TP53 in NSCLC. PMID 41424301", gene="TP53"
    )
    assert "NSCLC: found: non-small cell lung cancer" in section


async def test_breast_cancer_abstract_does_not_match_nsclc(pubmed):
    pubmed["records"] = [
        _record("41424301", "Single cell sequencing of breast cancer", "TP53 cohort.")
    ]
    section = await build_citation_check_section(
        "Review TP53 in NSCLC. PMID 41424301", gene="TP53"
    )
    assert "NSCLC: not found" in section
    assert "searched: NSCLC, non-small cell lung cancer" in section


async def test_aml_abstract_does_not_match_all(pubmed):
    pubmed["records"] = [_record("41424301", "TP53 in AML", "TP53 cohort.")]
    section = await build_citation_check_section(
        "Review TP53 in ALL. PMID 41424301", gene="TP53"
    )
    assert "ALL: not found" in section


async def test_two_letter_tumor_abbreviations_are_not_searched_in_abstracts(pubmed):
    pubmed["records"] = [
        _record("41424301", "TP53 in GC B cells", "GC-MS was used.")
    ]
    section = await build_citation_check_section(
        "Review TP53 in GC. PMID 41424301", gene="TP53"
    )
    assert "GC: not found" in section

    pubmed["records"] = [
        _record("41424301", "TP53 in UC (ulcerative colitis)", "TP53 cohort.")
    ]
    section = await build_citation_check_section(
        "Review TP53 in UC. PMID 41424301", gene="TP53"
    )
    assert "UC: not found" in section


async def test_utrosct_does_not_count_as_ovarian_cancer(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "Uterine tumor resembling ovarian sex cord tumor (UTROSCT)",
            "TP53 cohort.",
        )
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "ovarian cancer: not found" in section


async def test_utrosct_case_121_end_to_end(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "Uterine tumor resembling ovarian sex cord tumor (UTROSCT)",
            "GREB1 rearrangements were identified in this uterine tumor.",
        )
    ]
    section = await build_citation_check_section(
        "Review GREB1::BCL9L fusion annotation for ovarian cancer. PMID 41424301"
    )
    assert "mentions `GREB1` — yes; `BCL9L` — NO" in section
    assert "ovarian cancer: not found" in section


async def test_colorectal_metastasis_does_not_count_as_ovarian(pubmed):
    pubmed["records"] = [
        _record("41424301", "Ovarian metastasis from colorectal carcinoma", "")
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "ovarian cancer: not found" in section


async def test_ovarian_function_sentence_does_not_count_as_ovarian(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "Ovarian function was preserved. The uterine tumor was resected.",
            "",
        )
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "ovarian cancer: not found" in section


async def test_sclc_abstract_does_not_match_nsclc(pubmed):
    pubmed["records"] = [
        _record("41424301", "TP53 in small cell lung cancer", "TP53 cohort.")
    ]
    section = await build_citation_check_section(
        "Review TP53 in NSCLC. PMID 41424301", gene="TP53"
    )
    assert "NSCLC: not found" in section


async def test_nsclc_abstract_does_not_match_sclc(pubmed):
    pubmed["records"] = [
        _record("41424301", "TP53 in non-small cell lung cancer", "TP53 cohort.")
    ]
    section = await build_citation_check_section(
        "Review TP53 in SCLC. PMID 41424301", gene="TP53"
    )
    assert "SCLC: not found" in section


async def test_no_tumor_detected_is_stated(pubmed):
    pubmed["records"] = [_record("41424301", "TP53 study", "")]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "Tumor type not detected in feedback; tumor mention was not checked." in section


async def test_alias_mention_counts_as_positive(pubmed, monkeypatch):
    pubmed["records"] = [_record("41424301", "P53 and ovarian cancer", "")]

    async def aliases(five, three):
        return ["P53"], []

    monkeypatch.setattr(feedback_citations, "_resolve_aliases", aliases)
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "mentions `TP53` — yes" in section


async def test_alias_resolution_wiring_uses_literature_helpers(pubmed, monkeypatch):
    pubmed["records"] = [_record("41424301", "P53 and ovarian cancer", "")]

    async def hgnc_aliases(gene):
        return ["P53"]

    monkeypatch.setattr(feedback_citations, "_resolve_aliases", _REAL_RESOLVE_ALIASES)
    monkeypatch.setattr(
        feedback_citations.literature, "_resolve_one_partner_aliases", hgnc_aliases
    )
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "mentions `TP53` — yes" in section


async def test_retracted_citation_is_marked(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "TP53 study",
            "",
            publication_types=["Retracted Publication"],
        )
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "PMID 41424301: RETRACTED — retracted publication" in section
    assert "abstract not retrieved" not in section
    assert pubmed["filter_retracted"] is False


async def test_retraction_notice_is_labelled_distinctly(pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "Retraction: TP53 study",
            "",
            publication_types=["Retraction of Publication"],
        )
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "PMID 41424301: RETRACTED — retraction notice" in section


async def test_retraction_title_is_detected(pubmed):
    pubmed["records"] = [_record("41424301", "Retraction: TP53 study", "")]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "PMID 41424301: RETRACTED — retraction notice" in section


async def test_bibliometric_retractions_title_is_not_flagged(pubmed):
    pubmed["records"] = [
        _record("41424301", "Retractions in oncology: a bibliometric study", "")
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "RETRACTED" not in section


async def test_ovarian_carcinoma_is_an_explicit_ovarian_cancer_alias(pubmed):
    pubmed["records"] = [_record("41424301", "TP53 in ovarian carcinoma", "")]
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "ovarian cancer: found: ovarian carcinoma" in section


async def test_untrusted_subject_cannot_break_code_span(pubmed):
    pubmed["records"] = [_record("41424301", "TP53 study", "")]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="T`P\n53"
    )
    assert "mentions `TP53` — NO" in section
    assert "T`P" not in section


async def test_missing_abstract_is_unavailable_per_pmid(pubmed):
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "PMID 41424301: check unavailable (abstract not retrieved)" in section


async def test_cap_overflow_is_reported(pubmed, monkeypatch):
    monkeypatch.setattr(feedback_citations.settings, "feedback_citation_max_pmids", 1)
    pubmed["records"] = [
        _record("11111111", "TP53 study", ""),
        _record("22222222", "TP53 study", ""),
    ]
    section = await build_citation_check_section(
        "Review TP53 annotation. PMIDs: 11111111, 22222222", gene="TP53"
    )
    assert "1 additional cited PMID(s) were not checked (cap 1)" in section
    assert "PMID 11111111" in section
    assert "PMID 22222222" not in section


async def test_pubmed_failure_does_not_raise(pubmed, monkeypatch):
    async def failing_efetch(pmids, client, *, filter_retracted=True):
        raise RuntimeError("PubMed down")

    monkeypatch.setattr(feedback_citations.literature, "_efetch", failing_efetch)
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "check unavailable (PubMed request failed)" in section


async def test_timeout_does_not_raise(pubmed, monkeypatch):
    async def slow_efetch(pmids, client, *, filter_retracted=True):
        await asyncio.sleep(0.5)
        return []

    monkeypatch.setattr(feedback_citations.literature, "_efetch", slow_efetch)
    monkeypatch.setattr(feedback_citations.settings, "feedback_citation_check_timeout_seconds", 0.01)
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "check unavailable (PubMed request timed out)" in section


async def test_no_pmids_means_no_section(pubmed):
    assert await build_citation_check_section("Review TP53 annotation.", gene="TP53") is None


class _FakeRunStore:
    async def save_feedback(self, **kwargs):
        return None


@pytest.fixture
def feedback_client(monkeypatch):
    monkeypatch.setattr(main, "_feedback_requests", {})
    monkeypatch.setattr(main.settings, "feedback_rate_limit_per_hour", 100)
    monkeypatch.setattr(main.settings, "feedback_issue_creation_enabled", True)
    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "")
    main.app.state.run_store = _FakeRunStore()

    async def fake_draft(**kwargs):
        return {
            "title": "Review GREB1::BCL9L fusion annotation",
            "problem_summary": "The cited paper does not support the fusion.",
            "suggested_solution": "Re-review the citation.",
            "acceptance_criteria": ["Citation is corrected."],
        }

    monkeypatch.setattr(main, "complete_with_tool", fake_draft)
    return TestClient(main.app)


def test_issue_body_gets_citation_section(feedback_client, pubmed):
    pubmed["records"] = [
        _record(
            "41424301",
            "GREB1 rearrangements in ovarian cancer",
            "No second partner mention here.",
        )
    ]
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "Review GREB1::BCL9L fusion annotation for ovarian cancer. PMID 41424301",
        },
    )
    assert response.status_code == 201
    body = response.json()["issue_body"]
    assert body.endswith("(abstract checked; full text not checked)")
    assert "## Automated citation check" in body
    assert "`BCL9L` — NO" in body


def test_citation_failure_does_not_block_submission(feedback_client, monkeypatch):
    async def failing_efetch(pmids, client, *, filter_retracted=True):
        raise RuntimeError("PubMed down")

    monkeypatch.setattr(feedback_citations.literature, "_get_ncbi_client", lambda: object())
    monkeypatch.setattr(feedback_citations.literature, "_efetch", failing_efetch)
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "Review GREB1::BCL9L fusion annotation. PMID 41424301",
        },
    )
    assert response.status_code == 201
    assert "check unavailable" in response.json()["issue_body"]


def test_unexpected_check_error_does_not_block_submission(feedback_client, monkeypatch):
    async def broken_check(*args, **kwargs):
        raise RuntimeError("bug in check")

    monkeypatch.setattr(main, "build_citation_check_section", broken_check)
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "Review GREB1::BCL9L fusion annotation. PMID 41424301",
        },
    )
    assert response.status_code == 201
    assert "check unavailable (citation check failed)" in response.json()["issue_body"]


def test_structured_pmids_are_checked(feedback_client, pubmed):
    pubmed["records"] = [_record("41424301", "TP53 in ovarian cancer", "")]
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "gene": "TP53",
            "pmids": ["41424301"],
        },
    )
    assert response.status_code == 201
    assert "PMID 41424301: mentions `TP53` — yes" in response.json()["issue_body"]


def test_structured_pmid_with_label_prefix_is_parsed(feedback_client, pubmed):
    pubmed["records"] = [_record("41424301", "TP53 in ovarian cancer", "")]
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "gene": "TP53",
            "pmids": ["PMID 41424301"],
        },
    )
    assert response.status_code == 201
    assert "PMID 41424301: mentions `TP53` — yes" in response.json()["issue_body"]


def test_short_and_ten_digit_structured_pmids_are_accepted(feedback_client, pubmed):
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "gene": "TP53",
            "pmids": ["12345", "1234567890"],
        },
    )
    assert response.status_code == 201
    body = response.json()["issue_body"]
    assert "PMID 12345: check unavailable (abstract not retrieved)" in body
    assert "PMID 1234567890: check unavailable (abstract not retrieved)" in body


def test_unparseable_structured_pmid_is_reported(feedback_client):
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "pmids": ["abc"],
        },
    )
    assert response.status_code == 201
    body = response.json()["issue_body"]
    assert "No parseable PMIDs were cited; citation check skipped." in body
    assert "Structured PMID value `abc` not checked: no PMID found." in body


def test_pmcid_structured_value_is_reported_as_unsupported(feedback_client):
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "pmids": ["PMC1234567"],
        },
    )
    assert response.status_code == 201
    body = response.json()["issue_body"]
    assert "Structured PMID value `PMC1234567` not checked: PMCID not supported." in body


def test_doi_structured_value_is_reported_as_unsupported(feedback_client):
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "pmids": ["doi:10.1038/s41586-023-06123-4"],
        },
    )
    assert response.status_code == 201
    assert "DOI not supported" in response.json()["issue_body"]


def test_structured_pmid_list_is_capped_at_twenty(feedback_client):
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "pmids": [str(10000000 + index) for index in range(21)],
        },
    )
    assert response.status_code == 422


def test_structured_pmid_entry_is_capped_at_64_characters(feedback_client):
    response = feedback_client.post(
        "/v1/feedback",
        json={
            "category": "gene_annotation_issue",
            "message": "The cited paper does not support this annotation.",
            "pmids": ["9" * 65],
        },
    )
    assert response.status_code == 422


class _FakeGithubResponse:
    def __init__(self, status_code=201):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return {"html_url": "https://github.com/example/issues/121"}


def _fake_github_client(posts, statuses):
    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            posts.append(json)
            status = statuses.pop(0) if statuses else 201
            return _FakeGithubResponse(status)

    return FakeAsyncClient


@pytest.mark.parametrize("labels", [["feedback", "needs-triage"], ["triage"]])
def test_create_github_issue_passes_configured_labels(monkeypatch, labels):
    posts = []
    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "token")
    monkeypatch.setattr(main.settings, "feedback_issue_labels", labels)
    monkeypatch.setattr(
        main, "httpx", type("_httpx", (), {"AsyncClient": _fake_github_client(posts, [])})
    )
    url = asyncio.run(main._create_github_issue("Feedback: title", "body"))
    assert url == "https://github.com/example/issues/121"
    assert posts == [{"title": "Feedback: title", "body": "body", "labels": labels}]


def test_create_github_issue_retries_without_labels_on_422(monkeypatch):
    posts = []
    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "token")
    monkeypatch.setattr(main.settings, "feedback_issue_labels", ["feedback", "needs-triage"])
    monkeypatch.setattr(
        main, "httpx", type("_httpx", (), {"AsyncClient": _fake_github_client(posts, [422])})
    )
    url = asyncio.run(main._create_github_issue("Feedback: title", "body"))
    assert url == "https://github.com/example/issues/121"
    assert len(posts) == 2
    assert posts[0]["labels"] == ["feedback", "needs-triage"]
    assert posts[1] == {"title": "Feedback: title", "body": "body"}


def test_create_github_issue_does_not_retry_without_labels_on_other_errors(monkeypatch):
    posts = []
    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "token")
    monkeypatch.setattr(main.settings, "feedback_issue_labels", ["feedback", "needs-triage"])
    monkeypatch.setattr(
        main, "httpx", type("_httpx", (), {"AsyncClient": _fake_github_client(posts, [500])})
    )
    url = asyncio.run(main._create_github_issue("Feedback: title", "body"))
    assert url is None
    assert len(posts) == 1


def test_create_github_issue_returns_none_when_retry_also_fails(monkeypatch):
    posts = []
    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "token")
    monkeypatch.setattr(main.settings, "feedback_issue_labels", ["feedback", "needs-triage"])
    monkeypatch.setattr(
        main,
        "httpx",
        type("_httpx", (), {"AsyncClient": _fake_github_client(posts, [422, 422])}),
    )
    url = asyncio.run(main._create_github_issue("Feedback: title", "body"))
    assert url is None
    assert len(posts) == 2
