"""Tests for citation precision controls."""

from src.models.schema import LiteratureRecord
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline.synthesis import _verify_citations


async def test_selection_preserves_model_relevance_order(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"selected_pmids": ["333", "111", "333", "999", "222"]}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid="111", title="First", abstract="Abstract 1"),
        LiteratureRecord(pmid="222", title="Second", abstract="Abstract 2"),
        LiteratureRecord(pmid="333", title="Third", abstract="Abstract 3"),
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert [record.pmid for record in selected] == ["333", "111"]


async def test_selection_can_abstain_when_no_papers_are_relevant(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"selected_pmids": []}

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fake_complete_with_tool)

    records = [
        LiteratureRecord(pmid=str(i), title=f"Paper {i}", abstract=f"Abstract {i}")
        for i in range(10)
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert selected == []


def test_verify_citations_deduplicates_rejects_unretrieved_and_caps():
    verified = _verify_citations(
        ["111", "222", "222", "999", "333"],
        {"111", "222", "333", "444"},
        max_citations=2,
    )

    assert verified == ["111", "222"]
