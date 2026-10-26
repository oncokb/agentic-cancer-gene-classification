"""Tests for the shared async job store (src.job_store) and the owner checks
on every job-status endpoint.

Redis is always the in-test FakeRedis from conftest, never a real server."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import settings
from src.job_store import JobStore
from src.main import (
    AnnotationJobStatusResponse,
    EnrichmentJobStatusResponse,
    FusionEvidenceJobStatusResponse,
    app,
)
from src.models.schema import AnnotationResult, FusionEvidenceResult, GeneAnnotation
from tests.conftest import FakeRedis
from tests.test_api_keys import _bearer, _create_key, _session_client, store  # noqa: F401 (fixture)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _BrokenRedis:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        raise ConnectionError("redis down")

    async def set(self, key, value, ex=None):
        self.calls += 1
        raise ConnectionError("redis down")


def _annotation_job(job_id: str = "job-1", **overrides) -> AnnotationJobStatusResponse:
    data = dict(job_id=job_id, status="running", fusions_processed=2, kind="gene_query", owner="a@mskcc.org")
    data.update(overrides)
    return AnnotationJobStatusResponse(**data)


def _store(redis, **kwargs) -> JobStore:
    return JobStore("annotation", AnnotationJobStatusResponse, redis_client_factory=lambda: redis, **kwargs)


# --- store unit tests -------------------------------------------------------


async def test_round_trip_through_redis_keeps_internal_fields():
    redis = FakeRedis()
    job = _annotation_job(genes_completed=1, annotations=[GeneAnnotation(gene="ALK")])
    await _store(redis).put(job)

    assert redis.keys_with_prefix("job:annotation:") == ["job:annotation:job-1"]
    # A second store instance on the same backend is another pod: it has
    # nothing locally and must read the snapshot from Redis.
    loaded = await _store(redis).get("job-1")
    assert loaded is not None and loaded is not job
    assert loaded.model_dump() == job.model_dump()
    assert (loaded.owner, loaded.kind) == ("a@mskcc.org", "gene_query")
    # ...while the public response shape still hides them.
    assert "owner" not in loaded.model_dump() and "kind" not in loaded.model_dump()


async def test_cross_process_reads_see_latest_progress():
    redis = FakeRedis()
    submitter, poller = _store(redis), _store(redis)
    job = _annotation_job(status="queued")
    await submitter.put(job)
    assert (await poller.get("job-1")).status == "queued"

    job.status = "complete"
    job.genes_completed = 2
    await submitter.put(job)

    polled = await poller.get("job-1")
    assert (polled.status, polled.genes_completed) == ("complete", 2)
    assert await poller.get("missing") is None


async def test_ttl_applied_in_redis_and_locally(monkeypatch):
    monkeypatch.setattr(settings, "job_ttl_seconds", 120)
    redis, clock = FakeRedis(), _Clock()
    submitter = _store(redis, clock=clock)
    await submitter.put(_annotation_job())

    assert redis._store["job:annotation:job-1"][1] == redis.now + 120

    clock.now += 119
    assert await submitter.get("job-1") is not None
    redis.advance(121)
    clock.now += 2
    assert await submitter.get("job-1") is None
    assert "job-1" not in submitter.local


async def test_every_job_kind_defaults_to_24h_ttl():
    assert settings.job_ttl_seconds == 86400
    for store_ in (main._annotation_jobs, main._enrichment_jobs, main._fusion_evidence_jobs):
        assert store_._ttl_seconds() == 86400
    assert {main._annotation_jobs.kind, main._enrichment_jobs.kind, main._fusion_evidence_jobs.kind} == {
        "annotation",
        "enrichment",
        "fusion_evidence",
    }


async def test_writes_refresh_ttl_so_running_jobs_survive(monkeypatch):
    monkeypatch.setattr(settings, "job_ttl_seconds", 100)
    clock = _Clock()
    store_ = _store(None, clock=clock)
    job = _annotation_job()
    await store_.put(job)
    clock.now += 90
    await store_.put(job)  # progress update
    clock.now += 90
    assert await store_.get("job-1") is job


async def test_local_sweep_evicts_expired_jobs(monkeypatch):
    monkeypatch.setattr(settings, "job_ttl_seconds", 10)
    clock = _Clock()
    store_ = _store(None, clock=clock)
    await store_.put(_annotation_job("old"))
    clock.now += 61
    await store_.put(_annotation_job("new"))
    assert set(store_.local) == {"new"}


async def test_without_redis_falls_back_to_process_memory():
    store_ = _store(None)
    job = _annotation_job()
    await store_.put(job)
    assert await store_.get("job-1") is job
    assert await _store(None).get("job-1") is None  # nothing shared without Redis


async def test_redis_errors_fall_back_and_back_off():
    broken = _BrokenRedis()
    clock = _Clock()
    store_ = _store(broken, clock=clock)
    job = _annotation_job()

    await store_.put(job)
    assert await store_.get("job-1") is job
    await store_.put(job)
    assert await store_.get("unknown") is None
    assert broken.calls == 1  # backed off after the first failure

    clock.now += 31
    await store_.put(job)
    assert broken.calls == 2


async def test_unconfigured_redis_url_disables_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "redis_sentinel_enabled", False)
    store_ = JobStore("annotation", AnnotationJobStatusResponse)
    assert store_._redis() is None
    await store_.put(_annotation_job())
    assert (await store_.get("job-1")).job_id == "job-1"


# --- owner enforcement over HTTP -------------------------------------------


@pytest.fixture
def job_backends(monkeypatch):
    """Fake out every job's background work and give the job stores a
    shared FakeRedis, with empty local maps."""

    async def fake_run_pipeline(fusions, **kwargs):
        return AnnotationResult(
            run_id="run-1", timestamp="2026-10-26T00:00:00+00:00", fusions_processed=len(fusions), genes_annotated=0
        )

    async def fake_enrich(annotations, **kwargs):
        return annotations

    async def fake_fusion_evidence(fusion, tumor_type=None):
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main, "enrich_gene_annotations", fake_enrich)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_fusion_evidence)
    monkeypatch.setattr(main, "_persist_run_result", noop)
    monkeypatch.setattr(main, "_save_run_result", noop)
    monkeypatch.setattr(main, "record_user_annotation_activity", noop)
    redis = FakeRedis()
    stores = (main._annotation_jobs, main._enrichment_jobs, main._fusion_evidence_jobs)
    for store_ in stores:
        store_.clear_local()
        monkeypatch.setattr(store_, "_redis_client_factory", lambda: redis)
    yield redis
    for store_ in stores:
        store_.clear_local()


JOB_KINDS = {
    "annotate": ("/v1/annotate/jobs", {"fusions": ["ALK"], "mode": "core"}),
    "enrichment": ("/v1/annotate/enrichment/jobs", {"annotations": [{"gene": "ALK"}]}),
    "gene_query": ("/v1/genes/query/jobs", {"genes": ["ALK"]}),
    "fusion_evidence": ("/v1/fusion-evidence/jobs", {"fusions": [{"fusion": "EML4::ALK"}], "mode": "core"}),
}


@pytest.mark.parametrize("kind", sorted(JOB_KINDS))
def test_job_status_is_owner_only(store, job_backends, kind):  # noqa: F811
    path, body = JOB_KINDS[kind]
    owner = _session_client("Curator@MSKCC.org")
    created = owner.post(path, json=body)
    assert created.status_code == 200, created.text
    status_url = created.json()["status_url"]

    assert owner.get(status_url).status_code == 200
    other = _session_client("someone.else@mskcc.org").get(status_url)
    assert other.status_code == 404
    assert _session_client("boss@mskcc.org", role="admin").get(status_url).status_code == 200

    # Another pod (empty local map) answers from Redis with the same checks.
    for store_ in (main._annotation_jobs, main._enrichment_jobs, main._fusion_evidence_jobs):
        store_.clear_local()
    assert owner.get(status_url).status_code == 200
    assert _session_client("someone.else@mskcc.org").get(status_url).status_code == 404
    assert job_backends.keys_with_prefix("job:")


def test_api_key_jobs_are_owned_by_the_key_owner(store, job_backends):  # noqa: F811
    owner_session = _session_client("curator@mskcc.org")
    key = _create_key(owner_session)["key"]
    other_key = _create_key(_session_client("someone.else@mskcc.org"))["key"]
    path, body = JOB_KINDS["fusion_evidence"]

    created = TestClient(app).post(path, json=body, headers=_bearer(key))
    assert created.status_code == 200, created.text
    status_url = created.json()["status_url"]
    job = main._fusion_evidence_jobs.local[created.json()["job_id"]][0]
    assert job.owner == "curator@mskcc.org"

    assert TestClient(app).get(status_url, headers=_bearer(key)).status_code == 200
    assert owner_session.get(status_url).status_code == 200
    assert TestClient(app).get(status_url, headers=_bearer(other_key)).status_code == 404


def test_local_dev_without_auth_still_reads_its_jobs(job_backends):
    client = TestClient(app)
    created = client.post(*JOB_KINDS["fusion_evidence"][:1], json=JOB_KINDS["fusion_evidence"][1])
    assert created.status_code == 200
    assert client.get(created.json()["status_url"]).status_code == 200


async def test_job_without_owner_is_admin_only(store, job_backends):  # noqa: F811
    await main._store_enrichment_job(EnrichmentJobStatusResponse(job_id="legacy", status="complete"))
    assert _session_client().get("/v1/annotate/enrichment/jobs/legacy").status_code == 404
    admin = _session_client("boss@mskcc.org", role="admin")
    assert admin.get("/v1/annotate/enrichment/jobs/legacy").status_code == 200


async def test_status_response_shape_unchanged(job_backends):
    await main._store_fusion_evidence_job(
        FusionEvidenceJobStatusResponse(job_id="fe-1", status="queued", fusions_total=1, owner="local@internal")
    )
    body = TestClient(app).get("/v1/fusion-evidence/jobs/fe-1").json()
    assert set(body) == {
        "job_id",
        "status",
        "fusions_completed",
        "fusions_total",
        "fusion_evidence",
        "error",
        "timings_ms",
    }
