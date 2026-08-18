from __future__ import annotations

from src.models.schema import LiteratureRecord
from src.pipeline import literature
from src.pipeline.literature import (
    _fusion_partner_precedent_queries,
    retrieve_fusion_partner_evidence,
)


async def _uncached(_key, compute, ttl_seconds=None):
    return await compute()


def test_scoped_queries_restrict_to_tumor_type():
    queries = _fusion_partner_precedent_queries("ALK", tumor_type="LUAD", agnostic=False)

    assert len(queries) == 2
    assert all("lung adenocarcinoma" in q or "LUAD" in q for q in queries)
    assert all("cancer[MeSH Terms]" not in q for q in queries)


def test_agnostic_queries_ignore_tumor_type():
    scoped = _fusion_partner_precedent_queries("ALK", tumor_type="LUAD", agnostic=False)
    agnostic = _fusion_partner_precedent_queries("ALK", tumor_type="LUAD", agnostic=True)

    assert agnostic != scoped
    assert any("cancer[MeSH Terms]" in q for q in agnostic)
    assert all("LUAD" not in q and "lung adenocarcinoma" not in q for q in agnostic)


def test_queries_without_tumor_type_are_always_agnostic():
    with_none = _fusion_partner_precedent_queries("ALK", tumor_type=None, agnostic=False)
    agnostic = _fusion_partner_precedent_queries("ALK", tumor_type=None, agnostic=True)

    assert with_none == agnostic


async def test_retrieve_fusion_partner_evidence_scopes_to_tumor_type(monkeypatch):
    cache_keys = []

    async def fake_cached_call(key, compute, ttl_seconds=None):
        cache_keys.append((key, ttl_seconds))
        return await compute()

    async def fake_esearch(query, max_results, client):
        return ["1"]

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(
                pmid="1",
                title="ALK fusions in lung adenocarcinoma",
                abstract="Patients with ALK fusion lung cancer respond to crizotinib.",
                journal="J Clin Oncol",
                publication_types=["Journal Article"],
            )
        ]

    monkeypatch.setattr(literature, "cached_call", fake_cached_call)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "fusion_partner_evidence_cache_ttl_seconds", 456)

    result = await retrieve_fusion_partner_evidence("ALK", tumor_type="LUAD", max_results=10)

    assert result.scope == "tumor_type_scoped"
    assert result.has_precedent is True
    assert result.retrieved_count == 1
    assert result.pmids == ["1"]
    assert result.evidence_cards[0].pmid == "1"
    assert cache_keys[0][0].startswith("fusion_partner_evidence:")
    assert cache_keys[0][1] == 456


async def test_agnostic_follow_up_excludes_already_seen_pmids(monkeypatch):
    async def fake_esearch(query, max_results, client):
        return ["1", "2"]

    async def fake_efetch(pmids, client):
        return [
            LiteratureRecord(pmid="1", title="Scoped paper", abstract="Found in scoped pass."),
            LiteratureRecord(pmid="2", title="New paper", abstract="Only found in agnostic pass."),
        ]

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)

    result = await retrieve_fusion_partner_evidence(
        "ALK",
        tumor_type="LUAD",
        agnostic=True,
        exclude_pmids={"1"},
        max_results=10,
    )

    assert result.scope == "all_tumor_types"
    assert result.pmids == ["2"]
    assert result.retrieved_count == 1


async def test_no_precedent_found_reports_zero_papers(monkeypatch):
    async def fake_esearch(query, max_results, client):
        return []

    async def fake_efetch(pmids, client):
        return []

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)

    result = await retrieve_fusion_partner_evidence("TUSC5", max_results=10)

    assert result.scope == "all_tumor_types"
    assert result.has_precedent is False
    assert result.retrieved_count == 0
    assert "No non-retracted PubMed records" in result.interpretation
