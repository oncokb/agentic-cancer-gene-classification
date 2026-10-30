"""Retrying failed / no-result gene annotations on a saved run."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src import auth, main
from src.auth import AuthenticatedUser
from src.config import settings
from src.models.schema import AnnotationResult, GeneAnnotation
from tests.auth_helpers import session_token_for


def _annotation(gene, **overrides):
    data = dict(gene=gene, insufficient_evidence=False)
    data.update(overrides)
    return GeneAnnotation(**data)


def _result(annotations, run_id="retry-run", warnings=None, fusion_evidence=None):
    return AnnotationResult(
        run_id=run_id,
        timestamp="2026-10-29T12:00:00+00:00",
        fusions_processed=len(annotations),
        genes_annotated=len(annotations),
        annotations=annotations,
        warnings=warnings or [],
        fusion_evidence=fusion_evidence or [],
    )


class _FakeRunStore:
    def __init__(self, stored, request_payload=None, owner_email="local@internal"):
        self.stored = stored
        self.request_payload = request_payload or {"fusions": []}
        self.owner_email = owner_email
        self.updated_runs = []

    async def get_run(self, run_id):
        return self.stored if run_id == self.stored.get("run_id") else None

    async def get_run_metadata(self, run_id):
        if run_id != self.stored.get("run_id"):
            return None
        return {"owner_email": self.owner_email, "source": "session"}

    async def get_run_request(self, run_id):
        if run_id != self.stored.get("run_id"):
            return None
        return self.request_payload

    async def update_run_result(self, run_id, result_payload, *, genes_annotated=None):
        self.updated_runs.append((run_id, result_payload, genes_annotated))
        self.stored = result_payload


def _poll(client, status_url, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(status_url).json()
        if body["status"] in ("complete", "failed") or time.monotonic() > deadline:
            return body
        time.sleep(0.02)


def _stored_run(**overrides):
    data = {
        "run_id": "run-1",
        "timestamp": "2026-10-29T10:00:00+00:00",
        "fusions_processed": 2,
        "genes_annotated": 2,
        "annotations": [
            {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
            {"gene": "TP53", "insufficient_evidence": False, "cancer_associated": True},
        ],
        "warnings": ["ALK: using tumor type 'LUAD'; ignored tumor types: 'COAD'."],
        "fusion_evidence": [{"fusion": "EML4::ALK", "tumor_type": "LUAD", "retrieval_status": "ok"}],
    }
    data.update(overrides)
    return data


def test_merge_replaces_only_targeted_annotations_and_keeps_fusion_evidence():
    stored = _stored_run()
    retried = _result(
        [_annotation("ALK", cancer_associated=True, gene_summary="Retried")],
        warnings=["ALK: using tumor type 'LUAD'."],
    )

    merged = main._merge_retried_annotations(stored, retried)

    assert [item["gene"] for item in merged["annotations"]] == ["ALK", "TP53"]
    assert merged["annotations"][0]["gene_summary"] == "Retried"
    assert merged["annotations"][1]["cancer_associated"] is True
    assert merged["warnings"] == ["ALK: using tumor type 'LUAD'."]
    assert merged["fusion_evidence"] == stored["fusion_evidence"]
    assert merged["genes_annotated"] == 2
    assert merged["run_id"] == "run-1"
    assert merged["fusions_processed"] == 2


def test_merge_appends_new_annotation_and_updates_count():
    stored = _stored_run(annotations=[{"gene": "TP53"}], genes_annotated=1)
    retried = _result([_annotation("ALK"), _annotation("BRAF")])

    merged = main._merge_retried_annotations(stored, retried)

    assert [item["gene"] for item in merged["annotations"]] == ["TP53", "ALK", "BRAF"]
    assert merged["genes_annotated"] == 3


@pytest.mark.parametrize(
    "message,transient",
    [
        ("Synthesis error: connection reset", True),
        ("Unresolvable gene symbol — The Ensembl lookup for ENST1 failed; please retry.", True),
        ("'ENSTBAD' is not a valid human Ensembl ID or supported form.", False),
        ("'TP53(NM_000546.6)': NCBI NM_000546.6 belongs to TP53, not ALK.", False),
        ("Ensembl has no human gene, transcript or protein ENSG0.", False),
    ],
)
def test_transient_error_classification(message, transient):
    assert main._is_transient_annotation_error(message) is transient


def test_retry_job_merges_into_run_and_updates_gene_count(monkeypatch):
    main._annotation_jobs.clear_local()
    seen = {}

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
        seen["fusions"] = [item.fusion for item in fusions]
        seen["force_refresh"] = force_refresh
        seen["mode"] = kwargs.get("mode")
        seen["on_annotation"] = kwargs.get("on_annotation")
        annotation = _annotation("ALK", cancer_associated=True, gene_summary="Retried")
        if kwargs.get("on_annotation"):
            await kwargs["on_annotation"](annotation)
        if kwargs.get("on_total_known"):
            await kwargs["on_total_known"](1)
        return _result([annotation], run_id="pipeline-new-id")

    fake_store = _FakeRunStore(
        _stored_run(),
        request_payload={"fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}], "mode": "core"},
    )
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main.app.state, "run_store", fake_store, raising=False)
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"inputs": [{"fusion": "ALK", "tumor_type": "LUAD"}], "force_refresh": True},
    )

    assert response.status_code == 200
    body = _poll(client, response.json()["status_url"])
    assert body["status"] == "complete"
    assert body["result"]["run_id"] == "run-1"
    assert seen == {
        "fusions": ["ALK"],
        "force_refresh": True,
        "mode": "core",
        "on_annotation": seen["on_annotation"],
    }
    run_id, updated, genes_annotated = fake_store.updated_runs[-1]
    assert run_id == "run-1"
    assert genes_annotated == 2
    assert updated["annotations"][0]["gene_summary"] == "Retried"
    assert updated["annotations"][1]["gene"] == "TP53"
    assert updated["fusion_evidence"] == _stored_run()["fusion_evidence"]
    main._annotation_jobs.clear_local()


def test_deterministic_rejections_are_filtered_from_retry(monkeypatch):
    main._annotation_jobs.clear_local()
    seen = []

    async def fake_run_pipeline(fusions, **kwargs):
        seen.extend(item.fusion for item in fusions)
        return _result([_annotation("ALK")])

    stored = _stored_run(annotations=[
        {"gene": "BAD", "insufficient_evidence": True, "error": "'BAD' is not a valid human Ensembl ID."},
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
    ])
    fake_store = _FakeRunStore(stored)
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main.app.state, "run_store", fake_store, raising=False)
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"inputs": [{"fusion": "BAD"}, {"fusion": "ALK"}]},
    )

    assert response.status_code == 200
    _poll(client, response.json()["status_url"])
    assert seen == ["ALK"]
    main._annotation_jobs.clear_local()


def test_all_deterministic_inputs_return_422_without_starting_a_job(monkeypatch):
    main._annotation_jobs.clear_local()
    called = []

    async def fake_run_pipeline(*args, **kwargs):
        called.append(args)

    stored = _stored_run(annotations=[
        {"gene": "BAD", "insufficient_evidence": True, "error": "'BAD' is not a valid human Ensembl ID."},
    ])
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main.app.state, "run_store", _FakeRunStore(stored), raising=False)
    client = TestClient(main.app)

    response = client.post("/v1/runs/run-1/retry", json={"inputs": [{"fusion": "BAD"}]})

    assert response.status_code == 422
    assert "deterministic" in response.json()["detail"]
    assert called == []
    main._annotation_jobs.clear_local()


def test_non_owner_cannot_retry_a_run(monkeypatch):
    main._annotation_jobs.clear_local()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "run-retry-owner-test-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth, "_redis_client", None)
    called = []

    async def fake_run_pipeline(*args, **kwargs):
        called.append(args)

    fake_store = _FakeRunStore(_stored_run(), owner_email="owner@mskcc.org")
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main.app.state, "run_store", fake_store, raising=False)
    client = TestClient(main.app)
    token = session_token_for(
        AuthenticatedUser(email="other@mskcc.org", name="Other", domain="mskcc.org")
    )

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"inputs": [{"fusion": "ALK"}]},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert called == []
    assert fake_store.updated_runs == []
    main._annotation_jobs.clear_local()


def test_retry_unknown_run_is_404(monkeypatch):
    main._annotation_jobs.clear_local()
    monkeypatch.setattr(main.app.state, "run_store", _FakeRunStore(_stored_run()), raising=False)
    client = TestClient(main.app)

    response = client.post("/v1/runs/missing/retry", json={"inputs": [{"fusion": "ALK"}]})

    assert response.status_code == 404
    main._annotation_jobs.clear_local()


def test_run_inputs_endpoint_returns_original_submission(monkeypatch):
    request_payload = {
        "fusions": [
            {"fusion": "EML4::ALK", "tumor_type": "LUAD", "five_transcript": "ENST1"},
        ],
        "mode": "core",
    }
    fake_store = _FakeRunStore(_stored_run(), request_payload=request_payload)
    monkeypatch.setattr(main.app.state, "run_store", fake_store, raising=False)
    client = TestClient(main.app)

    response = client.get("/v1/runs/run-1/inputs")

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "inputs": [
            {
                "fusion": "EML4::ALK",
                "tumor_type": "LUAD",
                "five_exon": None,
                "three_exon": None,
                "five_genomic": None,
                "three_genomic": None,
                "five_transcript": "ENST1",
                "three_transcript": None,
            },
        ],
    }
