"""DogStatsD metrics for the OpenEvidence sidecar (endpoint outcomes, paid
upstream calls, retries, cache peeks, and the async lookup lifecycle).

A FakeStatsd is installed as src.observability's client with
DATADOG_METRICS_ENABLED on, so these tests see exactly what would be sent to
the Agent (metric names without the `acgc.` namespace, which DogStatsd
prepends). Metric names are spelled out literally on purpose: they are the
contract the Datadog dashboard (docs/datadog/) is built on.
"""

from __future__ import annotations

import asyncio
import json
from typing import List, Optional

import httpx
import pytest
import tenacity
from fastapi import Response

from src import main, observability
from src.models.schema import FusionInput, ResolvedGene
from src.pipeline import openevidence, openevidence_warmup
from src.pipeline.openevidence import OpenEvidenceClient

_SSE_STREAM = (
    'data: {"text": "NCCN recommends alectinib first-line for ALK-rearranged NSCLC. "}\n\n'
    'data: {"text": "[[1]]", "reference": {"citation_key": 1, '
    '"reference_text": "National Comprehensive Cancer Network. Non-Small Cell Lung Cancer.", '
    '"reference_detail": {"title": "Non-Small Cell Lung Cancer", '
    '"authors_string": "National Comprehensive Cancer Network", '
    '"publication_date": "2026-09-02", '
    '"url": "https://www.nccn.org/professionals/physician_gls/pdf/nscl.pdf"}, '
    '"source_texts": []}}\n\n'
    "data: [DONE]\n\n"
)
_REAL_POST = openevidence._post_streaming_analysis


class FakeStatsd:
    def __init__(self) -> None:
        self.calls: list = []

    def increment(self, metric, value=1, tags=None):
        self.calls.append(("increment", metric, value, list(tags or [])))

    def distribution(self, metric, value, tags=None):
        self.calls.append(("distribution", metric, value, list(tags or [])))

    def set(self, metric, value, tags=None):
        self.calls.append(("set", metric, value, list(tags or [])))

    def named(self, metric: str, kind: Optional[str] = None) -> List[tuple]:
        return [call for call in self.calls if call[1] == metric and (kind is None or call[0] == kind)]

    def tags_of(self, metric: str, kind: str = "increment") -> List[List[str]]:
        return [call[3] for call in self.named(metric, kind)]

    def oe_calls(self) -> List[tuple]:
        return [call for call in self.calls if call[1].startswith("openevidence.")]


@pytest.fixture
def statsd(monkeypatch):
    fake = FakeStatsd()
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability, "_statsd_client", fake)
    return fake


@pytest.fixture(autouse=True)
def _oe_settings(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)
    monkeypatch.setattr(main.settings, "openevidence_api_key", "test-key")
    monkeypatch.setattr(main.settings, "openevidence_model", "osler")
    monkeypatch.setattr(main.settings, "openevidence_sidecar_pending_wait_seconds", 0.05)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_retry_after_seconds", 10)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_inflight_ttl_seconds", 600)
    monkeypatch.setattr(main.settings, "openevidence_sidecar_failed_ttl_seconds", 300)
    # No real backoff sleeps between retry attempts.
    monkeypatch.setattr(openevidence._post_streaming_analysis.retry, "wait", tenacity.wait_none())


class GatedUpstream:
    """Replaces the paid call; blocks until release(). By default it runs the
    REAL _post_streaming_analysis against an httpx.MockTransport (so the
    upstream-call metrics and their `source` tag are exercised end to end)."""

    def __init__(self, handler=None) -> None:
        self.calls = 0
        self._gate = asyncio.Event()
        self.fail_with: Optional[BaseException] = None
        self.handler = handler or (lambda request: httpx.Response(200, text=_SSE_STREAM))

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, question: str, api_key: str, client: httpx.AsyncClient) -> str:
        self.calls += 1
        await self._gate.wait()
        if self.fail_with is not None:
            raise self.fail_with
        async with httpx.AsyncClient(transport=httpx.MockTransport(self.handler)) as http_client:
            return await _REAL_POST(question, api_key, http_client)


@pytest.fixture
def upstream(monkeypatch):
    fake = GatedUpstream()
    monkeypatch.setattr(openevidence, "_post_streaming_analysis", fake)
    return fake


async def _request(gene: str = "ALK", **params):
    response = Response()
    response.status_code = 200
    result = await asyncio.wait_for(
        main.get_gene_openevidence(
            gene,
            response=response,
            tumor_type=params.get("tumor_type"),
            fusion=params.get("fusion"),
            cancer_associated=params.get("cancer_associated"),
            core_pmids=[],
            core_titles=[],
        ),
        timeout=2.0,
    )
    return result, response


async def _wait_for_background_lookups() -> None:
    tasks = list(main._openevidence_sidecar_tasks.values())
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)
    await asyncio.sleep(0)


def _outcomes(statsd: FakeStatsd) -> List[str]:
    return [
        tag.split(":", 1)[1]
        for tags in statsd.tags_of("openevidence.endpoint.requests")
        for tag in tags
        if tag.startswith("outcome:")
    ]


def _assert_low_cardinality(statsd: FakeStatsd) -> None:
    for _, _, _, tags in statsd.oe_calls():
        for tag in tags:
            for raw in ("ALK", "EML4", "NSCLC", "nsclc", "test-key", "://", "openevidence.com", "@"):
                assert raw not in tag, f"raw value {raw!r} leaked into metric tag {tag!r}"


async def test_pending_then_later_ready_emits_lifecycle_metrics(fake_redis, statsd, upstream):
    first, response = await _request("ALK", tumor_type="NSCLC", fusion="EML4::ALK")
    assert first.status == "pending"
    assert _outcomes(statsd) == ["pending"]
    assert statsd.tags_of("openevidence.endpoint.requests") == [
        ["outcome:pending", "model:osler", "available:false", "has_fusion:true", "has_tumor_type:true"]
    ]
    assert statsd.tags_of("openevidence.cache.lookups") == [["model:osler", "result:miss"]]
    assert statsd.tags_of("openevidence.lookups.started") == [["model:osler"]]
    assert statsd.tags_of("openevidence.endpoint.duration_ms", "distribution") == [
        ["outcome:pending", "model:osler"]
    ]

    upstream.release()
    await _wait_for_background_lookups()
    second, _ = await _request("ALK", tumor_type="NSCLC", fusion="EML4::ALK")

    assert second.status == "ready"
    assert _outcomes(statsd) == ["pending", "ready"]
    assert statsd.tags_of("openevidence.endpoint.requests")[-1] == [
        "outcome:ready", "model:osler", "available:true", "has_fusion:true", "has_tumor_type:true"
    ]
    assert statsd.tags_of("openevidence.lookups.completed") == [["model:osler", "result:success"]]
    assert statsd.tags_of("openevidence.lookups.time_to_ready_ms", "distribution") == [["model:osler"]]
    assert statsd.tags_of("openevidence.semaphore_wait_ms", "distribution") == [["model:osler"]]
    # Exactly one paid call, attributed to the sidecar.
    assert statsd.tags_of("openevidence.upstream.calls") == [
        ["model:osler", "status:success", "source:sidecar", "http_status_class:2xx"]
    ]
    assert statsd.tags_of("openevidence.upstream.duration_ms", "distribution") == [
        ["model:osler", "status:success", "source:sidecar"]
    ]
    assert statsd.tags_of("openevidence.upstream.time_to_first_chunk_ms", "distribution") == [
        ["model:osler", "source:sidecar"]
    ]
    assert statsd.named("openevidence.retries") == []
    _assert_low_cardinality(statsd)


async def test_cache_hit_counts_hit_without_starting_a_lookup(fake_redis, statsd, upstream):
    upstream.release()
    await OpenEvidenceClient().get_gene_analysis("ALK", tumor_type="NSCLC")  # fill the shared cache
    statsd.calls.clear()

    result, _ = await _request("ALK", tumor_type="NSCLC")

    assert result.status == "ready"
    assert _outcomes(statsd) == ["cache_hit"]
    assert statsd.tags_of("openevidence.endpoint.requests") == [
        ["outcome:cache_hit", "model:osler", "available:true", "has_fusion:false", "has_tumor_type:true"]
    ]
    assert statsd.tags_of("openevidence.cache.lookups") == [["model:osler", "result:hit"]]
    assert statsd.named("openevidence.lookups.started") == []
    assert statsd.named("openevidence.upstream.calls") == []
    # The sidecar's peek is not double counted in the generic cache-aside metric.
    assert statsd.named("redis_cache.lookups") == []
    _assert_low_cardinality(statsd)


async def test_failed_lookup_emits_failed_outcome(fake_redis, statsd, upstream):
    upstream.fail_with = RuntimeError("upstream exploded")
    await _request("ALK")
    upstream.release()
    await _wait_for_background_lookups()

    result, _ = await _request("ALK")

    assert result.status == "failed"
    assert _outcomes(statsd) == ["pending", "failed"]
    assert statsd.tags_of("openevidence.lookups.completed") == [["model:osler", "result:failed"]]
    assert statsd.named("openevidence.lookups.time_to_ready_ms") == []


async def test_upstream_timeout_is_counted_without_retry(fake_redis, statsd):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        with pytest.raises(httpx.ReadTimeout):
            await OpenEvidenceClient().get_gene_analysis("ALK", client=http_client)

    assert statsd.tags_of("openevidence.upstream.calls") == [["model:osler", "status:timeout", "source:other"]]
    assert statsd.tags_of("openevidence.upstream.duration_ms", "distribution") == [
        ["model:osler", "status:timeout", "source:other"]
    ]
    assert statsd.named("openevidence.retries") == []


async def test_retry_counts_each_paid_attempt_and_the_retry_reason(fake_redis, statsd):
    statuses = iter([429, 503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, text=_SSE_STREAM if status == 200 else "busy")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        analysis = await OpenEvidenceClient().get_gene_analysis("ALK", client=http_client)

    assert analysis.citations
    assert statsd.tags_of("openevidence.upstream.calls") == [
        ["model:osler", "status:http_error", "source:other", "http_status_class:4xx"],
        ["model:osler", "status:http_error", "source:other", "http_status_class:5xx"],
        ["model:osler", "status:success", "source:other", "http_status_class:2xx"],
    ]
    assert statsd.tags_of("openevidence.retries") == [
        ["model:osler", "reason:http_429", "source:other"],
        ["model:osler", "reason:http_5xx", "source:other"],
    ]


async def test_dedupe_counts_in_process_and_cross_pod_joins(fake_redis, statsd, upstream):
    results = await asyncio.gather(*[_request("ALK") for _ in range(3)])
    assert [result.status for result, _ in results] == ["pending"] * 3
    # A second worker/pod (empty in-process registry) is held off by the lease.
    registry = dict(main._openevidence_sidecar_tasks)
    main._openevidence_sidecar_tasks.clear()
    await _request("ALK")
    main._openevidence_sidecar_tasks.update(registry)

    scopes = [tags[1] for tags in statsd.tags_of("openevidence.lookups.deduped")]
    assert scopes.count("scope:in_process") == 2
    assert scopes.count("scope:cross_pod") == 1
    assert len(statsd.named("openevidence.lookups.started")) == 1
    assert upstream.calls == 1
    upstream.release()
    await _wait_for_background_lookups()


async def test_lease_unavailable_fallback_is_counted(fake_redis, statsd, upstream, monkeypatch):
    async def broken_eval(*args, **kwargs):
        raise ConnectionError("redis down")

    monkeypatch.setattr(fake_redis, "eval", broken_eval)
    result, _ = await _request("ALK")

    assert result.status == "pending"
    assert ["model:osler", "op:claim"] in statsd.tags_of("openevidence.lease.unavailable")
    upstream.release()
    await _wait_for_background_lookups()


async def test_warmup_calls_are_tagged_source_warmup(fake_redis, statsd, monkeypatch):
    statuses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, text=_SSE_STREAM if status == 200 else "busy")

    async def fake_normalize_fusions(inputs):
        return {"ALK": (ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True), ["EML4::ALK"])}

    class MockedHttpClient(OpenEvidenceClient):
        async def get_gene_analysis(self, gene, tumor_type=None, fusion=None):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                return await super().get_gene_analysis(gene, tumor_type=tumor_type, fusion=fusion, client=http_client)

    monkeypatch.setattr(openevidence_warmup, "normalize_fusions", fake_normalize_fusions)
    report = await openevidence_warmup.warm_openevidence_cache(
        [FusionInput(fusion="EML4::ALK", tumor_type="NSCLC")], client=MockedHttpClient()
    )

    assert report["genes_warmed"] == 1
    assert statsd.tags_of("openevidence.upstream.calls") == [
        ["model:osler", "status:http_error", "source:warmup", "http_status_class:5xx"],
        ["model:osler", "status:success", "source:warmup", "http_status_class:2xx"],
    ]
    assert statsd.tags_of("openevidence.retries") == [["model:osler", "reason:http_5xx", "source:warmup"]]
    # Warmup never touches the sidecar endpoint's metrics.
    assert statsd.named("openevidence.endpoint.requests") == []
    _assert_low_cardinality(statsd)


async def test_not_cancer_associated_skip_is_counted(fake_redis, statsd, upstream):
    result, _ = await _request("ALK", cancer_associated=False)

    assert result.status == "unavailable"
    assert _outcomes(statsd) == ["not_cancer_associated"]
    assert upstream.calls == 0


async def test_flag_off_response_is_unchanged_and_only_counts_disabled(fake_redis, statsd, upstream, monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", False)

    result, _ = await _request("ALK", tumor_type="NSCLC")

    assert json.loads(result.body) == {"available": False, "distilled": None, "error": None}
    assert result.body == b'{"available":false,"distilled":null,"error":null}'
    assert _outcomes(statsd) == ["disabled"]
    assert {call[1] for call in statsd.oe_calls()} == {
        "openevidence.endpoint.requests",
        "openevidence.endpoint.duration_ms",
    }
    assert upstream.calls == 0
    assert fake_redis.keys_with_prefix("") == []


async def test_nothing_is_emitted_when_datadog_metrics_are_disabled(fake_redis, upstream, monkeypatch):
    fake = FakeStatsd()
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", False)
    monkeypatch.setattr(observability, "_statsd_client", fake)

    first, _ = await _request("ALK")
    upstream.release()
    await _wait_for_background_lookups()
    second, _ = await _request("ALK")

    assert (first.status, second.status) == ("pending", "ready")
    assert fake.calls == []


async def test_a_failing_metrics_client_never_changes_behavior(fake_redis, upstream, monkeypatch):
    class ExplodingStatsd:
        """Raises for every openevidence.* metric (the pre-existing
        redis_cache.lookups metric is left alone — its call site predates this
        change and is out of scope)."""

        def increment(self, metric, *args, **kwargs):
            if metric.startswith("openevidence."):
                raise OSError("statsd socket gone")

        distribution = increment

    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability, "_statsd_client", ExplodingStatsd())

    first, _ = await _request("ALK")
    upstream.release()
    await _wait_for_background_lookups()
    second, _ = await _request("ALK")

    assert (first.status, second.status) == ("pending", "ready")
    assert upstream.calls == 1
