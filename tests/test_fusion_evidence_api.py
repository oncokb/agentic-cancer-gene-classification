from __future__ import annotations

import time

from fastapi.testclient import TestClient

from src import main
from src.models.schema import FusionEvidenceResult


def test_fusion_evidence_job_runs_in_background_and_ignores_gene_inputs(monkeypatch):
    main._fusion_evidence_jobs.clear()
    seen = []

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None):
        seen.append((fusion, tumor_type))
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
    assert seen == [("EML4::ALK", "LUAD")]
    main._fusion_evidence_jobs.clear()


class _FakeRunStore:
    def __init__(self, stored_run):
        self._stored_run = stored_run
        self.updated_runs = []

    async def get_run(self, run_id):
        return self._stored_run if run_id == self._stored_run.get("run_id") else None

    async def update_run_result(self, run_id, result_payload):
        self.updated_runs.append((run_id, result_payload))
        self._stored_run = result_payload


def test_fusion_evidence_job_persists_result_onto_run_when_run_id_given(monkeypatch):
    main._fusion_evidence_jobs.clear()

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None):
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
    main._fusion_evidence_jobs.clear()
