from __future__ import annotations

from src.models.schema import LiteratureRecord
from src.pipeline import literature
from src.pipeline.literature import (
    _fusion_evidence_queries,
    retrieve_fusion_evidence,
)


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


def test_fusion_evidence_queries_use_exact_pair_variants_and_tumor_type():
    queries = _fusion_evidence_queries("EML4::ALK", tumor_type="LUAD")

    assert queries[0].startswith('("EML4::ALK" OR "EML4-ALK"')
    assert "lung adenocarcinoma" in queries[0]
    assert "fusion OR rearrangement OR translocation" in queries[1]


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
                title="Novel fusion report",
                abstract="A novel fusion was observed in one case.",
                journal="Cancer Res",
                publication_types=["Case Reports"],
            ),
        ]

    monkeypatch.setattr(literature, "cached_call", fake_cached_call)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 2)
    monkeypatch.setattr(literature.settings, "fusion_evidence_cache_ttl_seconds", 123)

    result = await retrieve_fusion_evidence("EML4::ALK", tumor_type="LUAD", max_results=10)

    assert result.well_supported is True
    assert result.retrieved_count == 2
    assert set(result.pmids) == {"1", "2"}
    assert result.evidence_cards[0].fusion == "EML4::ALK"
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
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 2)

    result = await retrieve_fusion_evidence("GENE1::GENE2", max_results=10)

    assert result.well_supported is False
    assert "novelty" in result.interpretation
