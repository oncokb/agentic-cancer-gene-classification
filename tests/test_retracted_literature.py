from __future__ import annotations

import httpx

from src.models.schema import LiteratureRecord
from src.pipeline import literature
from src.pipeline.literature import (
    _efetch,
    _esearch,
    _exclude_retracted_query,
    _filter_retracted_records,
    find_retracted_pmids,
)
from src.pipeline.synthesis import _postprocess_synthesis_output


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


def test_filter_retracted_records_uses_publication_type_and_title_prefix():
    records = [
        LiteratureRecord(
            pmid="1",
            title="[Retracted] BRWD1 cancer study",
            abstract="Retracted abstract.",
            publication_types=["Journal Article"],
        ),
        LiteratureRecord(
            pmid="2",
            title="BRWD1 cancer study",
            abstract="Retraction notice.",
            publication_types=["Retraction of Publication"],
        ),
        LiteratureRecord(
            pmid="3",
            title="BRWD1 cancer biology",
            abstract="Supported abstract.",
            publication_types=["Journal Article"],
        ),
        LiteratureRecord(
            pmid="4",
            title="BRWD1 cancer biology",
            abstract="Supported abstract.",
            publication_types=["Journal Article"],
            pubmed_comment_ref_types=["RetractionIn"],
            pubmed_comment_pmids=["5"],
        ),
    ]

    filtered = _filter_retracted_records(records)

    assert [record.pmid for record in filtered] == ["3"]


def test_exclude_retracted_query_adds_pubmed_publication_type_filter():
    query = _exclude_retracted_query('"BRWD1" AND cancer')

    assert '"BRWD1" AND cancer' in query
    assert '"Retracted Publication"[Publication Type]' in query
    assert '"Retraction of Publication"[Publication Type]' in query


async def test_esearch_sends_retracted_publication_exclusion(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["term"] = request.url.params["term"]
        return httpx.Response(200, json={"esearchresult": {"idlist": ["1"]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pmids = await _esearch('"BRWD1" AND cancer', 10, client)

    assert pmids == ["1"]
    assert '"Retracted Publication"[Publication Type]' in seen["term"]
    assert '"Retraction of Publication"[Publication Type]' in seen["term"]


async def test_efetch_filters_retracted_pubmed_records(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)
    xml = """\
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>1</PMID>
      <Article>
        <ArticleTitle>Retracted BRWD1 paper</ArticleTitle>
        <Abstract><AbstractText>Retracted abstract.</AbstractText></Abstract>
        <Journal><ISOAbbreviation>J Clin Oncol</ISOAbbreviation></Journal>
        <PublicationTypeList>
          <PublicationType>Retracted Publication</PublicationType>
        </PublicationTypeList>
      </Article>
      <MedlineJournalInfo><MedlineTA>J Clin Oncol</MedlineTA></MedlineJournalInfo>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>2</PMID>
      <Article>
        <ArticleTitle>BRWD1 cancer paper</ArticleTitle>
        <Abstract><AbstractText>Supported abstract.</AbstractText></Abstract>
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
        records = await _efetch(["1", "2"], client)

    assert [record.pmid for record in records] == ["2"]


async def test_efetch_filters_pubmed_comments_retraction_metadata(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)
    xml = """\
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>1</PMID>
      <Article>
        <ArticleTitle>BRWD1 cancer paper later retracted</ArticleTitle>
        <Abstract><AbstractText>Retracted abstract.</AbstractText></Abstract>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">
          <PMID>3</PMID>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>2</PMID>
      <Article>
        <ArticleTitle>BRWD1 cancer paper</ArticleTitle>
        <Abstract><AbstractText>Supported abstract.</AbstractText></Abstract>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await _efetch(["1", "2"], client)

    assert [record.pmid for record in records] == ["2"]


async def test_find_retracted_pmids_detects_metadata_even_without_abstract(monkeypatch):
    monkeypatch.setattr(literature, "cached_call", _uncached)
    xml = """\
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>1</PMID>
      <Article>
        <ArticleTitle>BRWD1 cancer paper later retracted</ArticleTitle>
        <PublicationTypeList>
          <PublicationType>Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
      <CommentsCorrectionsList>
        <CommentsCorrections RefType="RetractionIn">
          <PMID>3</PMID>
        </CommentsCorrections>
      </CommentsCorrectionsList>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>
"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=xml)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        original_efetch = literature._efetch

        async def fake_efetch(pmids, _client, *, filter_retracted=True):
            return await original_efetch(pmids, client, filter_retracted=filter_retracted)

        monkeypatch.setattr(literature, "_efetch", fake_efetch)
        retracted = await find_retracted_pmids(["1"])

    assert retracted == {"1"}


def test_synthesis_postprocess_drops_retracted_citation():
    records = [
        LiteratureRecord(
            pmid="1",
            title="Retracted: GENE cancer",
            abstract="GENE cancer evidence.",
            publication_types=["Retracted Publication"],
        ),
        LiteratureRecord(
            pmid="2",
            title="GENE cancer",
            abstract="GENE cancer evidence.",
            publication_types=["Journal Article"],
        ),
    ]
    tool_input = {"citations": ["1", "2"]}

    postprocessed = _postprocess_synthesis_output(
        gene="GENE",
        tool_input=tool_input,
        records=records,
        gene_identity=None,
    )

    assert postprocessed["citations"] == ["2"]
