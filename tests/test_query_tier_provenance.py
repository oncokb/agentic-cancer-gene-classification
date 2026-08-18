from __future__ import annotations

import httpx

from src.pipeline import literature
from src.pipeline.literature import _efetch, _parse_pub_year, _tier1_retrieve, _tier2_agentic_retrieve
from src.models.schema import LiteratureRecord

import xml.etree.ElementTree as ET


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


# ---------------------------------------------------------------------------
# Publication year parsing
# ---------------------------------------------------------------------------

def test_parse_pub_year_from_year_element():
    el = ET.fromstring("<PubDate><Year>2021</Year></PubDate>")
    assert _parse_pub_year(el) == 2021


def test_parse_pub_year_falls_back_to_medline_date():
    el = ET.fromstring("<PubDate><MedlineDate>2019 Jan-Feb</MedlineDate></PubDate>")
    assert _parse_pub_year(el) == 2019


def test_parse_pub_year_returns_none_when_absent():
    el = ET.fromstring("<PubDate></PubDate>")
    assert _parse_pub_year(el) is None
    assert _parse_pub_year(None) is None


async def test_efetch_populates_publication_year(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)
    xml = """\
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>1</PMID>
      <Article>
        <ArticleTitle>ALK fusion paper</ArticleTitle>
        <Abstract><AbstractText>ALK fusion evidence.</AbstractText></Abstract>
        <Journal><JournalIssue><PubDate><Year>2022</Year></PubDate></JournalIssue></Journal>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <MedlineJournalInfo><MedlineTA>Cancer Res</MedlineTA></MedlineJournalInfo>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await _efetch(["1"], client)

    assert records[0].publication_year == 2022


# ---------------------------------------------------------------------------
# Tier 1 query-family provenance tagging
# ---------------------------------------------------------------------------

async def test_tier1_tags_pmid_with_every_matching_query_family(monkeypatch):
    """A PMID returned by both the MeSH query and the free-text fallback query
    should end up tagged with both families (max precision wins downstream)."""
    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature.settings, "pubmed_staged_retrieval", False)

    async def fake_esearch(query, max_results, client):
        if "[Gene Name]" in query:
            return ["1"]
        if "(cancer OR tumor OR oncology OR carcinoma)" in query:
            return ["1", "2"]
        return []

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(pmid=pmid, title=f"Title {pmid}", abstract="Abstract.")
            for pmid in pmids
        ]

    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)

    records = await _tier1_retrieve("ALK", fusions=[], tumor_type=None)
    by_pmid = {r.pmid: r for r in records}

    assert set(by_pmid["1"].matched_query_tiers) >= {"mesh_gene_name", "free_text"}
    assert set(by_pmid["2"].matched_query_tiers) == {"free_text"}


async def test_tier1_staged_retrieval_also_tags_provenance(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature.settings, "pubmed_staged_retrieval", True)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 1)
    monkeypatch.setattr(literature.settings, "max_papers_for_synthesis", 1)

    async def fake_esearch(query, max_results, client):
        if "[Gene Name]" in query:
            return ["1"]
        return []

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(pmid=pmid, title=f"Title {pmid}", abstract="Abstract.")
            for pmid in pmids
        ]

    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)

    records = await _tier1_retrieve("ALK", fusions=[], tumor_type=None)
    assert records[0].matched_query_tiers == ["mesh_gene_name"]


# ---------------------------------------------------------------------------
# Tier 2 agentic provenance tagging
# ---------------------------------------------------------------------------

async def test_tier2_agentic_tags_new_records_uniformly(monkeypatch):
    async def fake_search_and_fetch(query, max_results, client, already_seen):
        return [LiteratureRecord(pmid="2", title="New paper", abstract="Found via agentic search.")]

    monkeypatch.setattr(literature, "_search_and_fetch", fake_search_and_fetch)

    class _FakeToolUse:
        type = "tool_use"
        id = "call_1"
        name = "search_pubmed"
        input = {"query": "ALK fusion", "max_results": 5}

    class _FakeDone:
        type = "tool_use"
        id = "call_2"
        name = "done"
        input = {}

    class _FakeResponse:
        def __init__(self, content, stop_reason):
            self.content = content
            self.stop_reason = stop_reason

    call_count = {"n": 0}

    class _FakeMessages:
        async def create(self, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _FakeResponse([_FakeToolUse()], "tool_use")
            return _FakeResponse([_FakeDone()], "tool_use")

    class _FakeClient:
        messages = _FakeMessages()

    monkeypatch.setattr(literature, "make_async_sdk_client", lambda: _FakeClient())

    initial = [LiteratureRecord(pmid="1", title="Tier1 paper", abstract="From tier1.", matched_query_tiers=["mesh_gene_name"])]
    records = await _tier2_agentic_retrieve("ALK", ["ALK::EML4"], initial, tumor_type=None)
    by_pmid = {r.pmid: r for r in records}

    assert by_pmid["1"].matched_query_tiers == ["mesh_gene_name"]  # untouched
    assert by_pmid["2"].matched_query_tiers == ["tier2_agentic"]
