"""Expression-context API with the flag on: job flow, surfaced/excluded logic,
verdict reuse, gene resolution, 404/422/502 paths and history pagination.

The artifact contract is served by pytest-httpx from tests/fixtures/
expression_context/, the literature gate is a fake, and the store is the
module's in-memory implementation, so nothing needs MySQL, Redis or an LLM.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import AuthenticatedUser, require_auth
from src.expression_context import register_expression_context
from src.expression_context.artifact_client import ArtifactClient
from src.expression_context.models import JobRecord, StoredPair
from src.expression_context.service import ExpressionContextService
from src.expression_context.settings import ExpressionContextSettings
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.expression_context_helpers import (
    BASE_URL,
    RELEASE_ID,
    FakeLiteratureGate,
    LeaseFakeRedis,
    add_artifacts,
    gene_file,
    manifest,
    module_redis,
    url,
)
from src.expression_context.lease import VerdictLease

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

PREFIX = "/v1/expression-context"
ALICE = AuthenticatedUser(email="alice@example.org", name="Alice", domain="example.org")
BOB = AuthenticatedUser(email="bob@example.org", name="Bob", domain="example.org")


class Harness:
    def __init__(self, gate=None, store=None):
        self.gate = gate or FakeLiteratureGate()
        self.store = store or InMemoryExpressionContextStore()
        self.user = ALICE
        self.redis = LeaseFakeRedis()
        self.app = FastAPI()
        settings = ExpressionContextSettings(
            enabled=True, artifact_base_url=BASE_URL, http_retry_backoff_seconds=0
        )

        async def factory(app, s):
            return ExpressionContextService(
                artifacts=ArtifactClient(s, redis=module_redis(self.redis)),
                gate=self.gate,
                store=self.store,
                jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
                lease=VerdictLease(ttl_seconds=5, redis=module_redis(self.redis)),
                lease_poll_seconds=0.01,
            )

        assert register_expression_context(self.app, settings=settings, service_factory=factory)
        self.app.dependency_overrides[require_auth] = lambda: self.user


@pytest.fixture
def harness():
    return Harness()


@pytest.fixture
def client(harness):
    with TestClient(harness.app) as test_client:
        yield test_client


def wait_for_job(client, status_url: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(status_url)
        assert response.status_code == 200, response.text
        body = response.json()
        if body["status"] in ("succeeded", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def run_job(client, gene: str = "ERBB2") -> dict:
    response = client.post(f"{PREFIX}/jobs", json={"gene": gene})
    assert response.status_code == 202, response.text
    accepted = response.json()
    assert accepted["status_url"] == f"{PREFIX}/jobs/{accepted['job_id']}"
    return wait_for_job(client, accepted["status_url"])


def _by_code(pairs):
    return {pair["oncotree_code"]: pair for pair in pairs}


def test_job_flow_surfaces_only_stats_and_literature_supported_pairs(httpx_mock, harness, client):
    add_artifacts(httpx_mock)
    job = run_job(client)

    assert job["status"] == "succeeded"
    assert job["hugo_symbol"] == "ERBB2" and job["release_id"] == RELEASE_ID
    assert "owner" not in job
    result = job["result"]
    assert [pair["oncotree_code"] for pair in result["surfaced"]] == ["BRCA"]
    brca = result["surfaced"][0]
    assert brca["cancer_type_name"] == "Breast Invasive Carcinoma"
    assert brca["study_id"] == "brca_tcga_pan_can_atlas_2018"
    # Stats and citations side by side.
    assert brca["stats"]["log2fc"] == 1.5 and brca["stats"]["q_value"] == 1e-39
    assert brca["stats"]["flags"] == ["tissue_of_origin_risk"]
    assert brca["literature"]["pmids"] == ["12345678", "23456789"]
    assert brca["literature"]["supported"] is True
    assert brca["literature"]["rationale"] == "ERBB2 in breast cancer"

    excluded = _by_code(result["excluded"])
    assert {code: pair["exclusion_reason"] for code, pair in excluded.items()} == {
        "STAD": "literature",  # insufficient_evidence despite a PMID
        "ESCA": "literature",  # not insufficient, but zero cited PMIDs
        "UCEC": "precheck",
        "LUAD": "stats",
    }
    # The fake gate claims "supported" for everything; the service recomputes.
    assert excluded["STAD"]["literature"]["supported"] is False
    assert excluded["ESCA"]["literature"]["supported"] is False
    assert excluded["UCEC"]["literature"] is None

    # Only stats_pass && literature_precheck_pass pairs reached the gate,
    # each with the manifest's tumor_type_query.
    assert sorted(harness.gate.calls) == [
        ("ERBB2", "breast cancer"),
        ("ERBB2", "esophageal cancer"),
        ("ERBB2", "gastric cancer"),
    ]
    assert job["literature_evaluated"] == 3 and job["literature_reused"] == 0
    assert job["pair_errors"] == []

    stored = harness.store.rows
    assert len(stored) == 5
    assert all(row.requested_by == "alice@example.org" for row in stored.values())
    assert all(row.literature_gate == "acgc_pipeline_v1" for row in stored.values())


def test_get_gene_returns_surfaced_by_default(httpx_mock, client):
    add_artifacts(httpx_mock)
    run_job(client)

    default = client.get(f"{PREFIX}/genes/erbb2")
    assert default.status_code == 200, default.text
    body = default.json()
    assert body["hugo_symbol"] == "ERBB2"
    assert [pair["oncotree_code"] for pair in body["surfaced"]] == ["BRCA"]
    assert body["excluded"] is None

    full = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"})
    reasons = {pair["oncotree_code"]: pair["exclusion_reason"] for pair in full.json()["excluded"]}
    assert reasons == {"ESCA": "literature", "LUAD": "stats", "STAD": "literature", "UCEC": "precheck"}


def test_stored_verdicts_are_reused(httpx_mock, harness, client):
    add_artifacts(httpx_mock)
    first = run_job(client)
    calls_after_first = list(harness.gate.calls)

    second = run_job(client)
    assert harness.gate.calls == calls_after_first
    assert second["literature_evaluated"] == 0 and second["literature_reused"] == 3
    assert second["result"]["surfaced"][0]["literature"] == first["result"]["surfaced"][0]["literature"]


def test_verdicts_from_another_release_or_gate_are_not_reused(httpx_mock, harness, client):
    add_artifacts(httpx_mock)
    now = datetime.now(timezone.utc)
    evidence = {
        "literature_gate": "acgc_pipeline_v1", "tumor_type_query": "breast cancer",
        "supported": True, "insufficient_evidence": False, "pmids": ["1"],
        "evaluated_at": now.isoformat(),
    }
    stats = gene_file()["cancer_types"][0]
    for release_id, gate in (("older-release", "acgc_pipeline_v1"), (RELEASE_ID, "lras_v1")):
        harness.store.rows[("ERBB2", "BRCA", release_id, gate)] = StoredPair(
            hugo_symbol="ERBB2", oncotree_code="BRCA", release_id=release_id,
            literature_gate=gate, surfaced=True, stats=stats, evidence=evidence,
            computed_at=now - timedelta(days=1),
        )
    job = run_job(client)
    assert ("ERBB2", "breast cancer") in harness.gate.calls
    assert job["literature_reused"] == 0


def test_gate_failure_is_reported_and_retried_next_run(httpx_mock, harness, client):
    add_artifacts(httpx_mock)
    harness.gate.fail_for = {"gastric cancer"}
    job = run_job(client)
    assert job["status"] == "succeeded"
    assert [err["oncotree_code"] for err in job["pair_errors"]] == ["STAD"]
    assert ("ERBB2", "STAD", RELEASE_ID, "acgc_pipeline_v1") not in harness.store.rows

    harness.gate.fail_for = set()
    harness.gate.calls.clear()
    again = run_job(client)
    assert harness.gate.calls == [("ERBB2", "gastric cancer")]
    assert again["literature_reused"] == 2 and again["pair_errors"] == []


def test_job_fails_cleanly_when_store_write_fails(httpx_mock, harness, client):
    add_artifacts(httpx_mock)

    async def broken(pairs):
        raise RuntimeError("db down")

    harness.store.upsert_pairs = broken
    job = run_job(client)
    assert job["status"] == "failed" and "db down" in job["error"]
    assert job["finished_at"] is not None


def test_gene_resolved_through_aliases(httpx_mock, client):
    add_artifacts(httpx_mock)
    httpx_mock.add_response(url=url("genes/HER2.json"), status_code=404, is_reusable=True)
    job = run_job(client, "her2")
    assert job["gene"] == "her2" and job["hugo_symbol"] == "ERBB2"
    # Reads also resolve through aliases.
    assert client.get(f"{PREFIX}/genes/HER2").json()["hugo_symbol"] == "ERBB2"


def test_unknown_gene_is_404(httpx_mock, harness, client):
    add_artifacts(httpx_mock)
    httpx_mock.add_response(url=url("genes/NOPE1.json"), status_code=404)
    response = client.post(f"{PREFIX}/jobs", json={"gene": "NOPE1"})
    assert response.status_code == 404
    assert "NOPE1" in response.json()["detail"]
    assert harness.gate.calls == []


def test_gene_never_run_is_404(httpx_mock, client):
    add_artifacts(httpx_mock)
    assert client.get(f"{PREFIX}/genes/ERBB2").status_code == 404
    assert client.get(f"{PREFIX}/genes/NOPE1").status_code == 404


@pytest.mark.parametrize(
    "body",
    [{}, {"gene": ""}, {"gene": "../etc/passwd"}, {"gene": "A" * 65}, {"gene": "ERBB2", "x": 1}],
)
def test_invalid_job_request_is_422(client, body):
    assert client.post(f"{PREFIX}/jobs", json=body).status_code == 422


def test_invalid_gene_path_is_422(client):
    assert client.get(f"{PREFIX}/genes/BAD$SYMBOL").status_code == 422


def test_unknown_schema_version_is_502(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), json=manifest(schema_version="2"))
    response = client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"})
    assert response.status_code == 502
    assert "schema_version" in response.json()["detail"]


def test_artifact_outage_is_502(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), status_code=503, is_reusable=True)
    assert client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).status_code == 502


def test_job_visible_only_to_owner_or_admin(httpx_mock, harness, client):
    add_artifacts(httpx_mock)
    accepted = client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).json()
    wait_for_job(client, accepted["status_url"])

    harness.user = BOB
    assert client.get(accepted["status_url"]).status_code == 404
    harness.user = BOB.model_copy(update={"role": "admin"})
    assert client.get(accepted["status_url"]).status_code == 200
    assert client.get(f"{PREFIX}/jobs/does-not-exist").status_code == 404


def _seed_history(store, symbols_minutes_ago):
    now = datetime.now(timezone.utc)
    stats = gene_file()["cancer_types"][0]
    for symbol, minutes_ago, surfaced in symbols_minutes_ago:
        for code in ("BRCA", "STAD"):
            store.rows[(symbol, code, RELEASE_ID, "acgc_pipeline_v1")] = StoredPair(
                hugo_symbol=symbol, oncotree_code=code, release_id=RELEASE_ID,
                literature_gate="acgc_pipeline_v1", surfaced=surfaced and code == "BRCA",
                exclusion_reason=None if surfaced and code == "BRCA" else "stats",
                stats={**stats, "oncotree_code": code},
                computed_at=now - timedelta(minutes=minutes_ago),
            )


def test_history_pagination(harness, client):
    _seed_history(harness.store, [("ERBB2", 1, True), ("EGFR", 2, False), ("KRAS", 3, True)])
    # A different gate's rows never show up.
    _seed_history_other = StoredPair(
        hugo_symbol="TP53", oncotree_code="BRCA", release_id=RELEASE_ID, literature_gate="lras_v1",
        surfaced=False, exclusion_reason="stats", stats=gene_file()["cancer_types"][0],
        computed_at=datetime.now(timezone.utc),
    )
    harness.store.rows[("TP53", "BRCA", RELEASE_ID, "lras_v1")] = _seed_history_other

    first = client.get(f"{PREFIX}/genes", params={"limit": 2}).json()
    assert [item["hugo_symbol"] for item in first["items"]] == ["ERBB2", "EGFR"]
    assert first["items"][0]["pair_count"] == 2 and first["items"][0]["surfaced_count"] == 1
    assert first["items"][1]["surfaced_count"] == 0
    assert first["next_cursor"]

    second = client.get(f"{PREFIX}/genes", params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [item["hugo_symbol"] for item in second["items"]] == ["KRAS"]
    assert second["next_cursor"] is None

    everything = client.get(f"{PREFIX}/genes").json()
    assert len(everything["items"]) == 3 and everything["next_cursor"] is None


@pytest.mark.parametrize("params", [{"cursor": "not-a-cursor"}, {"limit": 0}, {"limit": 101}])
def test_history_bad_params_are_422(client, params):
    assert client.get(f"{PREFIX}/genes", params=params).status_code == 422


def test_history_empty(client):
    assert client.get(f"{PREFIX}/genes").json() == {"items": [], "next_cursor": None}


def test_routes_require_auth(monkeypatch, harness):
    from src.config import settings

    harness.app.dependency_overrides.clear()
    monkeypatch.setattr(settings, "auth_enabled", True)
    with TestClient(harness.app) as client:
        assert client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).status_code == 401
        assert client.get(f"{PREFIX}/jobs/x").status_code == 401
        assert client.get(f"{PREFIX}/genes/ERBB2").status_code == 401
        assert client.get(f"{PREFIX}/genes").status_code == 401


def test_service_closed_on_shutdown(harness):
    closed = []
    with TestClient(harness.app) as client:
        service = client.app.state.expression_context
        original = service.store.close

        async def close():
            closed.append(True)
            await original()

        service.store.close = close
    assert closed == [True]
    assert harness.app.state.expression_context is None


def test_alias_lookup_during_artifact_outage_is_502(httpx_mock, client):
    # Nothing stored for HER2, so the read resolves the alias -> artifact is down.
    httpx_mock.add_response(url=url("manifest.json"), status_code=503, is_reusable=True)
    response = client.get(f"{PREFIX}/genes/HER2")
    assert response.status_code == 502
    assert "unavailable" in response.json()["detail"]


def test_alias_lookup_contract_violation_is_502(httpx_mock, client):
    httpx_mock.add_response(url=url("manifest.json"), json=manifest(schema_version="2"))
    assert client.get(f"{PREFIX}/genes/HER2").status_code == 502


async def test_running_status_write_failure_marks_job_failed(httpx_mock):
    add_artifacts(httpx_mock)

    class FlakyJobs:
        def __init__(self):
            self.jobs = {}

        async def put(self, job):
            if job.status == "running":
                raise RuntimeError("job store down")
            self.jobs[job.job_id] = job.model_copy(deep=True)

        async def get(self, job_id):
            return self.jobs.get(job_id)

    settings = ExpressionContextSettings(
        enabled=True, artifact_base_url=BASE_URL, http_retry_backoff_seconds=0
    )
    gate = FakeLiteratureGate()
    redis = LeaseFakeRedis()
    service = ExpressionContextService(
        artifacts=ArtifactClient(settings, redis=module_redis(redis)), gate=gate,
        store=InMemoryExpressionContextStore(), jobs=FlakyJobs(),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
    )
    job = await service.submit("ERBB2", owner="alice@example.org")
    await asyncio.gather(*service._tasks)
    stored = await service.jobs.get(job.job_id)
    assert stored.status == "failed" and "job store down" in stored.error
    assert stored.finished_at is not None and gate.calls == []
    await service.aclose()
