import asyncio

import pytest
from fastapi.testclient import TestClient

from src import main
from src.models.schema import LiteratureRecord
from src.pipeline import feedback_citations
from src.pipeline.feedback_citations import (
    build_citation_check_section,
    extract_subject,
    extract_tumor_type,
    parse_cited_pmids,
)


def _record(pmid: str, title: str, abstract: str = "") -> LiteratureRecord:
    return LiteratureRecord(pmid=pmid, title=title, abstract=abstract, journal="Test")


@pytest.fixture
def pubmed(monkeypatch):
    monkeypatch.setattr(
        feedback_citations.literature, "_get_ncbi_client", lambda: object()
    )
    calls = {}

    async def fake_efetch(pmids, client):
        calls["pmids"] = list(pmids)
        calls["client"] = client
        return [calls["record"]] if calls.get("record") is not None else []

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


def test_parse_pmids_from_structured_field_and_dedupe():
    text = "The cited paper is PMID 41424301."
    assert parse_cited_pmids(text, ["PMID 99999999", "41424301"]) == [
        "99999999",
        "41424301",
    ]


def test_parse_pmids_respects_cap(monkeypatch):
    monkeypatch.setattr(feedback_citations.settings, "feedback_citation_max_pmids", 2)
    text = "PMID 11111111 PMID 22222222 PMID 33333333"
    assert parse_cited_pmids(text) == ["11111111", "22222222"]


def test_parse_pmids_ignores_non_pmid_numbers():
    assert parse_cited_pmids("Run 123 on version 2.5 with sample 1234567890") == []


@pytest.mark.parametrize(
    "message,expected,is_fusion",
    [
        ("Review GREB1::BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review GREB1-BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review GREB1/BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review GREB1 -- BCL9L fusion annotation.", "GREB1::BCL9L", True),
        ("Review the gene TP53 annotation.", "TP53", False),
    ],
)
def test_extract_subject(message, expected, is_fusion):
    subject, fusion = extract_subject(message)
    assert (subject, fusion) == (expected, is_fusion)


def test_extract_subject_prefers_structured_gene():
    assert extract_subject("Review GREB1::BCL9L fusion.", "TP53") == ("TP53", False)


def test_hyphenated_gene_symbol_is_not_mistaken_for_a_fusion():
    assert extract_subject("Review HLA-A annotation.", "HLA-A") == ("HLA-A", False)
    assert extract_subject("Review BCR-ABL1 fusion.", "BCR-ABL1") == ("BCR-ABL1", True)


@pytest.mark.parametrize(
    "message,tumor",
    [
        ("Review GREB1::BCL9L fusion annotation for ovarian cancer.", "ovarian cancer"),
        ("This annotation is missing melanoma context.", "melanoma"),
        ("Reported in a patient with pancreatic cancer and liver metastases.", "pancreatic cancer"),
        ("Please fix the export dropdown.", None),
    ],
)
def test_extract_tumor_type(message, tumor):
    assert extract_tumor_type(message) == tumor


async def test_positive_gene_mention_and_tumor(pubmed):
    pubmed["record"] = _record(
        "41424301",
        "TP53 mutations in ovarian cancer",
        "TP53 was associated with ovarian cancer outcomes.",
    )
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "## Automated citation check" in section
    assert "PMID 41424301: mentions TP53 — yes; ovarian cancer — yes" in section
    assert "(abstract checked; full text not checked)" in section
    assert pubmed["pmids"] == ["41424301"]


async def test_negative_fusion_partner_is_called_out(pubmed):
    pubmed["record"] = _record(
        "41424301",
        "GREB1 rearrangements in ovarian cancer",
        "GREB1 fusion partners were characterized in ovarian cancer.",
    )
    section = await build_citation_check_section(
        "Review GREB1::BCL9L fusion annotation for ovarian cancer. PMID 41424301"
    )
    assert "mentions GREB1 — yes; BCL9L — NO" in section
    assert "ovarian cancer — yes" in section
    assert "(abstract checked; full text not checked)" in section


@pytest.mark.parametrize("spelling", ["GREB1::BCL9L", "GREB1-BCL9L", "GREB1/BCL9L"])
async def test_fusion_spellings_match_both_partners(pubmed, spelling):
    pubmed["record"] = _record(
        "41424301",
        f"The {spelling} fusion drives ovarian cancer",
        "",
    )
    section = await build_citation_check_section(
        f"Review {spelling} fusion annotation for ovarian cancer. PMID 41424301"
    )
    assert "mentions GREB1 — yes; BCL9L — yes" in section


async def test_alias_mention_counts_as_positive(pubmed, monkeypatch):
    pubmed["record"] = _record("41424301", "P53 and ovarian cancer", "")

    async def aliases(five, three):
        return ["P53"], []

    monkeypatch.setattr(feedback_citations, "_resolve_aliases", aliases)
    section = await build_citation_check_section(
        "Review TP53 annotation for ovarian cancer. PMID 41424301", gene="TP53"
    )
    assert "mentions TP53 — yes" in section


async def test_missing_abstract_is_unavailable_per_pmid(pubmed):
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "PMID 41424301: check unavailable (abstract not retrieved)" in section


async def test_pubmed_failure_does_not_raise(pubmed, monkeypatch):
    async def failing_efetch(pmids, client):
        raise RuntimeError("PubMed down")

    monkeypatch.setattr(feedback_citations.literature, "_efetch", failing_efetch)
    section = await build_citation_check_section(
        "Review TP53 annotation. PMID 41424301", gene="TP53"
    )
    assert "check unavailable (PubMed request failed)" in section


async def test_timeout_does_not_raise(pubmed, monkeypatch):
    async def slow_efetch(pmids, client):
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
    pubmed["record"] = _record(
        "41424301",
        "GREB1 rearrangements in ovarian cancer",
        "No second partner mention here.",
    )
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
    assert "BCL9L — NO" in body


def test_citation_failure_does_not_block_submission(feedback_client, monkeypatch):
    async def failing_efetch(pmids, client):
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
    pubmed["record"] = _record("41424301", "TP53 in ovarian cancer", "")
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
    assert "PMID 41424301: mentions TP53 — yes" in response.json()["issue_body"]


def test_create_github_issue_passes_configured_labels(monkeypatch):
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"html_url": "https://github.com/example/issues/121"}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "token")
    monkeypatch.setattr(main.settings, "feedback_issue_labels", ["feedback", "needs-triage"])
    monkeypatch.setattr(
        main, "httpx", type("_httpx", (), {"AsyncClient": FakeAsyncClient})
    )
    url = asyncio.run(main._create_github_issue("Feedback: title", "body"))
    assert url == "https://github.com/example/issues/121"
    assert seen["json"] == {
        "title": "Feedback: title",
        "body": "body",
        "labels": ["feedback", "needs-triage"],
    }


def test_create_github_issue_label_names_are_configurable(monkeypatch):
    seen = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"html_url": "https://github.com/example/issues/122"}

    class FakeAsyncClient:
        def __init__(self, *, timeout):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            seen["json"] = json
            return FakeResponse()

    monkeypatch.setattr(main.settings, "oncokbdev_private_access_token", "token")
    monkeypatch.setattr(main.settings, "feedback_issue_labels", ["triage"])
    monkeypatch.setattr(
        main, "httpx", type("_httpx", (), {"AsyncClient": FakeAsyncClient})
    )
    asyncio.run(main._create_github_issue("Feedback: title", "body"))
    assert seen["json"]["labels"] == ["triage"]
