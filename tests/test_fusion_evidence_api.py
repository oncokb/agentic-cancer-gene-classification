from __future__ import annotations

import asyncio
import copy
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest

from fastapi.testclient import TestClient

from src import api_keys, auth, main
from src.api_keys import ApiKeyRecord, InMemoryRateLimiter, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser
from src.config import settings
from src.models.schema import FusionEvidenceResult
from tests.auth_helpers import session_token_for


@pytest.mark.parametrize("force_refresh", [False, True])
def test_fusion_evidence_job_runs_in_background_and_ignores_gene_inputs(monkeypatch, force_refresh):
    main._fusion_evidence_jobs.clear_local()
    seen = []

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None, force_refresh=False):
        seen.append((fusion, tumor_type, force_refresh))
        return FusionEvidenceResult(
            fusion=fusion,
            tumor_type=tumor_type,
            well_supported=True,
            retrieved_count=2,
            pmids=["1", "2"],
            interpretation="Fusion is well supported.",
        )

    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve_fusion_evidence)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)
    client = TestClient(main.app)

    create_response = client.post(
        "/v1/fusion-evidence/jobs",
        json={
            "fusions": [
                {"fusion": "EML4::ALK", "tumor_type": "LUAD"},
                {"fusion": "EML4::ALK", "tumor_type": "LUAD"},
                "BRAF",
            ],
            "mode": "core",
            "force_refresh": force_refresh,
        },
    )

    assert create_response.status_code == 200
    status_url = create_response.json()["status_url"]

    payload = None
    for _ in range(20):
        status_response = client.get(status_url)
        assert status_response.status_code == 200
        payload = status_response.json()
        if payload["status"] == "complete":
            break
        time.sleep(0.01)

    assert payload is not None
    assert payload["status"] == "complete"
    assert payload["fusions_total"] == 1
    assert payload["fusions_completed"] == 1
    assert payload["fusion_evidence"][0]["fusion"] == "EML4::ALK"
    assert payload["fusion_evidence"][0]["well_supported"] is True
    assert seen == [("EML4::ALK", "LUAD", force_refresh)]
    main._fusion_evidence_jobs.clear_local()


class _FakeRunStore:
    """In-memory run store whose merge primitive serializes per run, matching
    the row lock the MySQL store uses."""

    def __init__(self, stored_run, owner_email="local@internal"):
        self._stored_run = stored_run
        self._owner_email = owner_email
        self.updated_runs = []
        self._merge_lock = asyncio.Lock()
        self.keys: dict[str, ApiKeyRecord] = {}

    async def get_run(self, run_id):
        return self._stored_run if run_id == self._stored_run.get("run_id") else None

    async def get_run_metadata(self, run_id):
        if run_id != self._stored_run.get("run_id"):
            return None
        return {"owner_email": self._owner_email, "source": "session"}

    async def update_run_result(self, run_id, result_payload):
        self.updated_runs.append((run_id, result_payload))
        self._stored_run = result_payload

    async def merge_run_result(self, run_id, mutate):
        async with self._merge_lock:
            if run_id != self._stored_run.get("run_id"):
                return None
            updated = mutate(copy.deepcopy(self._stored_run))
            if updated is None:
                updated = self._stored_run
            self._stored_run = updated
            self.updated_runs.append((run_id, updated))
            return updated

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        return next((key for key in self.keys.values() if key.key_hash == key_hash), None)

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        pass


class _InterleavingRunStore(_FakeRunStore):
    """A read-modify-write store with a barrier on reads: the pre-atomic
    get_run/update_run_result path lets both jobs read the same snapshot, so
    the last update loses the other job's attach."""

    def __init__(self, stored_run, **kwargs):
        super().__init__(stored_run, **kwargs)
        self._reads = 0
        self._both_read = asyncio.Event()

    async def get_run(self, run_id):
        snapshot = copy.deepcopy(self._stored_run)
        self._reads += 1
        if self._reads >= 2:
            self._both_read.set()
        await self._both_read.wait()
        return snapshot


def test_fusion_evidence_job_persists_result_onto_run_when_run_id_given(monkeypatch):
    main._fusion_evidence_jobs.clear_local()

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None, force_refresh=False):
        return FusionEvidenceResult(
            fusion=fusion,
            tumor_type=tumor_type,
            well_supported=True,
            retrieved_count=2,
            pmids=["1", "2"],
            interpretation="Fusion is well supported.",
        )

    fake_run_store = _FakeRunStore(
        {"run_id": "run-42", "annotations": [], "fusion_evidence": []}
    )
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve_fusion_evidence)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)
    main.app.state.run_store = fake_run_store
    client = TestClient(main.app)

    create_response = client.post(
        "/v1/fusion-evidence/jobs",
        json={
            "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}],
            "mode": "core",
            "run_id": "run-42",
        },
    )
    assert create_response.status_code == 200
    status_url = create_response.json()["status_url"]

    payload = None
    for _ in range(20):
        status_response = client.get(status_url)
        payload = status_response.json()
        if payload["status"] == "complete":
            break
        time.sleep(0.01)

    assert payload is not None
    assert payload["status"] == "complete"
    assert fake_run_store.updated_runs, "expected the run to be updated with fusion evidence"
    updated_run_id, updated_payload = fake_run_store.updated_runs[-1]
    assert updated_run_id == "run-42"
    assert updated_payload["fusion_evidence"][0]["fusion"] == "EML4::ALK"
    assert updated_payload["fusion_evidence"][0]["well_supported"] is True
    main._fusion_evidence_jobs.clear_local()


def test_fusion_evidence_job_merges_into_run_and_keeps_other_items(monkeypatch):
    main._fusion_evidence_jobs.clear_local()

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None, force_refresh=False):
        return FusionEvidenceResult(
            fusion=fusion,
            tumor_type=tumor_type,
            well_supported=True,
            retrieved_count=2,
            pmids=["9", "10"],
            interpretation="Retried successfully.",
        )

    fake_run_store = _FakeRunStore({
        "run_id": "run-merge",
        "owner_email": "local@internal",
        "source": "session",
        "input_summary": "EML4::ALK",
        "annotations": [],
        "fusion_evidence": [
            {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
            {"fusion": "EML4::ALK", "tumor_type": "LUAD", "retrieval_status": "failed"},
            {"fusion": "EML4::ALK", "tumor_type": "COAD", "retrieval_status": "ok"},
        ],
    })
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve_fusion_evidence)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)
    main.app.state.run_store = fake_run_store
    client = TestClient(main.app)

    create_response = client.post(
        "/v1/fusion-evidence/jobs",
        json={
            "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}],
            "run_id": "run-merge",
        },
    )
    assert create_response.status_code == 200
    status_url = create_response.json()["status_url"]
    for _ in range(20):
        payload = client.get(status_url).json()
        if payload["status"] == "complete":
            break
        time.sleep(0.01)

    assert payload["status"] == "complete"
    updated_run_id, updated_payload = fake_run_store.updated_runs[-1]
    assert updated_run_id == "run-merge"
    assert updated_payload["fusion_evidence"] == [
        {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
        fake_retrieve_result("EML4::ALK", "LUAD"),
        {"fusion": "EML4::ALK", "tumor_type": "COAD", "retrieval_status": "ok"},
    ]
    # Attaching evidence must not rewrite the run's attribution or summary.
    assert updated_payload["owner_email"] == "local@internal"
    assert updated_payload["source"] == "session"
    assert updated_payload["input_summary"] == "EML4::ALK"
    main._fusion_evidence_jobs.clear_local()


def fake_retrieve_result(fusion, tumor_type):
    return FusionEvidenceResult(
        fusion=fusion,
        tumor_type=tumor_type,
        well_supported=True,
        retrieved_count=2,
        pmids=["9", "10"],
        interpretation="Retried successfully.",
    ).model_dump()


def test_non_owner_cannot_attach_fusion_evidence_to_a_run(monkeypatch):
    main._fusion_evidence_jobs.clear_local()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "fusion-evidence-owner-test-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth, "_redis_client", None)
    called = []

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None, force_refresh=False):
        called.append(fusion)
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type)

    fake_run_store = _FakeRunStore(
        {"run_id": "run-owned", "annotations": [], "fusion_evidence": []},
        owner_email="owner@mskcc.org",
    )
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve_fusion_evidence)
    main.app.state.run_store = fake_run_store
    client = TestClient(main.app)
    token = session_token_for(
        AuthenticatedUser(email="other@mskcc.org", name="Other", domain="mskcc.org")
    )

    response = client.post(
        "/v1/fusion-evidence/jobs",
        json={"fusions": [{"fusion": "EML4::ALK"}], "run_id": "run-owned"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert called == []
    assert fake_run_store.updated_runs == []
    main._fusion_evidence_jobs.clear_local()


def test_merge_matches_submitted_input_when_resolved_symbol_differs():
    existing = [
        {"fusion": "EML4::ALK", "input": "ENST1::ENST2", "tumor_type": "LUAD",
         "retrieval_status": "failed"},
        {"fusion": "EML4::ALK", "tumor_type": "COAD", "retrieval_status": "ok"},
        {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
    ]
    incoming = [
        {"fusion": "EML4::ALK", "input": "ENST1::ENST2", "tumor_type": "LUAD",
         "retrieval_status": "ok", "pmids": ["3"]},
    ]

    merged = main._merge_fusion_evidence(existing, incoming)

    assert merged == [
        {"fusion": "EML4::ALK", "input": "ENST1::ENST2", "tumor_type": "LUAD",
         "retrieval_status": "ok", "pmids": ["3"]},
        {"fusion": "EML4::ALK", "tumor_type": "COAD", "retrieval_status": "ok"},
        {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
    ]


def _auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "fusion-evidence-owner-test-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 1000)
    monkeypatch.setattr(auth, "_redis_client", None)
    monkeypatch.setattr(api_keys, "_memory_limiter", InMemoryRateLimiter())


def _session_headers(email: str, role: str = "curator") -> dict:
    token = session_token_for(
        AuthenticatedUser(email=email, name="User", domain="mskcc.org", role=role)
    )
    return {"Authorization": f"Bearer {token}"}


def _submit_and_poll(client, fusions, run_id, headers):
    response = client.post(
        "/v1/fusion-evidence/jobs",
        json={"fusions": fusions, "run_id": run_id},
        headers=headers,
    )
    if response.status_code != 200:
        return response, None
    for _ in range(50):
        payload = client.get(response.json()["status_url"], headers=headers).json()
        if payload["status"] in ("complete", "failed"):
            return response, payload
        time.sleep(0.01)
    return response, payload


def _ok_retrieve(monkeypatch):
    async def retrieve(fusion, tumor_type=None, force_refresh=False):
        return FusionEvidenceResult(
            fusion=fusion, tumor_type=tumor_type, retrieval_status="ok", pmids=["1"],
        )

    monkeypatch.setattr(main, "retrieve_fusion_evidence", retrieve)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)


def test_authenticated_owner_can_attach_fusion_evidence(monkeypatch):
    main._fusion_evidence_jobs.clear_local()
    _auth_settings(monkeypatch)
    _ok_retrieve(monkeypatch)
    store = _FakeRunStore(
        {"run_id": "run-owner", "annotations": [], "fusion_evidence": []},
        owner_email="owner@mskcc.org",
    )
    main.app.state.run_store = store
    client = TestClient(main.app)

    response, payload = _submit_and_poll(
        client, [{"fusion": "EML4::ALK"}], "run-owner", _session_headers("Owner@MSKCC.org")
    )

    assert response.status_code == 200
    assert payload["status"] == "complete"
    assert store.updated_runs[-1][1]["fusion_evidence"][0]["fusion"] == "EML4::ALK"


def test_authenticated_non_owner_and_missing_run_cannot_attach(monkeypatch):
    main._fusion_evidence_jobs.clear_local()
    _auth_settings(monkeypatch)
    _ok_retrieve(monkeypatch)
    store = _FakeRunStore(
        {"run_id": "run-owner", "annotations": [], "fusion_evidence": []},
        owner_email="owner@mskcc.org",
    )
    main.app.state.run_store = store
    client = TestClient(main.app)
    headers = _session_headers("other@mskcc.org")

    denied = client.post(
        "/v1/fusion-evidence/jobs",
        json={"fusions": [{"fusion": "EML4::ALK"}], "run_id": "run-owner"},
        headers=headers,
    )
    missing = client.post(
        "/v1/fusion-evidence/jobs",
        json={"fusions": [{"fusion": "EML4::ALK"}], "run_id": "not-a-run"},
        headers=headers,
    )

    assert denied.status_code == 403
    assert missing.status_code == 404
    assert store.updated_runs == []


def test_authenticated_admin_can_attach_to_another_users_run(monkeypatch):
    main._fusion_evidence_jobs.clear_local()
    _auth_settings(monkeypatch)
    _ok_retrieve(monkeypatch)
    store = _FakeRunStore(
        {"run_id": "run-owner", "annotations": [], "fusion_evidence": []},
        owner_email="owner@mskcc.org",
    )
    main.app.state.run_store = store
    client = TestClient(main.app)

    response, payload = _submit_and_poll(
        client,
        [{"fusion": "EML4::ALK"}],
        "run-owner",
        _session_headers("admin@mskcc.org", role="admin"),
    )

    assert response.status_code == 200
    assert payload["status"] == "complete"
    assert store.updated_runs


def test_owner_api_key_can_attach(monkeypatch):
    main._fusion_evidence_jobs.clear_local()
    _auth_settings(monkeypatch)
    _ok_retrieve(monkeypatch)
    store = _FakeRunStore(
        {"run_id": "run-owner", "annotations": [], "fusion_evidence": []},
        owner_email="owner@mskcc.org",
    )
    key = generate_api_key()
    store.keys["key-1"] = ApiKeyRecord(
        id="key-1",
        key_hash=hash_api_key(key),
        owner_email="Owner@MSKCC.org",
        name="script",
        created_at=datetime.now(timezone.utc),
    )
    main.app.state.run_store = store
    client = TestClient(main.app)

    response, payload = _submit_and_poll(
        client,
        [{"fusion": "EML4::ALK"}],
        "run-owner",
        {"Authorization": f"Bearer {key}"},
    )

    assert response.status_code == 200
    assert payload["status"] == "complete"
    assert store.updated_runs


def test_ownerless_legacy_run_is_admin_only(monkeypatch):
    main._fusion_evidence_jobs.clear_local()
    _auth_settings(monkeypatch)
    _ok_retrieve(monkeypatch)
    store = _FakeRunStore(
        {"run_id": "run-legacy", "annotations": [], "fusion_evidence": []},
        owner_email=None,
    )
    main.app.state.run_store = store
    client = TestClient(main.app)

    denied = client.post(
        "/v1/fusion-evidence/jobs",
        json={"fusions": [{"fusion": "EML4::ALK"}], "run_id": "run-legacy"},
        headers=_session_headers("curator@mskcc.org"),
    )
    allowed, payload = _submit_and_poll(
        client,
        [{"fusion": "EML4::ALK"}],
        "run-legacy",
        _session_headers("admin@mskcc.org", role="admin"),
    )

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert payload["status"] == "complete"
    assert store.updated_runs


def test_concurrent_fusion_evidence_merges_do_not_lose_updates(monkeypatch):
    async def scenario():
        store = _InterleavingRunStore({
            "run_id": "run-concurrent",
            "annotations": [],
            "fusion_evidence": [
                {"fusion": "A::B", "tumor_type": "LUAD", "retrieval_status": "failed"},
                {"fusion": "C::D", "tumor_type": "LUAD", "retrieval_status": "failed"},
            ],
        })
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(run_store=store))
        )
        retry_a = [FusionEvidenceResult(fusion="A::B", tumor_type="LUAD", pmids=["1"])]
        retry_b = [FusionEvidenceResult(fusion="C::D", tumor_type="LUAD", pmids=["2"])]

        await asyncio.gather(
            main._persist_fusion_evidence(request, "run-concurrent", retry_a),
            main._persist_fusion_evidence(request, "run-concurrent", retry_b),
        )
        return store._stored_run["fusion_evidence"]

    merged = asyncio.run(scenario())

    assert [item["retrieval_status"] for item in merged] == ["ok", "ok"]
    assert [item["pmids"] for item in merged] == [["1"], ["2"]]


def test_merge_resolves_duplicate_keys_to_one_item():
    existing = [
        {"fusion": "A::B", "tumor_type": "LUAD", "retrieval_status": "failed"},
        {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
    ]
    incoming = [
        {"fusion": "A::B", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["1"]},
        {"fusion": "A::B", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["2"]},
        {"fusion": "NEW::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
        {"fusion": "NEW::ONE", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["3"]},
    ]

    merged = main._merge_fusion_evidence(existing, incoming)

    assert merged == [
        {"fusion": "A::B", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["2"]},
        {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok"},
        {"fusion": "NEW::ONE", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["3"]},
    ]


def test_single_fusion_force_refresh_rerun_bypasses_cache_and_merges_only_it(
    monkeypatch, fake_redis,
):
    """The UI's per-card Re-run: one fusion + run_id + force_refresh skips the
    cached evidence, replaces that one item in place and refreshes the cache."""
    from src.pipeline import literature

    main._fusion_evidence_jobs.clear_local()
    # Job snapshots stay in-process; fake_redis is the evidence cache alone.
    monkeypatch.setattr(main._fusion_evidence_jobs, "_redis_client_factory", lambda: None)
    stale = FusionEvidenceResult(
        fusion="EML4::ALK", tumor_type="LUAD", retrieval_status="ok",
        literature_scope="no_literature", interpretation="Cached before the re-run.",
    )
    cache_key = literature._fusion_evidence_cache_key(
        "EML4::ALK", "LUAD", settings.fusion_evidence_max_results,
    )
    asyncio.run(fake_redis.set(cache_key, stale.model_dump_json()))

    live_calls = []

    async def fake_uncached(fusion, tumor_type=None, max_results=None):
        live_calls.append((fusion, tumor_type))
        return FusionEvidenceResult(
            fusion=fusion, tumor_type=tumor_type, retrieval_status="ok",
            literature_scope="requested_tumor", retrieved_count=2, pmids=["7", "8"],
            interpretation="Fresh lookup.",
        )

    monkeypatch.setattr(literature, "_retrieve_fusion_evidence_uncached", fake_uncached)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)
    others = [
        {"fusion": "KEEP::ONE", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["1"]},
        {"fusion": "EML4::ALK", "tumor_type": "COAD", "retrieval_status": "failed", "pmids": []},
    ]
    fake_run_store = _FakeRunStore({
        "run_id": "run-rerun", "annotations": [],
        "fusion_evidence": [others[0], stale.model_dump(), others[1]],
    })
    main.app.state.run_store = fake_run_store
    client = TestClient(main.app)

    def run_job(force_refresh):
        response = client.post("/v1/fusion-evidence/jobs", json={
            "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}],
            "run_id": "run-rerun",
            "force_refresh": force_refresh,
        })
        assert response.status_code == 200
        for _ in range(50):
            payload = client.get(response.json()["status_url"]).json()
            if payload["status"] == "complete":
                return payload
            time.sleep(0.01)
        raise AssertionError(f"job did not complete: {payload}")

    # Without force_refresh the cached evidence is reused (sanity check).
    cached = run_job(force_refresh=False)
    assert live_calls == []
    assert cached["fusion_evidence"][0]["interpretation"] == "Cached before the re-run."

    rerun = run_job(force_refresh=True)
    assert live_calls == [("EML4::ALK", "LUAD")], "force_refresh must bypass the cache"
    assert [item["fusion"] for item in rerun["fusion_evidence"]] == ["EML4::ALK"]
    assert rerun["fusion_evidence"][0]["pmids"] == ["7", "8"]

    _, updated = fake_run_store.updated_runs[-1]
    assert updated["fusion_evidence"][0] == others[0]
    assert updated["fusion_evidence"][1]["pmids"] == ["7", "8"]
    assert updated["fusion_evidence"][1]["interpretation"] == "Fresh lookup."
    assert updated["fusion_evidence"][2] == others[1], "same fusion, other tumor type untouched"
    assert len(updated["fusion_evidence"]) == 3
    refreshed = asyncio.run(fake_redis.get(cache_key))
    assert b"Fresh lookup." in refreshed, "the re-run replaces the cached value"
    main._fusion_evidence_jobs.clear_local()


def test_merge_never_replaces_succeeded_item_with_failed_or_partial():
    good = {"fusion": "A::B", "tumor_type": "LUAD", "retrieval_status": "ok", "pmids": ["1"]}
    failed = {"fusion": "C::D", "tumor_type": "LUAD", "retrieval_status": "failed", "pmids": []}
    partial = {"fusion": "E::F", "tumor_type": "LUAD", "retrieval_status": "ok", "partial": True}
    also_failed = {"fusion": "G::H", "tumor_type": "LUAD", "retrieval_status": "failed", "pmids": []}

    merged = main._merge_fusion_evidence(
        [good, failed, partial, also_failed],
        [
            {**good, "retrieval_status": "failed", "pmids": []},  # good kept
            {**failed, "partial": True, "retrieval_status": "ok", "pmids": ["2"]},  # failed -> partial
            {**partial, "retrieval_status": "failed", "partial": False},  # partial -> failed
            {**also_failed, "interpretation": "PubMed search failed (HTTP 503)"},  # failed -> failed
        ],
    )

    assert merged[0] == good
    assert merged[1]["partial"] is True and merged[1]["pmids"] == ["2"]
    assert merged[2]["retrieval_status"] == "failed"
    assert merged[3]["interpretation"] == "PubMed search failed (HTTP 503)"
    assert main._merge_fusion_evidence([good], [{**good, "partial": True, "pmids": []}]) == [good]


@pytest.mark.parametrize("outcome", ["raises", "partial"])
def test_failed_or_partial_rerun_keeps_runs_good_evidence(monkeypatch, outcome):
    """A single-fusion force_refresh re-run whose lookup fails completes with a
    failed (or partial) item, but the saved run keeps its earlier evidence."""
    main._fusion_evidence_jobs.clear_local()

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None, force_refresh=False):
        assert force_refresh is True
        if outcome == "raises":
            raise TimeoutError("PubMed timed out")
        return FusionEvidenceResult(
            fusion=fusion, tumor_type=tumor_type, partial=True, literature_scope="requested_tumor",
        )

    good = {
        "fusion": "EML4::ALK", "tumor_type": "LUAD", "retrieval_status": "ok",
        "literature_scope": "requested_tumor", "pmids": ["1", "2"],
    }
    failed = {"fusion": "KIF5B::RET", "tumor_type": "LUAD", "retrieval_status": "failed", "pmids": []}
    fake_run_store = _FakeRunStore({"run_id": "run-keep", "annotations": [], "fusion_evidence": [good, failed]})
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve_fusion_evidence)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)
    main.app.state.run_store = fake_run_store
    client = TestClient(main.app)

    response = client.post("/v1/fusion-evidence/jobs", json={
        "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}],
        "run_id": "run-keep",
        "force_refresh": True,
    })
    assert response.status_code == 200
    for _ in range(50):
        payload = client.get(response.json()["status_url"]).json()
        if payload["status"] == "complete":
            break
        time.sleep(0.01)

    assert payload["status"] == "complete"
    job_item = payload["fusion_evidence"][0]
    if outcome == "raises":
        assert job_item["retrieval_status"] == "failed"
    else:
        assert job_item["partial"] is True
    _, updated = fake_run_store.updated_runs[-1]
    assert updated["fusion_evidence"] == [good, failed], "the run keeps its earlier evidence"
    main._fusion_evidence_jobs.clear_local()
