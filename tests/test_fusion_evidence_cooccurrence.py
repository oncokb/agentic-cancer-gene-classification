"""Issue #166: gene co-occurrence search for fusion evidence.

PubMed's phrase index does not match "GENE1::GENE2" text, so IRF2BP2::RUNX2
papers that write the fusion only in "::" form were missed by the quoted
exact-pair phrases. No live network: every PubMed/HGNC call is mocked.
"""
from __future__ import annotations

import httpx
import pytest

from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import literature
from src.pipeline.literature import (
    _COOCCURRENCE_RETMAX_MULTIPLIER,
    _fusion_evidence_cache_key,
    _fusion_evidence_queries,
    retrieve_fusion_evidence,
)

_FUSION = "IRF2BP2::RUNX2"
_FUSION_TERMS = "(fusion*[tiab] OR rearrang*[tiab] OR translocation*[tiab] OR chimer*[tiab])"

_PHRASE_PMID = "40026174"
_COLON_ONLY_PMIDS = ["38084005", "42314909"]

_RECORDS = {
    "40026174": LiteratureRecord(
        pmid="40026174",
        title="IRF2BP2-RUNX2 fusion in a mesenchymal tumor",
        abstract="RNA sequencing identified an IRF2BP2-RUNX2 fusion transcript.",
        publication_types=["Journal Article"],
    ),
    "38084005": LiteratureRecord(
        pmid="38084005",
        title="A sarcoma harboring IRF2BP2::RUNX2",
        abstract="Targeted RNA sequencing detected IRF2BP2::RUNX2 in the tumor.",
        publication_types=["Journal Article"],
    ),
    "42314909": LiteratureRecord(
        pmid="42314909",
        title="Recurrent IRF2BP2::RUNX2 gene fusion",
        abstract="We describe tumors with the IRF2BP2::RUNX2 rearrangement.",
        publication_types=["Journal Article"],
    ),
    # Names both genes, but as a signalling axis, not a fusion (like 31186082).
    "31186082": LiteratureRecord(
        pmid="31186082",
        title="The IRF2BP2-KLF2 axis regulates RUNX2 expression in osteoblasts",
        abstract="IRF2BP2 represses KLF2; RUNX2 levels rose after knockdown.",
        publication_types=["Journal Article"],
    ),
}


async def _uncached(_key, compute, ttl_seconds=None, force_refresh=False):
    return await compute()


async def _resolve_no_aliases(symbol, client):
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=True)


async def _resolve_fails(symbol, client):
    raise httpx.ConnectError("HGNC down")


def _is_cooccurrence(query: str) -> bool:
    return "[tiab]" in query


def _patch(monkeypatch, *, resolve=_resolve_no_aliases, cooccurrence_pmids=None, fail_cooccurrence=False):
    calls = []

    async def fake_esearch(query, max_results, client):
        calls.append((query, max_results))
        if _is_cooccurrence(query):
            if fail_cooccurrence:
                raise httpx.HTTPStatusError(
                    "upstream error",
                    request=httpx.Request("POST", literature.ESEARCH_URL),
                    response=httpx.Response(503),
                )
            return list(cooccurrence_pmids if cooccurrence_pmids is not None else _COLON_ONLY_PMIDS)
        return [_PHRASE_PMID]

    async def fake_efetch(pmids, client):
        return [_RECORDS[pmid] for pmid in pmids if pmid in _RECORDS]

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "resolve_gene", resolve)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 3)
    return calls


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def test_queries_include_tiab_cooccurrence_without_aliases():
    queries = _fusion_evidence_queries(_FUSION)
    expected = f'("IRF2BP2"[tiab]) AND ("RUNX2"[tiab]) AND {_FUSION_TERMS}'
    assert expected in queries
    # The exact-pair phrase query is still run, ahead of the co-occurrence one.
    assert queries[0].startswith('("IRF2BP2::RUNX2" OR')
    assert queries.index(expected) > 0


def test_queries_include_tiab_cooccurrence_with_aliases_and_tumor_scope():
    queries = _fusion_evidence_queries(
        _FUSION, "sarcoma", ["PEBP2A1", "FLJ12345"], ["CBFA1", "AML3", "cbfa1"],
    )
    cooccurrence = (
        '("IRF2BP2"[tiab] OR "PEBP2A1"[tiab]) AND '
        '("RUNX2"[tiab] OR "CBFA1"[tiab] OR "AML3"[tiab]) AND ' + _FUSION_TERMS
    )
    assert cooccurrence in queries
    assert f'{cooccurrence} AND "sarcoma"' in queries
    assert "FLJ12345" not in " ".join(q for q in queries if "[tiab]" in q)
    assert len(queries) == 4


def test_cooccurrence_caps_aliases_per_partner():
    aliases = [f"ALIAS{i}" for i in range(20)]
    (query,) = [q for q in _fusion_evidence_queries("A1::B1", None, aliases, []) if "[tiab]" in q]
    left = query.split(" AND ")[0]
    assert left.count("[tiab]") == literature._MAX_QUERY_ALIASES_PER_PARTNER + 1


@pytest.mark.parametrize("fusion", ["IRF2BP2::.", ".::RUNX2", "IRF2BP2::--", "IRF2BP2::"])
def test_no_cooccurrence_query_for_punctuation_or_empty_partner(fusion):
    assert literature._fusion_cooccurrence_query(fusion) is None
    assert not any("[tiab]" in q for q in _fusion_evidence_queries(fusion, "sarcoma"))


def test_unnamed_ensembl_partner_builds_cooccurrence_query():
    query = literature._fusion_cooccurrence_query("ENSG00000284922::RUNX2")
    assert query.startswith('("ENSG00000284922"[tiab]) AND ("RUNX2"[tiab])')


def test_cooccurrence_query_skips_unquotable_alias():
    query = literature._fusion_cooccurrence_query("A1::B1", ['BAD"ALIAS', "OK1"], [])
    assert 'BAD"ALIAS' not in query
    assert '"OK1"[tiab]' in query


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

async def test_cooccurrence_retrieves_colon_only_papers(monkeypatch):
    calls = _patch(monkeypatch)

    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert set(result.pmids) == {_PHRASE_PMID, *_COLON_ONLY_PMIDS}
    assert result.retrieved_count == 3
    assert not result.partial
    assert result.literature_scope == "any_tumor"
    assert not result.interpretation.startswith("No non-retracted")
    retmax = {query: max_results for query, max_results in calls}
    assert all(
        value == (20 * _COOCCURRENCE_RETMAX_MULTIPLIER if _is_cooccurrence(query) else 20)
        for query, value in retmax.items()
    )


async def test_cooccurrence_retrieves_tumor_scoped(monkeypatch):
    calls = _patch(monkeypatch)

    result = await retrieve_fusion_evidence(_FUSION, tumor_type="sarcoma", max_results=20)

    assert set(result.pmids) == {_PHRASE_PMID, *_COLON_ONLY_PMIDS}
    assert result.requested_tumor_pmids == ["38084005"]
    assert result.literature_scope == "requested_tumor"
    assert sum(_is_cooccurrence(query) for query, _ in calls) == 2


async def test_hgnc_alias_failure_still_finds_colon_only_papers(monkeypatch):
    calls = _patch(monkeypatch, resolve=_resolve_fails)

    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert set(_COLON_ONLY_PMIDS) <= set(result.pmids)
    assert not result.partial
    assert (
        f'("IRF2BP2"[tiab]) AND ("RUNX2"[tiab]) AND {_FUSION_TERMS}' in [q for q, _ in calls]
    )


async def test_cooccurring_non_fusion_record_is_filtered(monkeypatch):
    _patch(monkeypatch, cooccurrence_pmids=["31186082", *_COLON_ONLY_PMIDS])

    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert "31186082" not in result.pmids
    assert set(result.pmids) == {_PHRASE_PMID, *_COLON_ONLY_PMIDS}


async def test_pmids_merge_deterministically_across_searches(monkeypatch):
    _patch(monkeypatch, cooccurrence_pmids=[_COLON_ONLY_PMIDS[1], _PHRASE_PMID, _COLON_ONLY_PMIDS[0]])
    fetched = []
    original = literature._efetch

    async def recording_efetch(pmids, client):
        fetched.append(list(pmids))
        return await original(pmids, client)

    literature._efetch = recording_efetch
    try:
        first = await retrieve_fusion_evidence(_FUSION, max_results=20)
        second = await retrieve_fusion_evidence(_FUSION, max_results=20)
    finally:
        literature._efetch = original
    # Phrase hits first, then co-occurrence hits in their own order, deduped.
    assert fetched[0] == [_PHRASE_PMID, _COLON_ONLY_PMIDS[1], _COLON_ONLY_PMIDS[0]]
    assert fetched[0] == fetched[1]
    assert first.pmids == second.pmids


@pytest.mark.parametrize("tumor_type", [None, "sarcoma"])
async def test_cooccurrence_failure_is_partial_and_not_cached(monkeypatch, fake_redis, tumor_type):
    from src.pipeline import cache

    _patch(monkeypatch, fail_cooccurrence=True)
    monkeypatch.setattr(literature, "cached_call", cache.cached_call)

    result = await retrieve_fusion_evidence(_FUSION, tumor_type=tumor_type, max_results=20)

    assert result.partial is True
    assert result.retrieval_status == "ok"
    assert result.pmids == [_PHRASE_PMID]
    assert "results are partial" in result.interpretation
    # A failed tumor-scoped co-occurrence search leaves tumor scope unknown.
    assert result.literature_scope == (None if tumor_type else "any_tumor")
    assert not fake_redis.keys_with_prefix("fusion_evidence:")


async def test_cooccurrence_failure_with_no_phrase_hits_is_not_no_literature(monkeypatch):
    calls = _patch(monkeypatch, fail_cooccurrence=True)

    async def phrase_empty(query, max_results, client):
        if _is_cooccurrence(query):
            raise httpx.ReadTimeout("timeout")
        calls.append((query, max_results))
        return []

    monkeypatch.setattr(literature, "_esearch", phrase_empty)
    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert result.partial is True
    assert result.literature_scope is None
    assert result.retrieval_status == "ok"


def test_cache_key_includes_cooccurrence_version():
    key = _fusion_evidence_cache_key(_FUSION, None, 20)
    assert '"cooccurrence_search_version": 1' in key
