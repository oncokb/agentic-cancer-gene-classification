"""Retrying failed / no-result gene annotations on a saved run."""

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
from src.models.schema import AnnotationResult, FusionEvidenceResult, GeneAnnotation
from tests.auth_helpers import session_token_for


def _annotation(gene, **overrides):
    data = dict(gene=gene, insufficient_evidence=False)
    data.update(overrides)
    return GeneAnnotation(**data)


def _result(annotations, run_id="retry-run", warnings=None):
    return AnnotationResult(
        run_id=run_id,
        timestamp="2026-10-29T12:00:00+00:00",
        fusions_processed=len(annotations),
        genes_annotated=len(annotations),
        annotations=annotations,
        warnings=warnings or [],
    )


def _dump(annotations):
    return [item.model_dump() for item in annotations]


class _FakeRunStore:
    """In-memory run store whose merge primitive serializes per run, matching
    the row lock the MySQL store uses."""

    def __init__(self, stored, request_payload=None, owner_email="local@internal"):
        self.stored = stored
        self.request_payload = request_payload or {"fusions": []}
        self.owner_email = owner_email
        self.updated_runs = []
        self.keys: dict[str, ApiKeyRecord] = {}
        self._merge_lock = asyncio.Lock()

    async def get_run(self, run_id):
        return copy.deepcopy(self.stored) if run_id == self.stored.get("run_id") else None

    async def get_run_metadata(self, run_id):
        if run_id != self.stored.get("run_id"):
            return None
        return {"owner_email": self.owner_email, "source": "session"}

    async def get_run_request(self, run_id):
        if run_id != self.stored.get("run_id"):
            return None
        return self.request_payload

    async def update_run_result(self, run_id, result_payload):
        self.updated_runs.append((run_id, result_payload, None))
        self.stored = result_payload

    async def merge_run_result(self, run_id, mutate, *, sync_genes_annotated=False):
        async with self._merge_lock:
            if run_id != self.stored.get("run_id"):
                return None
            updated = mutate(copy.deepcopy(self.stored))
            if updated is None:
                updated = self.stored
            self.stored = updated
            self.updated_runs.append((run_id, updated, sync_genes_annotated))
            return updated

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        return next((key for key in self.keys.values() if key.key_hash == key_hash), None)

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        pass


class _SlowReadRunStore(_FakeRunStore):
    """get_run returns a snapshot, then stalls: an unlocked read-modify-write
    retry (get_run + update_run_result) writes that stale snapshot back after
    a concurrent fusion-evidence merge has landed."""

    async def get_run(self, run_id):
        snapshot = await super().get_run(run_id)
        await asyncio.sleep(0.05)
        return snapshot


def _poll(client, status_url, headers=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(status_url, headers=headers).json()
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


@pytest.fixture
def fresh_jobs():
    main._annotation_jobs.clear_local()
    yield
    main._annotation_jobs.clear_local()


def _use_store(monkeypatch, store):
    monkeypatch.setattr(main.app.state, "run_store", store, raising=False)


def _ok_pipeline(monkeypatch, seen=None, annotations=None):
    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
        if seen is not None:
            seen.append({
                "fusions": [item.model_dump(exclude_none=True) for item in fusions],
                "force_refresh": force_refresh,
                "local_backend": local_backend,
                "mode": kwargs.get("mode"),
            })
        rows = annotations or [_annotation("ALK", cancer_associated=True, gene_summary="Retried")]
        for row in rows:
            if kwargs.get("on_annotation"):
                await kwargs["on_annotation"](row)
        return _result(rows, run_id="pipeline-new-id", warnings=["ALK: using tumor type 'LUAD'."])

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)


# ---------------------------------------------------------------------------
# Merge rules
# ---------------------------------------------------------------------------


def test_merge_replaces_only_targeted_annotations_and_keeps_fusion_evidence():
    stored = _stored_run()

    merged = main._merge_retried_annotations(
        copy.deepcopy(stored),
        _dump([_annotation("ALK", cancer_associated=True, gene_summary="Retried")]),
        ["ALK: using tumor type 'LUAD'."],
    )

    assert [item["gene"] for item in merged["annotations"]] == ["ALK", "TP53"]
    assert merged["annotations"][0]["gene_summary"] == "Retried"
    assert merged["annotations"][1] == stored["annotations"][1]
    assert merged["warnings"] == ["ALK: using tumor type 'LUAD'."]
    assert merged["fusion_evidence"] == stored["fusion_evidence"]
    assert merged["genes_annotated"] == 2
    assert merged["run_id"] == "run-1"
    assert merged["fusions_processed"] == 2


def test_merge_never_replaces_a_better_annotation_with_a_worse_one():
    stored = _stored_run(annotations=[
        {"gene": "TP53", "insufficient_evidence": False, "cancer_associated": True},
        {"gene": "BRAF", "insufficient_evidence": True},
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
    ], warnings=["TP53: kept warning."])

    merged = main._merge_retried_annotations(
        copy.deepcopy(stored),
        _dump([
            _annotation("TP53", insufficient_evidence=True, error="Synthesis error: again"),
            _annotation("BRAF", insufficient_evidence=True, error="Synthesis error: again"),
            _annotation("ALK", insufficient_evidence=True, error="Synthesis error: rate limit"),
        ]),
        ["TP53: new warning from a failed retry."],
    )

    assert merged["annotations"][0] == stored["annotations"][0]
    assert merged["annotations"][1] == stored["annotations"][1]
    # An error replaced by a newer error is fine: it is no worse.
    assert merged["annotations"][2]["error"] == "Synthesis error: rate limit"
    assert "TP53: kept warning." in merged["warnings"]


def test_merge_replaces_row_stored_under_its_raw_input():
    stored = _stored_run(annotations=[
        {
            "gene": "ENSG00000157764",
            "insufficient_evidence": True,
            "error": "Unresolvable gene symbol — Ensembl lookup temporarily unavailable (503); retry later.",
        },
        {"gene": "TP53", "insufficient_evidence": False},
    ])
    retried = _annotation(
        "BRAF",
        resolved_inputs=[{"input": "ENSG00000157764", "resolved": "BRAF"}],
    )

    merged = main._merge_retried_annotations(copy.deepcopy(stored), _dump([retried]), [])

    assert [item["gene"] for item in merged["annotations"]] == ["BRAF", "TP53"]
    assert merged["genes_annotated"] == 2


@pytest.mark.parametrize(
    "message,transient",
    [
        ("Synthesis error: connection reset", True),
        ("Unresolvable gene symbol — The NCBI RefSeq lookup for NM_1 failed; please retry.", True),
        ("Unresolvable gene symbol — Ensembl lookup temporarily unavailable (503); retry later.", True),
        ("Unresolvable gene symbol — Ensembl did not find ID ENSG0.", False),
        ("'ENSTBAD' is not a valid human Ensembl ID or supported form.", False),
        ("'TP53(NM_000546.6)': NCBI NM_000546.6 belongs to TP53, not ALK.", False),
        ("Ensembl has no human gene, transcript or protein ENSG0.", False),
    ],
)
def test_transient_error_classification(message, transient):
    assert main._is_transient_annotation_error(message) is transient


# ---------------------------------------------------------------------------
# The retry job
# ---------------------------------------------------------------------------


def test_retry_job_merges_into_run_under_the_lock(monkeypatch, fresh_jobs):
    seen = []
    _ok_pipeline(monkeypatch, seen)
    store = _FakeRunStore(
        _stored_run(),
        request_payload={
            "fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}],
            "mode": "core",
            "local_backend": "codex",
        },
    )
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"inputs": [{"fusion": "ALK", "tumor_type": "LUAD"}], "force_refresh": True},
    )

    assert response.status_code == 200
    body = _poll(client, response.json()["status_url"])
    assert body["status"] == "complete"
    assert body["result"]["run_id"] == "run-1"
    assert seen == [{
        "fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}],
        "force_refresh": True,
        "local_backend": "codex",
        "mode": "core",
    }]
    run_id, updated, sync_genes_annotated = store.updated_runs[-1]
    assert run_id == "run-1"
    assert sync_genes_annotated is True
    assert updated["annotations"][0]["gene_summary"] == "Retried"
    assert updated["annotations"][1]["gene"] == "TP53"
    assert updated["fusion_evidence"] == _stored_run()["fusion_evidence"]


def test_failed_retry_job_leaves_the_run_unchanged(monkeypatch, fresh_jobs):
    async def failing_pipeline(fusions, **kwargs):
        await kwargs["on_annotation"](_annotation("ALK", gene_summary="partial"))
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(main, "run_pipeline", failing_pipeline)
    store = _FakeRunStore(_stored_run())
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post("/v1/runs/run-1/retry", json={"inputs": [{"fusion": "ALK"}]})

    body = _poll(client, response.json()["status_url"])
    assert body["status"] == "failed"
    assert store.updated_runs == []
    assert store.stored == _stored_run()


def test_retry_that_fails_again_does_not_overwrite_a_good_annotation(monkeypatch, fresh_jobs):
    _ok_pipeline(monkeypatch, annotations=[
        _annotation("BRAF", insufficient_evidence=True, error="Synthesis error: timeout"),
    ])
    good_braf = {"gene": "BRAF", "insufficient_evidence": False, "gene_summary": "Good"}
    stored = _stored_run(annotations=[
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        good_braf,
    ])
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    # ALK::BRAF touches the retryable ALK row; BRAF's good row must survive.
    response = client.post("/v1/runs/run-1/retry", json={"inputs": ["ALK::BRAF"]})

    assert _poll(client, response.json()["status_url"])["status"] == "complete"
    assert store.stored["annotations"][1] == good_braf


def test_permanent_rejections_and_unrelated_inputs_are_not_retried(monkeypatch, fresh_jobs):
    seen = []
    _ok_pipeline(monkeypatch, seen)
    stored = _stored_run(annotations=[
        {"gene": "BAD", "insufficient_evidence": True, "error": "'BAD' is not a valid human Ensembl ID."},
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        {"gene": "TP53", "insufficient_evidence": False},
    ])
    _use_store(monkeypatch, _FakeRunStore(stored))
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"inputs": [{"fusion": "BAD"}, {"fusion": "ALK"}, {"fusion": "TP53"}, {"fusion": "KRAS"}]},
    )

    assert response.status_code == 200
    _poll(client, response.json()["status_url"])
    assert [item["fusions"] for item in seen] == [[{"fusion": "ALK"}]]


def test_only_permanent_rejections_return_422_without_starting_a_job(monkeypatch, fresh_jobs):
    called = []

    async def fake_run_pipeline(*args, **kwargs):
        called.append(args)

    stored = _stored_run(annotations=[
        {"gene": "BAD", "insufficient_evidence": True, "error": "'BAD' is not a valid human Ensembl ID."},
    ])
    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post("/v1/runs/run-1/retry", json={"inputs": [{"fusion": "BAD"}]})

    assert response.status_code == 422
    assert "No retryable inputs" in response.json()["detail"]
    assert called == []
    assert store.updated_runs == []


def test_retry_applies_the_annotate_input_cap(monkeypatch, fresh_jobs):
    called = []

    async def fake_run_pipeline(*args, **kwargs):
        called.append(args)

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    _use_store(monkeypatch, _FakeRunStore(_stored_run()))
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"inputs": ["ALK"] * (settings.annotate_max_inputs + 1)},
    )

    assert response.status_code == 422
    assert f"At most {settings.annotate_max_inputs} inputs" in response.json()["detail"]
    assert called == []


@pytest.mark.parametrize("body", [{"inputs": []}, {"inputs": [{"tumor_type": "LUAD"}]}, {}])
def test_retry_validates_inputs_like_a_fresh_request(monkeypatch, fresh_jobs, body):
    _use_store(monkeypatch, _FakeRunStore(_stored_run()))
    client = TestClient(main.app)

    assert client.post("/v1/runs/run-1/retry", json=body).status_code == 422
    assert client.post("/v1/annotate/jobs", json={"fusions": body.get("inputs", None)}).status_code == 422


def test_retry_unknown_run_is_404(monkeypatch, fresh_jobs):
    _use_store(monkeypatch, _FakeRunStore(_stored_run()))
    client = TestClient(main.app)

    response = client.post("/v1/runs/missing/retry", json={"inputs": [{"fusion": "ALK"}]})

    assert response.status_code == 404


def test_concurrent_retry_and_fusion_evidence_attach_do_not_clobber(monkeypatch):
    async def scenario():
        store = _SlowReadRunStore(_stored_run(fusion_evidence=[
            {"fusion": "EML4::ALK", "tumor_type": "LUAD", "retrieval_status": "failed"},
        ]))
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(run_store=store)))
        retried = _result([_annotation("ALK", gene_summary="Retried")])
        evidence = [FusionEvidenceResult(fusion="EML4::ALK", tumor_type="LUAD", pmids=["1"])]

        await asyncio.gather(
            main._persist_retried_annotations(request, "run-1", retried),
            main._persist_fusion_evidence(request, "run-1", evidence),
        )
        return store.stored

    stored = asyncio.run(scenario())

    assert stored["annotations"][0]["gene_summary"] == "Retried"
    assert stored["fusion_evidence"][0]["retrieval_status"] == "ok"
    assert stored["fusion_evidence"][0]["pmids"] == ["1"]


# ---------------------------------------------------------------------------
# Who may retry
# ---------------------------------------------------------------------------


def _auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "run-retry-owner-test-secret")
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


def _retry(client, headers, run_id="run-1"):
    response = client.post(
        f"/v1/runs/{run_id}/retry", json={"inputs": [{"fusion": "ALK"}]}, headers=headers,
    )
    if response.status_code != 200:
        return response, None
    return response, _poll(client, response.json()["status_url"], headers=headers)


@pytest.fixture
def authed_store(monkeypatch, fresh_jobs):
    _auth_settings(monkeypatch)
    _ok_pipeline(monkeypatch)
    store = _FakeRunStore(_stored_run(), owner_email="owner@mskcc.org")
    _use_store(monkeypatch, store)
    return store


def test_owner_can_retry(authed_store):
    response, body = _retry(TestClient(main.app), _session_headers("Owner@MSKCC.org"))

    assert response.status_code == 200
    assert body["status"] == "complete"
    assert authed_store.stored["annotations"][0]["gene_summary"] == "Retried"


def test_other_user_gets_403_and_missing_run_404(authed_store):
    client = TestClient(main.app)
    headers = _session_headers("other@mskcc.org")

    denied, _ = _retry(client, headers)
    missing, _ = _retry(client, headers, run_id="not-a-run")
    inputs_denied = client.get("/v1/runs/run-1/inputs", headers=headers)

    assert denied.status_code == 403
    assert missing.status_code == 404
    assert inputs_denied.status_code == 403
    assert authed_store.updated_runs == []


def test_admin_can_retry_another_users_run(authed_store):
    response, body = _retry(TestClient(main.app), _session_headers("admin@mskcc.org", role="admin"))

    assert response.status_code == 200
    assert body["status"] == "complete"
    assert authed_store.updated_runs


def test_owner_api_key_can_retry(authed_store):
    key = generate_api_key()
    authed_store.keys["key-1"] = ApiKeyRecord(
        id="key-1",
        key_hash=hash_api_key(key),
        owner_email="Owner@MSKCC.org",
        name="script",
        created_at=datetime.now(timezone.utc),
    )

    response, body = _retry(TestClient(main.app), {"Authorization": f"Bearer {key}"})

    assert response.status_code == 200
    assert body["status"] == "complete"
    assert authed_store.updated_runs


def test_ownerless_run_is_admin_only(authed_store):
    authed_store.owner_email = None
    client = TestClient(main.app)

    denied, _ = _retry(client, _session_headers("curator@mskcc.org"))
    allowed, body = _retry(client, _session_headers("admin@mskcc.org", role="admin"))

    assert denied.status_code == 403
    assert allowed.status_code == 200
    assert body["status"] == "complete"


# ---------------------------------------------------------------------------
# GET /v1/runs/{run_id}/inputs
# ---------------------------------------------------------------------------


def test_run_inputs_endpoint_returns_original_submission(monkeypatch):
    request_payload = {
        "fusions": [
            {"fusion": "EML4::ALK", "tumor_type": "LUAD", "five_transcript": "ENST1"},
            "TP53",
        ],
        "mode": "core",
    }
    _use_store(monkeypatch, _FakeRunStore(_stored_run(), request_payload=request_payload))
    client = TestClient(main.app)

    response = client.get("/v1/runs/run-1/inputs")

    assert response.status_code == 200
    inputs = response.json()["inputs"]
    assert inputs[0]["fusion"] == "EML4::ALK"
    assert inputs[0]["tumor_type"] == "LUAD"
    assert inputs[0]["five_transcript"] == "ENST1"
    assert inputs[1]["fusion"] == "TP53"


def test_run_inputs_endpoint_reads_single_gene_requests(monkeypatch):
    _use_store(monkeypatch, _FakeRunStore(
        _stored_run(), request_payload={"gene": "BRAF", "tumor_type": "Melanoma"},
    ))

    response = TestClient(main.app).get("/v1/runs/run-1/inputs")

    assert response.status_code == 200
    assert response.json()["inputs"][0]["fusion"] == "BRAF"
    assert response.json()["inputs"][0]["tumor_type"] == "Melanoma"
