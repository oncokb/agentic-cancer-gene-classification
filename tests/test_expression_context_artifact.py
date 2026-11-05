"""Artifact client against the v1 static JSON contract (pytest-httpx)."""

from __future__ import annotations

import httpx
import pytest

from src.expression_context.artifact_client import (
    ArtifactClient,
    ArtifactContractError,
    ArtifactUnavailable,
)
from src.expression_context.settings import ExpressionContextSettings
from tests.expression_context_helpers import (
    BASE_URL,
    RELEASE_ID,
    aliases,
    gene_file,
    manifest,
    module_redis,
    url,
)


def _settings(**overrides) -> ExpressionContextSettings:
    values = dict(
        enabled=True,
        artifact_base_url=BASE_URL + "/",
        http_retry_attempts=3,
        http_retry_backoff_seconds=0,
    )
    values.update(overrides)
    return ExpressionContextSettings(**values)


@pytest.fixture
async def client(fake_redis):
    # Core cached_call and the module's own client both hit the same fake.
    artifact_client = ArtifactClient(_settings(), redis=module_redis(fake_redis))
    yield artifact_client
    await artifact_client.aclose()


def test_enabled_requires_base_url():
    with pytest.raises(ValueError, match="ARTIFACT_BASE_URL"):
        ExpressionContextSettings(enabled=True, artifact_base_url="  ")
    assert ExpressionContextSettings(enabled=False).enabled is False


def test_settings_read_env_prefix(monkeypatch):
    monkeypatch.setenv("EXPRESSION_CONTEXT_ENABLED", "true")
    monkeypatch.setenv("EXPRESSION_CONTEXT_ARTIFACT_BASE_URL", "https://x.test/")
    monkeypatch.setenv("EXPRESSION_CONTEXT_HTTP_RETRY_ATTEMPTS", "5")
    loaded = ExpressionContextSettings()
    assert loaded.enabled and loaded.base_url == "https://x.test"
    assert loaded.http_retry_attempts == 5


async def test_manifest_parses_and_ignores_unknown_fields(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), json=manifest())
    parsed = await client.get_manifest()
    assert parsed.release_id == RELEASE_ID
    assert parsed.params["log2fc"] == 1.0
    assert parsed.cancer_type("BRCA").tumor_type_query == "breast cancer"
    assert parsed.cancer_type("XXXX") is None


@pytest.mark.parametrize("version", ["2", None, "1.1"])
async def test_manifest_rejects_unknown_schema_version(httpx_mock, client, version):
    body = manifest()
    if version is None:
        body.pop("schema_version")
    else:
        body["schema_version"] = version
    httpx_mock.add_response(url=url("manifest.json"), json=body)
    with pytest.raises(ArtifactContractError, match="schema_version"):
        await client.get_manifest()


async def test_gene_file_rejects_unknown_schema_version(httpx_mock, client):
    httpx_mock.add_response(url=url("genes/ERBB2.json"), json=gene_file(schema_version="9"))
    with pytest.raises(ArtifactContractError, match="schema_version"):
        await client.get_gene("ERBB2", RELEASE_ID)


async def test_gene_file_parses_and_404_means_absent(httpx_mock, client):
    httpx_mock.add_response(url=url("genes/ERBB2.json"), json=gene_file())
    httpx_mock.add_response(url=url("genes/NOPE1.json"), status_code=404)
    parsed = await client.get_gene("ERBB2", RELEASE_ID)
    assert parsed.gene.entrez_gene_id == 2064
    assert [ct.oncotree_code for ct in parsed.cancer_types][:2] == ["BRCA", "STAD"]
    assert await client.get_gene("NOPE1", RELEASE_ID) is None
    # 404 is definitive: not retried.
    assert len(httpx_mock.get_requests(url=url("genes/NOPE1.json"))) == 1


async def test_aliases_are_upper_cased(httpx_mock, client):
    httpx_mock.add_response(url=url("aliases.json"), json={"her2": "erbb2", "bad": 3})
    assert await client.get_aliases(RELEASE_ID) == {"HER2": "ERBB2"}


async def test_missing_aliases_file_is_empty(httpx_mock, client):
    httpx_mock.add_response(url=url("aliases.json"), status_code=404)
    assert await client.get_aliases(RELEASE_ID) == {}


async def test_transient_errors_are_retried(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), status_code=503)
    httpx_mock.add_exception(httpx.ReadTimeout("slow"), url=url("manifest.json"))
    httpx_mock.add_response(url=url("manifest.json"), json=manifest())
    assert (await client.get_manifest()).release_id == RELEASE_ID
    assert len(httpx_mock.get_requests()) == 3


async def test_retries_exhausted_raise_unavailable(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), status_code=502, is_reusable=True)
    with pytest.raises(ArtifactUnavailable, match="502"):
        await client.get_manifest()
    assert len(httpx_mock.get_requests()) == 3


async def test_client_errors_are_not_retried(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), status_code=403)
    with pytest.raises(ArtifactUnavailable, match="403"):
        await client.get_manifest()
    assert len(httpx_mock.get_requests()) == 1


async def test_non_json_body_is_contract_error(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), text="<html>")
    with pytest.raises(ArtifactContractError):
        await client.get_manifest()


async def test_timeouts_are_configured():
    artifact_client = ArtifactClient(
        _settings(http_timeout_seconds=7, http_connect_timeout_seconds=2)
    )
    try:
        timeout = artifact_client._http.timeout
        assert timeout.read == 7 and timeout.connect == 2
    finally:
        await artifact_client.aclose()


async def test_redis_cache_avoids_refetch(httpx_mock, client, fake_redis):
    httpx_mock.add_response(url=url("manifest.json"), json=manifest())
    httpx_mock.add_response(url=url("genes/ERBB2.json"), json=gene_file())
    httpx_mock.add_response(url=url("aliases.json"), json=aliases())
    for _ in range(2):
        await client.get_manifest()
        await client.get_gene("ERBB2", RELEASE_ID)
        await client.get_aliases(RELEASE_ID)
    assert len(httpx_mock.get_requests()) == 3
    keys = fake_redis.keys_with_prefix("expression_context:")
    assert any(f"gene:{RELEASE_ID}:ERBB2" in key for key in keys)


async def test_absent_gene_is_cached(httpx_mock, client, fake_redis):
    httpx_mock.add_response(url=url("genes/NOPE1.json"), status_code=404)
    assert await client.get_gene("NOPE1", RELEASE_ID) is None
    assert await client.get_gene("NOPE1", RELEASE_ID) is None
    assert len(httpx_mock.get_requests()) == 1


async def test_force_refresh_bypasses_cache(httpx_mock, client, fake_redis):
    httpx_mock.add_response(url=url("manifest.json"), json=manifest(), is_reusable=True)
    await client.get_manifest()
    await client.get_manifest(force_refresh=True)
    assert len(httpx_mock.get_requests()) == 2


async def test_cached_payload_with_unknown_version_rejected(httpx_mock, client, fake_redis):
    key = client._key("manifest")
    import json

    await fake_redis.set(key, json.dumps(manifest(schema_version="2")))
    with pytest.raises(ArtifactContractError):
        await client.get_manifest()


async def test_absent_gene_uses_short_negative_ttl(httpx_mock, fake_redis):
    artifact_client = ArtifactClient(
        _settings(gene_negative_cache_ttl_seconds=300), redis=module_redis(fake_redis)
    )
    httpx_mock.add_response(url=url("genes/NOPE1.json"), status_code=404, is_reusable=True)
    httpx_mock.add_response(url=url("genes/ERBB2.json"), json=gene_file())
    try:
        assert await artifact_client.get_gene("NOPE1", RELEASE_ID) is None
        await artifact_client.get_gene("ERBB2", RELEASE_ID)
        absent = [k for k in fake_redis.keys_with_prefix("expression_context:") if "absent" in k]
        assert len(absent) == 1
        # Hits keep the long TTL; the absence marker expires after 5 minutes.
        hit = next(k for k in fake_redis.keys_with_prefix("expression_context:") if ":gene:" in k)
        assert fake_redis._store[absent[0]][1] == fake_redis.now + 300
        assert fake_redis._store[hit][1] == fake_redis.now + 86400
        fake_redis.advance(301)
        assert await artifact_client.get_gene("NOPE1", RELEASE_ID) is None
        assert len(httpx_mock.get_requests(url=url("genes/NOPE1.json"))) == 2
    finally:
        await artifact_client.aclose()
