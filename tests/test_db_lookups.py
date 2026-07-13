"""Tests for deterministic database lookup helpers."""

import asyncio

import httpx
import pytest

from src.pipeline.db_lookups import (
    ONCOKB_CURATED_GENES_URL,
    OncoKBConfigurationError,
    OncoKBGeneLookup,
    configured_oncokb_api_token,
    default_oncokb_gene_cache_path,
    load_cached_oncokb_genes,
    oncokb_config_status,
    save_cached_oncokb_genes,
    save_oncokb_api_token,
)


@pytest.mark.asyncio
async def test_oncokb_lookup_caches_genes_per_instance(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
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
async def test_oncokb_lookup_load_cache_is_concurrency_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {"hugoSymbol": "TP53"},
                {"hugoSymbol": "BRAF"},
            ],
        )

    lookup = OncoKBGeneLookup(api_token="token")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await asyncio.gather(
            lookup.contains("TP53", client),
            lookup.contains("ALK", client),
            lookup.contains("BRAF", client),
        )

    assert results == [True, False, True]
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_oncokb_lookup_reuses_persistent_gene_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("src.pipeline.db_lookups.settings.oncokb_gene_cache_ttl_hours", 24)

    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=[
                {"hugoSymbol": "TP53"},
                {"hugoSymbol": "BRAF"},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        first_lookup = OncoKBGeneLookup(api_token="token")
        assert await first_lookup.contains("TP53", client) is True

        second_lookup = OncoKBGeneLookup(api_token="token")
        assert await second_lookup.contains("BRAF", client) is True

    assert len(requests) == 1
    assert default_oncokb_gene_cache_path().exists()


def test_oncokb_persistent_gene_cache_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("src.pipeline.db_lookups.settings.oncokb_gene_cache_ttl_hours", 0)

    save_cached_oncokb_genes({"TP53"})

    assert load_cached_oncokb_genes() is None


@pytest.mark.asyncio
async def test_oncokb_lookup_requires_api_token():
    lookup = OncoKBGeneLookup(api_token="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) as client:
        with pytest.raises(OncoKBConfigurationError):
            await lookup.contains("TP53", client)


def test_save_oncokb_api_token_configures_local_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("AGCG_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr("src.pipeline.db_lookups.settings.oncokb_api_token", "")

    response = save_oncokb_api_token(" local-token \n")
    status = oncokb_config_status()

    assert response.configured is True
    assert response.source == "local_upload"
    assert configured_oncokb_api_token() == "local-token"
    assert status.configured is True
    assert status.source == "local_upload"
