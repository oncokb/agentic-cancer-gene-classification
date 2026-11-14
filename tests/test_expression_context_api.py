"""Expression-context API with the flag on: job flow, surfaced/excluded logic,
verdict reuse, gene resolution, the per-gene stats view, bundle status,
404/422/503 paths and history pagination.

The fixture bundle (tests/fixtures/expression_context/) is seeded straight
into the module's in-memory bundle store (the loader itself is covered by
test_expression_context_bundle.py), the literature gate is a fake and the
stores are in-memory, so nothing needs MySQL, Redis, GitHub or an LLM.
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
from src.expression_context.bundle_store import InMemoryBundleStore
from src.expression_context.catalog import StatsCatalog
from src.expression_context.lease import VerdictLease
from src.expression_context.models import JobRecord, StoredPair
from src.expression_context.service import ExpressionContextService
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.expression_context_helpers import (
    RELEASE_ID,
    REPO,
    FakeLiteratureGate,
    LeaseFakeRedis,
    build_bundle,
    gene_file,
    make_settings,
    module_redis,
    seed_bundle,
)

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

PREFIX = "/v1/expression-context"
ALICE = AuthenticatedUser(email="alice@example.org", name="Alice", domain="example.org")
BOB = AuthenticatedUser(email="bob@example.org", name="Bob", domain="example.org")


class Harness:
    def __init__(self, gate=None, store=None, bundle=None, seed=True, settings=None):
        self.gate = gate or FakeLiteratureGate()
        self.store = store or InMemoryExpressionContextStore()
        self.bundles = InMemoryBundleStore()
        self.bundle = bundle or build_bundle()
        self.user = ALICE
        self.redis = LeaseFakeRedis()
        self.app = FastAPI()
        settings = settings or make_settings()

        async def factory(app, s):
            if seed:
                await seed_bundle(self.bundles, self.bundle)
            return ExpressionContextService(
                catalog=StatsCatalog(self.bundles, cache_seconds=0),
                gate=self.gate,
                store=self.store,
                jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
                lease=VerdictLease(ttl_seconds=5, redis=module_redis(self.redis)),
                lease_poll_seconds=0.01,
                releases_repo=REPO,
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


def test_job_flow_surfaces_only_stats_and_literature_supported_pairs(harness, client):
    job = run_job(client)

    assert job["status"] == "succeeded"
    assert job["hugo_symbol"] == "ERBB2" and job["release_id"] == RELEASE_ID
    assert job["bundle_tag"] == harness.bundle.tag
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
    assert all(row.literature_gate == "acgc_tumor_specific_v2" for row in stored.values())


def test_get_gene_returns_surfaced_by_default(client):
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


def test_stored_verdicts_are_reused(harness, client):
    first = run_job(client)
    calls_after_first = list(harness.gate.calls)

    second = run_job(client)
    assert harness.gate.calls == calls_after_first
    assert second["literature_evaluated"] == 0 and second["literature_reused"] == 3
    assert second["result"]["surfaced"][0]["literature"] == first["result"]["surfaced"][0]["literature"]


def test_verdicts_from_another_release_or_gate_are_not_reused(harness, client):
    now = datetime.now(timezone.utc)
    evidence = {
        "literature_gate": "acgc_tumor_specific_v2", "tumor_type_query": "breast cancer",
        "supported": True, "insufficient_evidence": False, "pmids": ["1"],
        "evaluated_at": now.isoformat(),
    }
    stats = gene_file()["cancer_types"][0]
    for release_id, gate in (("older-release", "acgc_tumor_specific_v2"), (RELEASE_ID, "lras_v1")):
        harness.store.rows[("ERBB2", "BRCA", release_id, gate)] = StoredPair(
            hugo_symbol="ERBB2", oncotree_code="BRCA", release_id=release_id,
            literature_gate=gate, surfaced=True, stats=stats, evidence=evidence,
            computed_at=now - timedelta(days=1),
        )
    job = run_job(client)
    assert ("ERBB2", "breast cancer") in harness.gate.calls
    assert job["literature_reused"] == 0


def test_gate_failure_is_reported_and_retried_next_run(harness, client):
    harness.gate.fail_for = {"gastric cancer"}
    job = run_job(client)
    assert job["status"] == "succeeded"
    assert [err["oncotree_code"] for err in job["pair_errors"]] == ["STAD"]
    assert ("ERBB2", "STAD", RELEASE_ID, "acgc_tumor_specific_v2") not in harness.store.rows

    harness.gate.fail_for = set()
    harness.gate.calls.clear()
    again = run_job(client)
    assert harness.gate.calls == [("ERBB2", "gastric cancer")]
    assert again["literature_reused"] == 2 and again["pair_errors"] == []


def test_job_fails_cleanly_when_store_write_fails(harness, client):

    async def broken(pairs):
        raise RuntimeError("db down")

    harness.store.upsert_pairs = broken
    job = run_job(client)
    assert job["status"] == "failed" and "db down" in job["error"]
    assert job["finished_at"] is not None


def test_gene_resolved_through_aliases(client):
    job = run_job(client, "her2")
    assert job["gene"] == "her2" and job["hugo_symbol"] == "ERBB2"
    # Reads also resolve through aliases.
    assert client.get(f"{PREFIX}/genes/HER2").json()["hugo_symbol"] == "ERBB2"


def test_unknown_gene_is_404(harness, client):
    response = client.post(f"{PREFIX}/jobs", json={"gene": "NOPE1"})
    assert response.status_code == 404
    assert "NOPE1" in response.json()["detail"]
    assert harness.gate.calls == []


def test_gene_never_run_is_404(client):
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


def test_no_bundle_loaded_is_503_with_retry_after():
    harness = Harness(seed=False)
    with TestClient(harness.app) as client:
        for response in (
            client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}),
            client.get(f"{PREFIX}/stats/ERBB2"),
            # Nothing stored, and aliases can't be resolved without a bundle.
            client.get(f"{PREFIX}/genes/HER2"),
            client.get(f"{PREFIX}/genes"),
        ):
            assert response.status_code == 503, response.text
            assert response.headers["retry-after"] == "60"
            assert "not loaded yet" in response.json()["detail"]
        status = client.get(f"{PREFIX}/bundle").json()
        assert status["active"] is None and status["refresh"]["releases_repo"] == REPO
    assert harness.gate.calls == []


def test_job_visible_only_to_owner_or_admin(harness, client):
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
            is_surfaced = surfaced and code == "BRCA"
            evidence = {
                "literature_gate": "acgc_tumor_specific_v2",
                "tumor_type_query": "breast cancer",
                "supported": True, "insufficient_evidence": False,
                "pmids": ["1"], "verdict": "supported",
                "papers": [{"pmid": "1", "tumor_specific": True}],
                "evaluated_at": now.isoformat(),
            } if is_surfaced else None
            store.rows[(symbol, code, RELEASE_ID, "acgc_tumor_specific_v2")] = StoredPair(
                hugo_symbol=symbol, oncotree_code=code, release_id=RELEASE_ID,
                literature_gate="acgc_tumor_specific_v2", surfaced=is_surfaced,
                exclusion_reason=None if is_surfaced else "stats",
                stats={**stats, "oncotree_code": code}, evidence=evidence,
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
        assert client.get(f"{PREFIX}/stats/ERBB2").status_code == 401
        assert client.get(f"{PREFIX}/bundle").status_code == 401


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


async def test_running_status_write_failure_marks_job_failed():

    class FlakyJobs:
        def __init__(self):
            self.jobs = {}

        async def put(self, job):
            if job.status == "running":
                raise RuntimeError("job store down")
            self.jobs[job.job_id] = job.model_copy(deep=True)

        async def get(self, job_id):
            return self.jobs.get(job_id)

    gate = FakeLiteratureGate()
    redis = LeaseFakeRedis()
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, build_bundle())
    service = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0), gate=gate,
        store=InMemoryExpressionContextStore(), jobs=FlakyJobs(),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
    )
    job = await service.submit("ERBB2", owner="alice@example.org")
    await asyncio.gather(*service._tasks)
    stored = await service.jobs.get(job.job_id)
    assert stored.status == "failed" and "job store down" in stored.error
    assert stored.finished_at is not None and gate.calls == []
    await service.aclose()


def test_stats_view_before_and_after_literature_check(harness, client):
    before = client.get(f"{PREFIX}/stats/erbb2")
    assert before.status_code == 200, before.text
    view = before.json()
    assert view["hugo_symbol"] == "ERBB2" and view["query"] == "erbb2"
    assert view["entrez_gene_id"] == 2064
    assert view["literature_gate"] == "acgc_tumor_specific_v2"
    bundle = view["bundle"]
    assert bundle["bundle_tag"] == harness.bundle.tag and bundle["release_id"] == RELEASE_ID
    assert bundle["method_version"] == "2" and bundle["pubmed_snapshot_date"] == "2026-11-02"
    assert bundle["source"]["datahub_commit"].startswith("abc1234def56")
    assert bundle["release_url"].startswith("https://github.com/")
    pairs = _by_code(view["pairs"])
    # Statistics-passing pairs first (by q), then the rest.
    assert [p["oncotree_code"] for p in view["pairs"]] == ["BRCA", "STAD", "ESCA", "UCEC", "LUAD"]
    assert {code: (p["status"], p["exclusion_reason"], p["precheck"]) for code, p in pairs.items()} == {
        "BRCA": ("literature_pending", None, "pass"),
        "STAD": ("literature_pending", None, "pass"),
        "ESCA": ("literature_pending", None, "pass"),
        "UCEC": ("excluded", "precheck", "fail"),
        "LUAD": ("excluded", "stats", "fail"),  # stats fail wins over the pre-check
    }
    assert pairs["BRCA"]["stats"]["flags"] == ["tissue_of_origin_risk"]
    assert pairs["BRCA"]["cancer_type_name"] == "Breast Invasive Carcinoma"
    assert pairs["BRCA"]["tumor_type_query"] == "breast cancer"
    assert view["summary"] == {"surfaced": 0, "literature_pending": 3, "excluded": 2, "candidate": 0}
    assert harness.gate.calls == []  # viewing statistics never runs the literature gate

    run_job(client)
    after = client.get(f"{PREFIX}/stats/ERBB2").json()
    pairs = _by_code(after["pairs"])
    assert pairs["BRCA"]["status"] == "surfaced"
    assert pairs["BRCA"]["literature"]["pmids"] == ["12345678", "23456789"]
    assert pairs["STAD"]["status"] == "excluded" and pairs["STAD"]["exclusion_reason"] == "literature"
    assert pairs["STAD"]["literature"]["supported"] is False  # recomputed, not trusted
    assert after["summary"] == {"surfaced": 1, "literature_pending": 0, "excluded": 4, "candidate": 0}


def test_stats_view_resolves_aliases_and_rejects_unknown(client):
    assert client.get(f"{PREFIX}/stats/her2").json()["hugo_symbol"] == "ERBB2"
    missing = client.get(f"{PREFIX}/stats/NOPE1")
    assert missing.status_code == 404 and "NOPE1" in missing.json()["detail"]
    assert client.get(f"{PREFIX}/stats/BAD$SYMBOL").status_code == 422


def test_bundle_status_reports_active_bundle(harness, client):
    status = client.get(f"{PREFIX}/bundle").json()
    assert status["active"]["bundle_tag"] == harness.bundle.tag
    assert status["active"]["gene_count"] == 2
    assert status["refresh"]["pinned_bundle_tag"] is None


def test_stored_results_carry_bundle_provenance(harness, client):
    run_job(client)
    row = harness.store.rows[("ERBB2", "BRCA", RELEASE_ID, "acgc_tumor_specific_v2")]
    assert row.stats["bundle_tag"] == harness.bundle.tag
    body = client.get(f"{PREFIX}/genes/ERBB2").json()
    assert body["surfaced"][0]["bundle_tag"] == harness.bundle.tag
