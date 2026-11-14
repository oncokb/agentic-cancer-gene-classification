"""Intergenic placeholders never become genes or exact-pair literature queries."""
from unittest.mock import AsyncMock

import httpx
import pytest

from src.pipeline import literature, normalization

PLACEHOLDERS = [".", "-", "NA", "N/A", "intergenic", "?"]


@pytest.fixture
def hgnc(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def handler(request):
        requests.append(request.url.path)
        assert request.url.path == "/fetch/symbol/OR4N2"
        return httpx.Response(200, json={"response": {"docs": [{"symbol": "OR4N2"}]}})

    monkeypatch.setattr(normalization.httpx, "AsyncClient", lambda: real_client(transport=httpx.MockTransport(handler)))
    return requests


@pytest.mark.parametrize("placeholder", PLACEHOLDERS + [" na ", " n/a ", " INTERGENIC ", "---"])
@pytest.mark.parametrize("separator", ["::", "--", "/"])
@pytest.mark.parametrize("first", [True, False])
async def test_only_named_partner_is_resolved(placeholder, separator, first, hgnc):
    partners = [placeholder, "OR4N2"] if first else ["OR4N2", placeholder]
    fusion = separator.join(partners)
    assert normalization.split_fusion(fusion) == tuple(part.strip() for part in partners)
    result = await normalization.normalize_fusions([fusion])
    assert list(result) == ["OR4N2"]
    assert result["OR4N2"][1] == [fusion]
    assert result.inputs[fusion].warnings
    assert hgnc == ["/fetch/symbol/OR4N2"]


@pytest.mark.parametrize("fusion", PLACEHOLDERS + [".::.", "N/A::?", "intergenic--NA", "N/A/N/A", "----"])
async def test_all_placeholders_have_no_annotations_or_network(fusion, hgnc):
    result = await normalization.normalize_fusions([fusion])
    assert not result
    assert "no gene partners" in result.inputs[fusion].warnings[0]
    assert hgnc == []


@pytest.mark.parametrize("placeholder", PLACEHOLDERS)
@pytest.mark.parametrize("first", [True, False])
async def test_intergenic_evidence_is_skipped(placeholder, first, monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError("No network lookup allowed"))
    monkeypatch.setattr(literature, "resolve_fusion_partner_aliases", lookup)
    monkeypatch.setattr(literature, "_esearch", lookup)
    monkeypatch.setattr(literature, "cached_call", lookup)
    partners = [placeholder, "OR4N2"] if first else ["OR4N2", placeholder]
    result = await literature.retrieve_fusion_evidence("::".join(partners), force_refresh=True)
    assert result.retrieval_status == "skipped"
    assert result.literature_scope is None
    assert "intergenic" in result.interpretation
    assert result.pmids == []
    lookup.assert_not_awaited()


@pytest.mark.parametrize("symbol", PLACEHOLDERS + ["123", "***"])
async def test_direct_resolve_placeholder_makes_no_requests(symbol):
    client = AsyncMock()
    result = await normalization.resolve_gene(symbol, client)
    assert not result.resolved
    client.get.assert_not_awaited()


@pytest.mark.parametrize("field", ["symbol", "alias_symbol", "prev_symbol", None])
async def test_hgnc_search_guard_checks_symbol_and_synthetic_alias_metadata(field):
    # Real HGNC search docs contain symbol/hgnc_id/score, not aliases. Extra
    # metadata here tests the guard defensively; it does not model real alias resolution.
    doc = {"symbol": "MATCH", "alias_symbol": [], "prev_symbol": []}
    if field == "symbol":
        doc["symbol"] = "miX"
    elif field:
        doc[field] = ["miX"]

    def handler(request):
        docs = [] if "/fetch/" in request.url.path else [{"symbol": "PHIP"}, doc]
        return httpx.Response(200, json={"response": {"docs": docs}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await normalization.resolve_gene("mix", client)
    assert result.resolved is (field is not None)
    if field:
        assert result.canonical_symbol == doc["symbol"]


async def test_valid_one_letter_symbol_keeps_exact_fetch():
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"response": {"docs": [{"symbol": "T"}]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert (await normalization.resolve_gene("T", client)).resolved
    assert requests == ["/fetch/symbol/T"]


async def test_all_intergenic_evidence_is_skipped_without_cache_or_network(monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError("No lookup allowed"))
    monkeypatch.setattr(literature, "cached_call", lookup)
    monkeypatch.setattr(literature, "_esearch", lookup)
    result = await literature.retrieve_fusion_evidence(".::.")
    assert result.retrieval_status == "skipped"
    assert result.pmids == []
    lookup.assert_not_awaited()


@pytest.mark.parametrize("fusion", [
    ".::OR4N2", "OR4N2::.", "N/A/OR4N2", "intergenic::OR4N2",
    "---OR4N2", "---::OR4N2", "----OR4N2", "OR4N2::---",
    "--::OR4N2", "::::OR4N2", "---::::OR4N2",
])
def test_gene_literature_never_queries_or_scores_placeholder_partners(fusion):
    from src.models.schema import LiteratureRecord

    partners = literature._fusion_partners("OR4N2", [fusion])
    assert partners == []
    assert literature._fusion_partner_search_queries("OR4N2", [fusion], "LUAD") == ([], [])
    record = LiteratureRecord(pmid="1", title="An unrelated paper.", abstract="N and A. intergenic -OR4N2.")
    assert literature._fusion_cooccurrence_score(record, partners) is None


def test_gene_literature_preserves_real_partner_query_and_boost():
    from src.models.schema import LiteratureRecord

    partners = literature._fusion_partners("OR4N2", ["ALK::OR4N2"])
    assert partners == ["ALK"]
    assert literature._fusion_partner_search_queries("OR4N2", ["ALK::OR4N2"], None)[0] == ['"OR4N2" AND "ALK"']
    assert literature._fusion_cooccurrence_score(LiteratureRecord(pmid="1", title="ALK fusion", abstract=""), partners) == 1


@pytest.mark.parametrize("fusion, expected", [
    ("---::OR4N2", ("---", "OR4N2")),
    ("--::OR4N2", (".", "OR4N2")),
    ("::::OR4N2", (".", "OR4N2")),
    (":::OR4N2", (".", "OR4N2")),
    ("/---OR4N2", (".", "OR4N2")),
    ("---::::OR4N2", ("---", "OR4N2")),
    ("----OR4N2", ("--", "OR4N2")),
    ("OR4N2::---", ("OR4N2", "---")),
    ("ALK::BRAF::TP53", ("ALK", "BRAF::TP53")),
])
def test_degenerate_separators_do_not_manufacture_partners(fusion, expected):
    assert normalization.split_fusion(fusion) == expected


def test_standalone_alias_is_not_a_fusion_partner():
    assert literature._fusion_partners("ALK", ["CD246"]) == []


@pytest.mark.parametrize("fusion", ["---::OR4N2", "--::OR4N2", "::::OR4N2", "---::::OR4N2"])
async def test_degenerate_separators_only_resolve_named_partner(fusion, hgnc):
    result = await normalization.normalize_fusions([fusion])
    assert list(result) == ["OR4N2"]
    assert result.inputs[fusion].warnings
    assert hgnc == ["/fetch/symbol/OR4N2"]
