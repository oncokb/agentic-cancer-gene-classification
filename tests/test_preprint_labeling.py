"""Preprint labeling: PubMed-indexed bioRxiv/medRxiv preprints (PublicationType
"Preprint") are kept, but labeled end to end — parse, ranking, the selection and
synthesis prompts, and evidence cards — rather than silently treated as
peer-reviewed original research."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from src.models.schema import EvidenceCard, GeneAnnotation, LiteratureRecord
from src.pipeline import literature
from src.pipeline.literature import (
    PREPRINT_MARKER,
    _efetch,
    _fusion_partner_evidence_cards,
    _build_fusion_evidence_cards,
    _pubtype_score,
    is_preprint_publication,
)
from src.pipeline.orchestrator import _cached_annotation_for_request
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import (
    CORE_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    _build_evidence_cards,
    _build_user_prompt,
)

# Real-shaped PubMed efetch record for a bioRxiv preprint (PubMed-not-MEDLINE,
# MedlineTA "bioRxiv", PublicationType "Preprint" with its MeSH UI), next to an
# ordinary peer-reviewed journal article.
_PREPRINT_EFETCH_XML = """\
<?xml version="1.0" ?>
<!DOCTYPE PubmedArticleSet PUBLIC "-//NLM//DTD PubMedArticle, 1st January 2024//EN" "https://dtd.nlm.nih.gov/ncbi/pubmed/out/pubmed_240101.dtd">
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation Status="PubMed-not-MEDLINE" Owner="NLM">
      <PMID Version="1">36711734</PMID>
      <DateRevised><Year>2023</Year><Month>02</Month><Day>01</Day></DateRevised>
      <Article PubModel="Electronic">
        <Journal>
          <ISSN IssnType="Electronic">2692-8205</ISSN>
          <JournalIssue CitedMedium="Internet">
            <PubDate><Year>2023</Year><Month>Jan</Month><Day>20</Day></PubDate>
          </JournalIssue>
          <Title>bioRxiv : the preprint server for biology</Title>
          <ISOAbbreviation>bioRxiv</ISOAbbreviation>
        </Journal>
        <ArticleTitle>BRWD1 loss drives oncogenic chromatin remodeling in lung adenocarcinoma.</ArticleTitle>
        <ELocationID EIdType="doi" ValidYN="Y">10.1101/2023.01.19.524000</ELocationID>
        <Abstract>
          <AbstractText>BRWD1 knockout accelerated tumor growth in xenograft models.</AbstractText>
        </Abstract>
        <Language>eng</Language>
        <PublicationTypeList>
          <PublicationType UI="D000076942">Preprint</PublicationType>
        </PublicationTypeList>
        <ArticleDate DateType="Electronic"><Year>2023</Year><Month>01</Month><Day>20</Day></ArticleDate>
      </Article>
      <MedlineJournalInfo>
        <Country>United States</Country>
        <MedlineTA>bioRxiv</MedlineTA>
        <NlmUniqueID>101680187</NlmUniqueID>
      </MedlineJournalInfo>
    </MedlineCitation>
    <PubmedData>
      <PublicationStatus>epublish</PublicationStatus>
      <ArticleIdList>
        <ArticleId IdType="pubmed">36711734</ArticleId>
        <ArticleId IdType="doi">10.1101/2023.01.19.524000</ArticleId>
      </ArticleIdList>
    </PubmedData>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation Status="MEDLINE" Owner="NLM">
      <PMID Version="1">30000001</PMID>
      <Article PubModel="Print">
        <Journal>
          <JournalIssue CitedMedium="Internet">
            <PubDate><Year>2019</Year></PubDate>
          </JournalIssue>
          <ISOAbbreviation>Cancer Res</ISOAbbreviation>
        </Journal>
        <ArticleTitle>BRWD1 in lung cancer.</ArticleTitle>
        <Abstract><AbstractText>Peer-reviewed BRWD1 abstract.</AbstractText></Abstract>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <MedlineJournalInfo><MedlineTA>Cancer Res</MedlineTA></MedlineJournalInfo>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


def _preprint(pmid: str = "36711734", **kwargs) -> LiteratureRecord:
    return LiteratureRecord(
        pmid=pmid,
        title="BRWD1 preprint",
        abstract="BRWD1 knockout accelerated tumor growth.",
        journal="bioRxiv",
        publication_types=["Preprint"],
        **kwargs,
    )


def _peer_reviewed(pmid: str = "30000001", **kwargs) -> LiteratureRecord:
    return LiteratureRecord(
        pmid=pmid,
        title="BRWD1 in lung cancer",
        abstract="Peer-reviewed BRWD1 abstract.",
        journal="Cancer Res",
        publication_types=["Journal Article"],
        **kwargs,
    )


# --- Data: parse ----------------------------------------------------------


async def test_efetch_parses_pubmed_preprint_publication_type_without_dropping_it(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_PREPRINT_EFETCH_XML)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await _efetch(["36711734", "30000001"], client)

    by_pmid = {record.pmid: record for record in records}
    assert set(by_pmid) == {"36711734", "30000001"}  # labeled, not excluded
    preprint = by_pmid["36711734"]
    assert preprint.publication_types == ["Preprint"]
    assert preprint.journal == "bioRxiv"
    assert is_preprint_publication(preprint.publication_types)
    assert not is_preprint_publication(by_pmid["30000001"].publication_types)

    cards = _build_evidence_cards(["36711734", "30000001"], records, [])
    assert [(card.pmid, card.is_preprint) for card in cards] == [
        ("36711734", True),
        ("30000001", False),
    ]
    assert cards[0].publication_types == ["Preprint"]


def test_is_preprint_publication_is_case_insensitive_and_exact():
    assert is_preprint_publication(["Journal Article", "preprint"])
    assert is_preprint_publication([" PREPRINT "])
    assert not is_preprint_publication([])
    assert not is_preprint_publication(["Journal Article", "Review"])


# --- Ranking ----------------------------------------------------------------


def test_preprint_pubtype_weight_is_below_peer_reviewed_original_research():
    settings = literature.settings
    assert settings.citation_score_pubtype_preprint_weight < settings.citation_score_pubtype_original_research_weight
    assert settings.context_score_pubtype_preprint_weight < settings.context_score_pubtype_original_research_weight

    assert _pubtype_score(_preprint(), "citation") == settings.citation_score_pubtype_preprint_weight
    assert _pubtype_score(_preprint(), "context") == settings.context_score_pubtype_preprint_weight
    assert _pubtype_score(_preprint(), "citation") < _pubtype_score(_peer_reviewed(), "citation")
    assert _pubtype_score(_preprint(), "context") < _pubtype_score(_peer_reviewed(), "context")


def test_preprint_caps_rather_than_competes_with_study_design_weight():
    settings = literature.settings
    preprinted_trial = LiteratureRecord(
        pmid="1", title="t", abstract="a", publication_types=["Preprint", "Clinical Trial"]
    )
    published_trial = LiteratureRecord(pmid="2", title="t", abstract="a", publication_types=["Clinical Trial"])
    assert _pubtype_score(preprinted_trial, "citation") == settings.citation_score_pubtype_preprint_weight
    assert _pubtype_score(preprinted_trial, "citation") < _pubtype_score(published_trial, "citation")

    # A preprinted editorial already weighs less than the cap; it keeps its own weight.
    preprinted_letter = LiteratureRecord(pmid="3", title="t", abstract="a", publication_types=["Preprint", "Letter"])
    assert _pubtype_score(preprinted_letter, "citation") == settings.citation_score_pubtype_editorial_weight


def test_peer_reviewed_pubtype_weights_unchanged():
    settings = literature.settings
    assert _pubtype_score(_peer_reviewed(), "citation") == settings.citation_score_pubtype_original_research_weight
    assert _pubtype_score(_peer_reviewed(), "context") == settings.context_score_pubtype_original_research_weight


# --- Selection + synthesis prompts ----------------------------------------


async def test_selection_prompt_shows_publication_type_and_preprint_marker(monkeypatch):
    captured: dict = {}

    async def fake_complete_with_tool(**kwargs):
        captured.update(kwargs)
        return {"selected_pmids": ["36711734"]}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [_preprint(), _peer_reviewed(), _peer_reviewed(pmid="30000002")]
    selected = await select_papers_for_synthesis("BRWD1", records, max_papers=2)

    assert [record.pmid for record in selected] == ["36711734"]
    prompt = captured["user"]
    assert f"PMID 36711734 {PREPRINT_MARKER}\nPublication type: Preprint\n" in prompt
    assert "PMID 30000001\nPublication type: Journal Article\n" in prompt
    assert prompt.count(PREPRINT_MARKER) == 1
    assert PREPRINT_MARKER in captured["system"]


def test_synthesis_prompt_marks_preprints_and_instructs_weaker_support():
    prompt = _build_user_prompt(
        gene="BRWD1",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[_preprint(), _peer_reviewed()],
        retrieval_tier=1,
    )
    assert f"PMID: 36711734 [bioRxiv] {PREPRINT_MARKER}" in prompt
    assert "PMID: 30000001 [Cancer Res] ★\n" in prompt
    assert prompt.count(PREPRINT_MARKER) == 1

    for system_prompt in (SYSTEM_PROMPT, CORE_SYSTEM_PROMPT):
        assert PREPRINT_MARKER in system_prompt
        assert "non-peer-reviewed support" in system_prompt
        assert "preprint alone for a strong classification" in " ".join(system_prompt.split())


def test_synthesis_prompt_marks_preprint_on_distilled_pmid_line(monkeypatch):
    from src.models.schema import PMIDEvidenceRecord
    from src.pipeline import synthesis

    monkeypatch.setattr(synthesis.settings, "pmid_distillation_enabled", True)
    prompt = _build_user_prompt(
        gene="BRWD1",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=[_preprint()],
        retrieval_tier=1,
        pmid_evidence={
            "36711734": PMIDEvidenceRecord(
                pmid="36711734", title="t", journal="bioRxiv", distilled_takeaway="BRWD1 loss drives growth."
            )
        },
    )
    assert f"PMID: 36711734 [bioRxiv] {PREPRINT_MARKER} Summary:" in prompt


# --- Evidence cards / API response ----------------------------------------


def test_evidence_cards_carry_preprint_flag_in_api_payload():
    cards = _build_evidence_cards(["36711734"], [_preprint()], [])
    annotation = GeneAnnotation(
        gene="BRWD1", cancer_associated=True, evidence_cards=cards, date_annotated="10/6/26"
    )
    payload = annotation.model_dump(mode="json")
    card = payload["evidence_cards"][0]
    assert card["is_preprint"] is True
    assert card["publication_types"] == ["Preprint"]


def test_fusion_and_partner_evidence_cards_carry_preprint_flag():
    fusion_cards = _build_fusion_evidence_cards("EML4::ALK", [_preprint(), _peer_reviewed()], {})
    assert [card.is_preprint for card in fusion_cards] == [True, False]
    partner_cards = _fusion_partner_evidence_cards([_preprint(), _peer_reviewed()])
    assert [card.is_preprint for card in partner_cards] == [True, False]


def test_cached_annotation_without_preprint_fields_still_loads():
    # Shape of an evidence card persisted before publication_types/is_preprint existed.
    old_payload = {
        "gene": "BRWD1",
        "cancer_associated": True,
        "date_annotated": "1/1/26",
        "evidence_cards": [
            {
                "pmid": "36711734",
                "title": "BRWD1 preprint",
                "journal": "bioRxiv",
                "evidence_type": "preclinical",
                "selected_reason": "Verified PMID selected as preclinical evidence.",
                "quote": None,
                "abstract": None,
            }
        ],
    }
    annotation = _cached_annotation_for_request(
        old_payload,
        fusions=[],
        reason="fresh_medium_evidence_support",
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_pubmed_checked_at=None,
    )
    card = annotation.evidence_cards[0]
    assert card.is_preprint is False
    assert card.publication_types == []
    assert EvidenceCard(pmid="1").is_preprint is False
