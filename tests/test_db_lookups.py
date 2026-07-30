"""Tests for deterministic database lookup helpers."""

import httpx
import pytest

from src.pipeline import cache as cache_module
from src.pipeline.db_lookups import (
    ONCOKB_CURATED_GENES_URL,
    OncoKBConfigurationError,
    OncoKBGeneLookup,
)


@pytest.mark.asyncio
async def test_oncokb_lookup_caches_genes_per_instance():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert str(request.url) == ONCOKB_CURATED_GENES_URL
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
async def test_oncokb_lookup_shares_redis_cache_across_instances():
    try:
        await cache_module._get_client().ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=[{"hugoSymbol": "TP53"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first_run = OncoKBGeneLookup(api_token="token")
        assert await first_run.contains("TP53", client) is True

        # A second instance simulates a separate pipeline run/process — it
        # should hit the shared Redis cache, not the network.
        second_run = OncoKBGeneLookup(api_token="token")
        assert await second_run.contains("TP53", client) is True

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_oncokb_lookup_requires_api_token():
    lookup = OncoKBGeneLookup(api_token="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
        with pytest.raises(OncoKBConfigurationError):
            await lookup.contains("TP53", client)
