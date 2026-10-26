"""Regression coverage for fusion-context queries in per-gene retrieval."""

import pytest

from src.models.schema import LiteratureRecord
from src.pipeline import literature


@pytest.mark.parametrize("tumor_type", [None, "UTROSCT"])
async def test_partner_query_runs_before_early_stop(monkeypatch, tumor_type):
    monkeypatch.setattr(literature.settings, "pubmed_staged_retrieval", True)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 4)
    monkeypatch.setattr(literature.settings, "max_papers_for_synthesis", 8)
    queries = []
    fetched = []
    common_pmids = [str(i) for i in range(1, 9)]
    partner_query = '"GREB1" AND "BCL9L"'

    async def fake_esearch(query, max_results, client):
        queries.append(query)
        if query == partner_query:
            return ["1", "9", "9"]
        return common_pmids

    async def fake_efetch(pmids, client):
        fetched.extend(pmids)
        return [LiteratureRecord(pmid=p, title=f"Paper {p}", abstract="Evidence.") for p in pmids]

    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    records = await literature._tier1_retrieve(
        "GREB1", ["GREB1::BCL9L", "BCL9L::GREB1"], tumor_type
    )

    assert partner_query in queries, queries
    assert queries.count(partner_query) == 1
    assert len(queries) == (3 if tumor_type else 2)
    assert not any("evidence_priority" == tier for r in records for tier in r.matched_query_tiers)
    assert set(fetched) == set(common_pmids) | {"9"}
    assert len(fetched) == len(set(fetched)) == len(records) == 9
    by_pmid = {r.pmid: r for r in records}
    assert "fusion_partner" in by_pmid["1"].matched_query_tiers
    assert "mesh_gene_name" in by_pmid["1"].matched_query_tiers
    assert by_pmid["9"].matched_query_tiers == ["fusion_partner"]


@pytest.mark.parametrize("staged", [True, False])
@pytest.mark.parametrize("saturates", [True, False])
async def test_unique_partners_have_bounded_queries(monkeypatch, staged, saturates):
    monkeypatch.setattr(literature.settings, "pubmed_staged_retrieval", staged)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 4)
    monkeypatch.setattr(literature.settings, "max_papers_for_synthesis", 8)
    queries = []
    fetch_batches = []

    async def fake_esearch(query, max_results, client):
        queries.append(query)
        return [str(i) for i in range(8)] if saturates else ["1"]

    async def fake_efetch(pmids, client):
        fetch_batches.append(list(pmids))
        return [LiteratureRecord(pmid=p, title=f"Paper {p}", abstract="Evidence.") for p in pmids]

    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    partners = ["BCL9L", "NCOA1", "NCOA2"]
    fusions = [f"GREB1::{partner}" for partner in partners] + ["BCL9L::GREB1"]
    records = await literature._tier1_retrieve("GREB1", fusions, "UTROSCT")

    for partner in partners:
        assert queries.count(f'"GREB1" AND "{partner}"') == 1
    assert len(queries) == len(set(queries))
    # First stage: tumor type + MeSH + one query per unique partner.
    assert {f'"GREB1" AND "{p}"' for p in partners} <= set(queries[:5])
    # Full retrieval adds four broad queries and at most two scoped partner queries.
    assert len(queries) == (5 if staged and saturates else 11)
    fetched = [pmid for batch in fetch_batches for pmid in batch]
    assert len(fetched) == len(set(fetched)) == len(records)


@pytest.mark.parametrize("tumor_type", [
    "UTROSCT",
    " utrosct ",
    "uterine tumor resembling ovarian sex cord tumor",
    "uterine tumor resembling ovarian sex-cord tumor",
])
def test_utrosct_aliases_are_uterine_and_separate_from_ovarian(tumor_type):
    terms = literature._expand_tumor_type_terms(tumor_type)
    assert terms == [
        "uterine tumor resembling ovarian sex cord tumor",
        "uterine tumor resembling ovarian sex-cord tumor",
        "UTROSCT",
    ]
    ovarian_terms = literature._expand_tumor_type_terms("OV")
    assert set(terms).isdisjoint(ovarian_terms)
    fragment = literature._tumor_type_query_fragment(tumor_type)
    assert fragment == "(" + " OR ".join(f'"{term}"' for term in terms) + ")"
    assert not any(f'"{term}"' in fragment for term in ovarian_terms)
    assert ovarian_terms == ["ovarian cancer", "ovarian serous carcinoma", "OV"]


@pytest.mark.parametrize("tumor_type, synonym", [
    ("uterine sarcoma", "uterine mesenchymal tumor"),
    ("uterine leiomyosarcoma", "uterine LMS"),
    ("endometrial stromal sarcoma", "uterine stromal sarcoma"),
    ("low-grade endometrial stromal sarcoma", "LG-ESS"),
    ("high-grade endometrial stromal sarcoma", "HG-ESS"),
])
def test_uterine_sarcoma_terms_do_not_leak_ovarian_terms(tumor_type, synonym):
    terms = literature._expand_tumor_type_terms(tumor_type)
    assert tumor_type in terms
    assert synonym in terms
    assert all("ovarian" not in term.lower() for term in terms)
