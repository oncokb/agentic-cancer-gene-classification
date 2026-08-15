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
