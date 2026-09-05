from contextlib import contextmanager

from fastapi.testclient import TestClient

from src import main, observability
from src.main import app
from src.models.schema import AnnotationResult, GeneAnnotation, ResolvedGene
from src.observability import NoopSpan, record_user_seen, stable_user_key
from src.pipeline import llm_client, orchestrator


class FakeStatsd:
    def __init__(self):
        self.calls = []

    def increment(self, metric, value=1, tags=None):
        self.calls.append(("increment", metric, value, tags))

    def distribution(self, metric, value, tags=None):
        self.calls.append(("distribution", metric, value, tags))

    def set(self, metric, value, tags=None):
        self.calls.append(("set", metric, value, tags))


@contextmanager
def noop_trace(*args, **kwargs):
    yield NoopSpan()


def test_record_user_seen_hashes_user_identifier(monkeypatch):
    fake_statsd = FakeStatsd()
    monkeypatch.setattr("src.observability.settings.datadog_metrics_enabled", True)
    monkeypatch.setattr("src.observability._statsd_client", fake_statsd)

    record_user_seen("User@Example.com", tags=["mode:core"])

    assert fake_statsd.calls == [
        ("set", "users.active", stable_user_key("User@Example.com"), ["mode:core"])
    ]
    assert "User@Example.com" not in str(fake_statsd.calls)


def test_record_user_seen_counts_anonymous_requests(monkeypatch):
    fake_statsd = FakeStatsd()
    monkeypatch.setattr("src.observability.settings.datadog_metrics_enabled", True)
    monkeypatch.setattr("src.observability._statsd_client", fake_statsd)

    record_user_seen(None, tags=["mode:full"])

    assert fake_statsd.calls == [
        ("increment", "users.anonymous_requests", 1, ["mode:full"])
    ]


async def test_run_pipeline_emits_gene_and_input_metrics(monkeypatch):
    metric_calls = []

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (
                ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True),
                ["BRAF::TP53"],
            ),
            "TP53": (
                ResolvedGene(input_symbol="TP53", canonical_symbol="TP53", resolved=True),
                ["BRAF::TP53"],
            ),
        }

    async def fake_annotate_gene(*, gene, **kwargs):
        return GeneAnnotation(gene=gene, timings_ms={"total": 5.0})

    def fake_increment(metric, value=1, tags=None):
        metric_calls.append(("increment", metric, value, tags))

    def fake_distribution(metric, value, tags=None):
        metric_calls.append(("distribution", metric, value, tags))

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator, "increment", fake_increment)
    monkeypatch.setattr(orchestrator, "distribution", fake_distribution)
    monkeypatch.setattr(orchestrator, "trace", noop_trace)

    result = await orchestrator.run_pipeline(["BRAF::TP53"], mode="core")

    assert result.genes_annotated == 2
    tags = ["mode:core", "local_backend:sdk", "skip_literature_for_oncokb:False"]
    assert ("increment", "pipeline.runs", 1, tags) in metric_calls
    assert ("increment", "inputs.submitted", 1, tags) in metric_calls
    assert ("increment", "genes.queried", 2, tags) in metric_calls
    assert any(call[1] == "pipeline.duration_ms" for call in metric_calls)


async def test_run_pipeline_tags_gene_metrics_with_cache_status_and_fusion(monkeypatch):
    metric_calls = []

    async def fake_normalize_fusions(inputs):
        return {
            "BRAF": (
                ResolvedGene(input_symbol="BRAF", canonical_symbol="BRAF", resolved=True),
                ["BRAF::TP53"],
            ),
            "MYH9": (
                ResolvedGene(input_symbol="MYH9", canonical_symbol="MYH9", resolved=True),
                ["MYH9"],
            ),
        }

    async def fake_annotate_gene(*, gene, fusions, **kwargs):
        # Real _annotate_gene/synthesis code always sets .fusions from the
        # fusions it was called with (see orchestrator.py's own branches) —
        # mirror that contract here rather than leaving it at the model default.
        return GeneAnnotation(
            gene=gene,
            fusions=list(fusions),
            timings_ms={"total": 5.0},
            cache_status="refreshed",
        )

    def fake_increment(metric, value=1, tags=None):
        metric_calls.append(("increment", metric, value, tags))

    def fake_distribution(metric, value, tags=None):
        metric_calls.append(("distribution", metric, value, tags))

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator, "increment", fake_increment)
    monkeypatch.setattr(orchestrator, "distribution", fake_distribution)
    monkeypatch.setattr(orchestrator, "trace", noop_trace)

    await orchestrator.run_pipeline(["BRAF::TP53", "MYH9"], mode="core")

    base_tags = ["mode:core", "local_backend:sdk", "skip_literature_for_oncokb:False"]
    fusion_gene_tags = base_tags + ["cache_status:refreshed", "is_fusion:True"]
    non_fusion_gene_tags = base_tags + ["cache_status:refreshed", "is_fusion:False"]
    assert ("increment", "genes.annotated", 1, fusion_gene_tags) in metric_calls
    assert ("increment", "genes.annotated", 1, non_fusion_gene_tags) in metric_calls
    assert any(
        call[1] == "gene.total_duration_ms" and call[3] == fusion_gene_tags for call in metric_calls
    )
    assert any(
        call[1] == "gene.total_duration_ms" and call[3] == non_fusion_gene_tags
        for call in metric_calls
    )


def test_record_llm_usage_emits_request_count_and_token_distributions(monkeypatch):
    metric_calls = []
    monkeypatch.setattr(
        llm_client, "increment", lambda metric, value=1, tags=None: metric_calls.append(("increment", metric, value, tags))
    )
    monkeypatch.setattr(
        llm_client, "distribution", lambda metric, value, tags=None: metric_calls.append(("distribution", metric, value, tags))
    )

    class FakeUsage:
        input_tokens = 1200
        output_tokens = 340
        cache_creation_input_tokens = 500
        cache_read_input_tokens = 2000

    llm_client.record_llm_usage("claude-haiku-4-5-20251001", "selection", FakeUsage())

    tags = ["model:claude-haiku-4-5-20251001", "model_purpose:selection"]
    assert ("increment", "llm.requests", 1, tags) in metric_calls
    assert ("distribution", "llm.tokens.input", 1200, tags) in metric_calls
    assert ("distribution", "llm.tokens.output", 340, tags) in metric_calls
    assert ("distribution", "llm.tokens.cache_creation", 500, tags) in metric_calls
    assert ("distribution", "llm.tokens.cache_read", 2000, tags) in metric_calls


def test_record_llm_usage_tags_unspecified_purpose_and_handles_missing_usage(monkeypatch):
    metric_calls = []
    monkeypatch.setattr(
        llm_client, "increment", lambda metric, value=1, tags=None: metric_calls.append(("increment", metric, value, tags))
    )
    monkeypatch.setattr(
        llm_client, "distribution", lambda metric, value, tags=None: metric_calls.append(("distribution", metric, value, tags))
    )

    llm_client.record_llm_usage("claude-opus-4-7", "", None)

    tags = ["model:claude-opus-4-7", "model_purpose:unspecified"]
    assert metric_calls == [("increment", "llm.requests", 1, tags)]


async def test_complete_sdk_records_llm_usage(monkeypatch):
    recorded = {}
    monkeypatch.setattr(
        llm_client, "record_llm_usage", lambda model, purpose, usage: recorded.update(
            model=model, purpose=purpose, usage=usage
        )
    )

    class FakeUsage:
        input_tokens = 10
        output_tokens = 5
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0

    class FakeToolUseBlock:
        type = "tool_use"
        name = "draft_feedback_issue"
        input = {"title": "ok"}

    class FakeResponse:
        content = [FakeToolUseBlock()]
        usage = FakeUsage()

    class FakeMessages:
        async def create(self, **kwargs):
            return FakeResponse()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm_client, "make_async_sdk_client", lambda: FakeClient())

    result = await llm_client._complete_sdk(
        model="claude-haiku-4-5-20251001",
        system="sys",
        user="user",
        tool={"name": "draft_feedback_issue"},
        max_tokens=100,
        model_purpose="selection",
    )

    assert result == {"title": "ok"}
    assert recorded == {"model": "claude-haiku-4-5-20251001", "purpose": "selection", "usage": FakeResponse.usage}


def test_statsd_omits_host_port_when_unconfigured(monkeypatch):
    """DogStatsd must fall through to its own DD_DOGSTATSD_URL/DD_AGENT_HOST
    detection (the cluster's injected Unix socket) rather than being pinned
    to a UDP host:port that doesn't exist in the pod."""
    captured = {}

    class FakeDogStatsd:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(observability, "_statsd_client", None)
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability.settings, "datadog_statsd_host", "")
    monkeypatch.setattr(observability.settings, "datadog_metrics_namespace", "acgc")
    monkeypatch.setitem(
        __import__("sys").modules,
        "datadog",
        type("_module", (), {"DogStatsd": FakeDogStatsd}),
    )

    observability._statsd()

    assert captured == {"namespace": "acgc"}
    assert "host" not in captured
    assert "port" not in captured


def test_statsd_uses_configured_host_port_when_set(monkeypatch):
    captured = {}

    class FakeDogStatsd:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(observability, "_statsd_client", None)
    monkeypatch.setattr(observability.settings, "datadog_metrics_enabled", True)
    monkeypatch.setattr(observability.settings, "datadog_statsd_host", "10.0.0.5")
    monkeypatch.setattr(observability.settings, "datadog_statsd_port", 9125)
    monkeypatch.setattr(observability.settings, "datadog_metrics_namespace", "acgc")
    monkeypatch.setitem(
        __import__("sys").modules,
        "datadog",
        type("_module", (), {"DogStatsd": FakeDogStatsd}),
    )

    observability._statsd()

    assert captured == {"namespace": "acgc", "host": "10.0.0.5", "port": 9125}


def test_annotate_endpoint_records_user_header(monkeypatch):
    seen = {}

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        skip_literature_for_oncokb=False,
        mode="full",
    ):
        return AnnotationResult(
            run_id="run-1",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="BRAF")],
            timings_ms={"total": 2.0},
        )

    async def fake_persist_run_result(http_request, request_payload, result):
        return None

    def fake_record_user_seen(user_id, tags=None):
        seen["user_id"] = user_id
        seen["tags"] = tags

    app.state.run_store = None
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main, "_persist_run_result", fake_persist_run_result)
    monkeypatch.setattr(main, "record_user_seen", fake_record_user_seen)
    client = TestClient(app)

    response = client.post(
        "/v1/annotate",
        json={"fusions": ["BRAF::TP53"], "mode": "core"},
        headers={"x-user-id": "curator@example.com"},
    )

    assert response.status_code == 200
    assert seen == {
        "user_id": "curator@example.com",
        "tags": ["mode:core", "local_backend:sdk", "skip_literature_for_oncokb:True"],
    }
