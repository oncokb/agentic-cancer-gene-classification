"""Tests that PubMed retrieval avoids re-hitting NCBI on a cache hit."""

from __future__ import annotations

import httpx
import pytest

from src.pipeline import cache as cache_module
from src.pipeline.literature import _esearch


@pytest.fixture
async def _require_redis():
    client = cache_module._get_client()
    try:
        await client.ping()
    except Exception as exc:
        pytest.skip(f"Redis not reachable: {exc}")


async def test_esearch_caches_across_calls(_require_redis):
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"esearchresult": {"idlist": ["123", "456"]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first = await _esearch("TP53 AND cancer", 10, client)
        second = await _esearch("TP53 AND cancer", 10, client)

    assert first == ["123", "456"]
    assert second == ["123", "456"]
    assert len(requests) == 1
