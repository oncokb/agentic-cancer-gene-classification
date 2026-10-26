"""Tests for the shared async job store (src.job_store) and the owner checks
on every job-status endpoint.

Redis is always an in-test FakeRedis: the module is marked `no_real_redis`
(so conftest never builds or flushes a client for the configured Redis), and
`_no_real_redis_clients` fails any test that tries to build one anyway."""

from __future__ import annotations

import asyncio
import logging
import time

import pytest
from fastapi.testclient import TestClient

from src import job_store as job_store_module
from src import main
from src.config import settings
from src.job_store import REDIS_RETRY_BACKOFF_SECONDS, JobStore
from src.main import (
    AnnotationJobStatusResponse,
    EnrichmentJobStatusResponse,
    FusionEvidenceJobStatusResponse,
    app,
)
from src.models.schema import AnnotationResult, FusionEvidenceResult, GeneAnnotation
from tests.conftest import FakeRedis
from tests.test_api_keys import _bearer, _create_key, _session_client, store  # noqa: F401 (fixture)

pytestmark = pytest.mark.no_real_redis


@pytest.fixture(autouse=True)
def _no_real_redis_clients(monkeypatch):
    import redis.asyncio
    from redis.asyncio import sentinel

    attempts = []

    def refuse(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("test tried to build a real Redis client")

    monkeypatch.setattr(redis.asyncio, "from_url", refuse)
    monkeypatch.setattr(sentinel, "Sentinel", refuse)
    yield
    assert attempts == []


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _FlakyRedis(FakeRedis):
    """FakeRedis that can be switched off; counts every command."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.calls = 0

    async def _check(self) -> None:
        self.calls += 1
        await asyncio.sleep(0)  # let concurrent writers pile up on the lock
        if self.down:
            raise ConnectionError("redis down")

    async def get(self, key):
        await self._check()
        return await super().get(key)

    async def set(self, key, value, ex=None, px=None, nx=False):
        await self._check()
        return await super().set(key, value, ex=ex, px=px, nx=nx)


def _annotation_job(job_id: str = "job-1", **overrides) -> AnnotationJobStatusResponse:
    data = dict(job_id=job_id, status="running", fusions_processed=2, kind="gene_query", owner="a@mskcc.org")
    data.update(overrides)
    return AnnotationJobStatusResponse(**data)


def _store(redis, **kwargs) -> JobStore:
    return JobStore("annotation", AnnotationJobStatusResponse, redis_client_factory=lambda: redis, **kwargs)


# --- store unit tests -------------------------------------------------------


async def test_round_trip_through_redis_keeps_internal_fields():
    redis = FakeRedis()
    result = AnnotationResult(
        run_id="run-1",
        timestamp="2026-10-26T00:00:00+00:00",
        fusions_processed=1,
        genes_annotated=1,
        annotations=[GeneAnnotation(gene="ALK", evidence_support_score=0.9)],
    )
    job = _annotation_job(status="complete", genes_completed=1, annotations=result.annotations, result=result)
    await _store(redis).put(job)

    # Gene-query jobs are annotation jobs, stored under the annotation prefix.
    assert redis.keys_with_prefix("job:") == ["job:annotation:job-1"]
    # A second store instance on the same backend is another pod: it has
    # nothing locally and must read the snapshot from Redis.
    loaded = await _store(redis).get("job-1")
    assert loaded is not None and loaded is not job
    assert loaded.model_dump() == job.model_dump()
    assert loaded.result.annotations[0].evidence_support_score == 0.9
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


async def _fail_running_then_complete_during_backoff(redis, clock):
    """Persist `queued`; fail the `running` write; finish during the back-off;
    then restore Redis and move past the back-off. Until the submitter
    republishes, another pod still sees `queued`."""
    submitter, poller = _store(redis, clock=clock), _store(redis, clock=clock)
    job = _annotation_job(status="queued")
    await submitter.put(job)
    redis.down = True
    job.status = "running"
    await submitter.put(job)
    calls_after_failure = redis.calls
    job.status = "complete"
    job.genes_completed = 2
    await submitter.put(job)
    assert redis.calls == calls_after_failure  # backing off: no attempt
    redis.down = False
    clock.now += REDIS_RETRY_BACKOFF_SECONDS + 1
    assert (await poller.get("job-1")).status == "queued"
    return submitter, poller


async def test_snapshot_written_during_outage_is_republished_by_retry_timer():
    redis, clock = _FlakyRedis(), _Clock()
    submitter, poller = await _fail_running_then_complete_during_backoff(redis, clock)

    assert submitter._retry_timer is not None
    loop, handle = submitter._retry_timer
    assert loop is asyncio.get_running_loop()
    assert handle.when() - loop.time() > REDIS_RETRY_BACKOFF_SECONDS - 1
    # Fire the timer now instead of waiting 31 real seconds.
    handle.cancel()
    submitter._on_retry_timer()
    await asyncio.gather(*submitter._retry_tasks)

    polled = await poller.get("job-1")
    assert (polled.status, polled.genes_completed) == ("complete", 2)
    assert submitter._dirty == {}


async def test_snapshot_written_during_outage_is_republished_on_next_read():
    redis, clock = _FlakyRedis(), _Clock()
    submitter, poller = await _fail_running_then_complete_during_backoff(redis, clock)
    submitter._retry_timer[1].cancel()

    assert (await submitter.get("job-1")).status == "complete"

    polled = await poller.get("job-1")
    assert (polled.status, polled.genes_completed) == ("complete", 2)


async def test_snapshot_written_during_outage_is_republished_on_next_write():
    redis, clock = _FlakyRedis(), _Clock()
    submitter, poller = _store(redis, clock=clock), _store(redis, clock=clock)
    job = _annotation_job(status="queued")
    await submitter.put(job)
    redis.down = True
    job.status = "failed"
    await submitter.put(job)
    redis.down = False
    clock.now += REDIS_RETRY_BACKOFF_SECONDS + 1

    await submitter.put(_annotation_job("job-2"))  # unrelated job

    assert (await poller.get("job-1")).status == "failed"
    assert (await poller.get("job-2")).status == "running"


async def test_concurrent_writes_to_a_dead_redis_try_it_once():
    redis = _FlakyRedis()
    redis.down = True
    store_ = _store(redis, clock=_Clock())

    await asyncio.gather(*(store_.put(_annotation_job(f"job-{i}")) for i in range(10)))

    assert redis.calls == 1
    assert len(store_.local) == 10
    assert set(store_._dirty) == {f"job-{i}" for i in range(10)}
    for i in range(10):
        assert (await store_.get(f"job-{i}")).job_id == f"job-{i}"


async def test_ttl_applied_in_redis_and_locally(monkeypatch):
    monkeypatch.setattr(settings, "job_ttl_seconds", 120)
    redis, clock = FakeRedis(), _Clock()
    submitter = _store(redis, clock=clock)
    await submitter.put(_annotation_job(status="complete"))

    assert redis._store["job:annotation:job-1"][1] == redis.now + 120

    clock.now += 119
    assert await submitter.get("job-1") is not None
    redis.advance(121)
    clock.now += 2
    assert await submitter.get("job-1") is None
    assert "job-1" not in submitter.local


async def test_every_job_kind_defaults_to_24h_ttl():
    assert settings.job_ttl_seconds is None
    for store_ in (main._annotation_jobs, main._enrichment_jobs, main._fusion_evidence_jobs):
        assert store_._ttl_seconds() == 86400
    assert {main._annotation_jobs.kind, main._enrichment_jobs.kind, main._fusion_evidence_jobs.kind} == {
        "annotation",
        "enrichment",
        "fusion_evidence",
    }


def test_legacy_annotation_job_ttl_is_a_deprecated_fallback(monkeypatch, caplog):
    monkeypatch.setattr(job_store_module, "_warned_legacy_ttl", False)
    monkeypatch.setattr(settings, "annotation_job_ttl_seconds", 3600)
    with caplog.at_level(logging.WARNING, logger="src.job_store"):
        assert job_store_module.job_ttl_seconds() == 3600
        assert job_store_module.job_ttl_seconds() == 3600
    assert [r.message for r in caplog.records if "ANNOTATION_JOB_TTL_SECONDS" in r.message] != []
    assert len([r for r in caplog.records if "deprecated" in r.message]) == 1

    monkeypatch.setattr(settings, "job_ttl_seconds", 600)
    assert job_store_module.job_ttl_seconds() == 600  # JOB_TTL_SECONDS wins


async def test_running_jobs_survive_a_progress_gap_longer_than_the_ttl(monkeypatch):
    monkeypatch.setattr(settings, "job_ttl_seconds", 100)
    redis, clock = FakeRedis(), _Clock()
    submitter, poller = _store(redis, clock=clock), _store(redis, clock=clock)
    job = _annotation_job(status="running")
    await submitter.put(job)

    # A periodic read on the submitting pod refreshes the stalled job's TTL...
    clock.now += 70
    redis.advance(70)
    await submitter.get("some-other-job")
    redis.advance(60)  # past the original 100s Redis TTL
    assert (await poller.get("job-1")).status == "running"

    # ...and the worker's own lookup never 404s, however long the gap.
    clock.now += 10_000
    assert await submitter.get("job-1") is job


async def test_expired_finished_jobs_are_swept_on_reads(monkeypatch):
    monkeypatch.setattr(settings, "job_ttl_seconds", 10)
    clock = _Clock()
    store_ = _store(None, clock=clock)
    await store_.put(_annotation_job("old", status="complete"))
    await store_.put(_annotation_job("running", status="running"))
    clock.now += 61
    assert await store_.get("unrelated") is None
    assert set(store_.local) == {"running"}


async def test_without_redis_falls_back_to_process_memory():
    store_ = _store(None)
    job = _annotation_job()
    await store_.put(job)
    assert await store_.get("job-1") is job
    assert await _store(None).get("job-1") is None  # nothing shared without Redis
    assert store_._retry_timer is None


async def test_redis_errors_fall_back_and_back_off():
    redis = _FlakyRedis()
    redis.down = True
    clock = _Clock()
    store_ = _store(redis, clock=clock)
    job = _annotation_job()

    await store_.put(job)
    assert await store_.get("job-1") is job
    await store_.put(job)
    assert await store_.get("unknown") is None
    assert redis.calls == 1  # backed off after the first failure

    clock.now += REDIS_RETRY_BACKOFF_SECONDS + 1
    await store_.put(job)
    assert redis.calls == 2


async def test_unconfigured_redis_url_disables_redis(monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "redis_sentinel_enabled", False)
    store_ = JobStore("annotation", AnnotationJobStatusResponse)
    assert store_._redis() is None
    await store_.put(_annotation_job())
    assert (await store_.get("job-1")).job_id == "job-1"


# --- owner enforcement over HTTP -------------------------------------------


_STORES = (main._annotation_jobs, main._enrichment_jobs, main._fusion_evidence_jobs)


@pytest.fixture
def job_backends(monkeypatch):
    """Fake out every job's background work and give the job stores a
    shared FakeRedis, with empty local maps."""

    async def fake_run_pipeline(fusions, on_annotation=None, on_total_known=None, **kwargs):
        annotations = [GeneAnnotation(gene="ALK", evidence_support_score=0.8)]
        if on_total_known:
            await on_total_known(len(annotations))
        for annotation in annotations:
            if on_annotation:
                await on_annotation(annotation)
        return AnnotationResult(
            run_id="run-1",
            timestamp="2026-10-26T00:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=len(annotations),
            annotations=annotations,
            timings_ms={"total": 1.0},
        )

    async def fake_enrich(annotations, on_annotation=None, **kwargs):
        for annotation in annotations:
            if on_annotation:
                await on_annotation(annotation)
        return annotations

    async def fake_fusion_evidence(fusion, tumor_type=None):
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, well_supported=True)

    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main, "enrich_gene_annotations", fake_enrich)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_fusion_evidence)
    monkeypatch.setattr(main, "_persist_run_result", noop)
    monkeypatch.setattr(main, "_save_run_result", noop)
    monkeypatch.setattr(main, "record_user_annotation_activity", noop)
    monkeypatch.setattr(settings, "public_app_base_url", "https://acgc.example.org/")
    redis = FakeRedis()
    for store_ in _STORES:
        store_.clear_local()
        monkeypatch.setattr(store_, "_redis_client_factory", lambda: redis)
    yield redis
    for store_ in _STORES:
        store_.clear_local()


JOB_KINDS = {
    "annotate": ("/v1/annotate/jobs", {"fusions": ["ALK"], "mode": "core"}),
    "enrichment": ("/v1/annotate/enrichment/jobs", {"annotations": [{"gene": "ALK"}]}),
    "gene_query": ("/v1/genes/query/jobs", {"genes": ["ALK"]}),
    "fusion_evidence": ("/v1/fusion-evidence/jobs", {"fusions": [{"fusion": "EML4::ALK"}], "mode": "core"}),
}


def _nested_result(kind: str, body: dict):
    """The deepest per-kind payload, to prove nested results survive Redis."""
    if kind == "annotate":
        return body["result"]["annotations"][0]["evidence_support_score"]
    if kind == "enrichment":
        return body["annotations"][0]["gene"]
    if kind == "gene_query":
        return (body["results"][0]["gene"], body["run_id"])
    return body["fusion_evidence"][0]["well_supported"]


def _poll_until_complete(client: TestClient, status_url: str) -> dict:
    body = {}
    for _ in range(50):
        response = client.get(status_url)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in ("complete", "failed"):
            break
        time.sleep(0.01)
    assert body["status"] == "complete", body
    return body


@pytest.mark.parametrize("kind", sorted(JOB_KINDS))
def test_job_status_is_owner_only_and_served_from_redis(store, job_backends, kind):  # noqa: F811
    path, body = JOB_KINDS[kind]
    owner = _session_client("Curator@MSKCC.org")
    created = owner.post(path, json=body)
    assert created.status_code == 200, created.text
    job_id, status_url = created.json()["job_id"], created.json()["status_url"]

    local = _poll_until_complete(owner, status_url)
    assert "owner" not in local and "kind" not in local
    expected = _nested_result(kind, local)

    # Mixed-case sign-in is normalised: stored owner is lowercased, and the
    # same user under any casing can read it.
    stored = next(s.local[job_id][0] for s in _STORES if job_id in s.local)
    assert stored.owner == "curator@mskcc.org"
    assert _session_client("curator@mskcc.org").get(status_url).status_code == 200

    assert _session_client("someone.else@mskcc.org").get(status_url).status_code == 404
    assert _session_client("boss@mskcc.org", role="admin").get(status_url).status_code == 200

    # Another pod (empty local map) answers from Redis, with the full nested
    # result reconstructed and the same owner checks.
    for store_ in _STORES:
        store_.clear_local()
    remote = owner.get(status_url)
    assert remote.status_code == 200
    assert remote.json() == local
    assert _nested_result(kind, remote.json()) == expected
    assert "owner" not in remote.json()
    assert _session_client("someone.else@mskcc.org").get(status_url).status_code == 404

    store_kind = {"annotate": "annotation", "gene_query": "annotation"}.get(kind, kind)
    assert job_backends.keys_with_prefix("job:") == [f"job:{store_kind}:{job_id}"]


def test_redis_snapshot_rebuilds_on_a_separate_store_instance(store, job_backends):  # noqa: F811
    owner = _session_client()
    created = owner.post(*JOB_KINDS["annotate"][:1], json=JOB_KINDS["annotate"][1])
    _poll_until_complete(owner, created.json()["status_url"])

    other_pod = JobStore("annotation", AnnotationJobStatusResponse, redis_client_factory=lambda: job_backends)
    job = asyncio.run(other_pod.get(created.json()["job_id"]))
    assert job.status == "complete" and job.owner == "curator@mskcc.org"
    assert isinstance(job.result, AnnotationResult)
    assert job.result.annotations[0].gene == "ALK"
    assert job.result.annotations[0].evidence_support_score == 0.8


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


def test_dev_mode_does_not_grant_job_access(store, job_backends, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "agcg_dev_mode", True)
    owner = _session_client()
    created = owner.post(*JOB_KINDS["fusion_evidence"][:1], json=JOB_KINDS["fusion_evidence"][1])
    status_url = created.json()["status_url"]
    assert owner.get(status_url).status_code == 200
    assert _session_client("someone.else@mskcc.org").get(status_url).status_code == 404


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
