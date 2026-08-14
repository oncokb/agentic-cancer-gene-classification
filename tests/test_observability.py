from contextlib import contextmanager

from fastapi.testclient import TestClient

from src import main
from src.main import app
from src.models.schema import AnnotationResult, GeneAnnotation, ResolvedGene
from src.observability import NoopSpan, record_user_seen, stable_user_key
from src.pipeline import orchestrator


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
    assert ("increment", "pipeline.runs", 1, ["mode:core", "local_backend:sdk"]) in metric_calls
    assert ("increment", "inputs.submitted", 1, ["mode:core", "local_backend:sdk"]) in metric_calls
    assert ("increment", "genes.queried", 2, ["mode:core", "local_backend:sdk"]) in metric_calls
    assert any(call[1] == "pipeline.duration_ms" for call in metric_calls)


def test_annotate_endpoint_records_user_header(monkeypatch):
    seen = {}

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
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
        "tags": ["mode:core", "local_backend:sdk"],
    }
