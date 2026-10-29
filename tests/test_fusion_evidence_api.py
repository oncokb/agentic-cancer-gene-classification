from __future__ import annotations

import time

import pytest

from fastapi.testclient import TestClient

from src import auth, main
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
    def __init__(self, stored_run, owner_email="local@internal"):
        self._stored_run = stored_run
        self._owner_email = owner_email
        self.updated_runs = []

    async def get_run(self, run_id):
        return self._stored_run if run_id == self._stored_run.get("run_id") else None

    async def get_run_metadata(self, run_id):
        if run_id != self._stored_run.get("run_id"):
            return None
        return {"owner_email": self._owner_email, "source": "session"}

    async def update_run_result(self, run_id, result_payload):
        self.updated_runs.append((run_id, result_payload))
        self._stored_run = result_payload


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
