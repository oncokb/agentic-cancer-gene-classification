"""Tests for deterministic database lookup helpers."""

import httpx
import pytest

from src.pipeline.db_lookups import (
    ONCOKB_GENES_URL,
    OncoKBConfigurationError,
    OncoKBGeneLookup,
)


@pytest.mark.asyncio
async def test_oncokb_lookup_caches_genes_per_instance():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == ONCOKB_GENES_URL
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(
            200,
            json=[
                {"hugoSymbol": "TP53"},
                {"hugoSymbol": "BRAF"},
                {"notSymbol": "ignored"},
            ],
        )

    lookup = OncoKBGeneLookup(api_token="token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await lookup.contains("TP53", client) is True
        assert await lookup.contains("ALK", client) is False

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_oncokb_lookup_requires_api_token():
    lookup = OncoKBGeneLookup(api_token="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
        with pytest.raises(OncoKBConfigurationError):
            await lookup.contains("TP53", client)
