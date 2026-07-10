"""Unit tests for fusion splitting and gene normalization."""

import httpx
import pytest

from src.pipeline.normalization import (
    ENSEMBL_LOOKUP_URL,
    HGNC_SEARCH_URL,
    _is_ensembl_id,
    _resolve_ensembl_ids,
    resolve_gene,
    split_fusion,
)


def test_split_double_colon():
    g1, g2 = split_fusion("ANKRD13A::ACACB")
    assert g1 == "ANKRD13A"
    assert g2 == "ACACB"


def test_split_double_dash():
    g1, g2 = split_fusion("EML4--ALK")
    assert g1 == "EML4"
    assert g2 == "ALK"


def test_split_slash():
    g1, g2 = split_fusion("BCR/ABL1")
    assert g1 == "BCR"
    assert g2 == "ABL1"


def test_split_no_separator():
    g1, g2 = split_fusion("KRAS")
    assert g1 == "KRAS"
    assert g2 is None


def test_ensembl_id_detection():
    assert _is_ensembl_id("ENSG00000253796")
    assert _is_ensembl_id("ENSG00000141510.21")
    assert not _is_ensembl_id("ACACB")
    assert not _is_ensembl_id("ANKRD13A")


@pytest.mark.asyncio
async def test_resolve_ensembl_ids_batches_lookup_and_maps_hgnc_metadata():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert str(request.url) == ENSEMBL_LOOKUP_URL
        assert request.headers["content-type"] == "application/json"
        assert request.content == b'{"ids":["ENSG00000141510"]}'
        return httpx.Response(
            200,
            json={
                "ENSG00000141510": {
                    "object_type": "Gene",
                    "display_name": "TP53",
                    "description": "tumor protein p53 [Source:HGNC Symbol;Acc:HGNC:11998]",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await _resolve_ensembl_ids(
            ["ENSG00000141510", "ENSG00000141510.21"],
            client,
        )

    assert len(requests) == 1
    assert resolved["ENSG00000141510"].canonical_symbol == "TP53"
    assert resolved["ENSG00000141510"].hgnc_id == "HGNC:11998"
    assert resolved["ENSG00000141510"].resolved is True
    assert resolved["ENSG00000141510.21"].canonical_symbol == "TP53"


@pytest.mark.asyncio
async def test_resolve_ensembl_ids_falls_back_when_ensembl_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow Ensembl response", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await _resolve_ensembl_ids(["ENSG00000253796"], client)

    assert resolved["ENSG00000253796"].canonical_symbol == "ENSG00000253796"
    assert resolved["ENSG00000253796"].resolved is False
    assert resolved["ENSG00000253796"].unresolvable is True


@pytest.mark.asyncio
async def test_resolve_gene_uses_hgnc_search_after_fetch_miss():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).startswith(HGNC_SEARCH_URL.format(symbol="OLD")):
            return httpx.Response(
                200,
                json={
                    "response": {
                        "docs": [
                            {
                                "symbol": "NEW",
                                "hgnc_id": "HGNC:1",
                            }
                        ]
                    }
                },
            )
        return httpx.Response(200, json={"response": {"docs": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        resolved = await resolve_gene("OLD", client)

    assert resolved.canonical_symbol == "NEW"
    assert resolved.hgnc_id == "HGNC:1"


@pytest.mark.asyncio
async def test_resolve_gene_continues_when_hgnc_lookup_fails():
    class FailingClient:
        async def get(self, *args, **kwargs):
            raise httpx.ConnectError("temporary HGNC outage")

    resolved = await resolve_gene("ACACB", FailingClient())

    assert resolved.input_symbol == "ACACB"
    assert resolved.canonical_symbol == "ACACB"
    assert resolved.resolved is False
    assert resolved.unresolvable is False
