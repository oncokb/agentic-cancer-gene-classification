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


def _merge(stored, incoming, targets, kind="errors", warnings=()):
    """`targets` is a {submitted spelling: row key} map, or a set of row genes
    each submitted under its own name."""
    if not isinstance(targets, dict):
        targets = {target: target for target in targets}
    return main._merge_retried_annotations(
        copy.deepcopy(stored), _dump(incoming), list(warnings), targets, kind,
    )


def test_merge_replaces_only_targeted_annotations_and_keeps_fusion_evidence():
    stored = _stored_run()

    merged = _merge(
        stored,
        [_annotation("ALK", cancer_associated=True, gene_summary="Retried")],
        {"ALK"},
        warnings=["ALK: new note.", "KRAS: unrelated note."],
    )

    assert [item["gene"] for item in merged["annotations"]] == ["ALK", "TP53"]
    assert merged["annotations"][0]["gene_summary"] == "Retried"
    assert merged["annotations"][1] == stored["annotations"][1]
    assert merged["warnings"] == stored["warnings"] + ["ALK: new note."]
    assert merged["fusion_evidence"] == stored["fusion_evidence"]
    assert merged["genes_annotated"] == 2
    assert merged["run_id"] == "run-1"
    assert merged["fusions_processed"] == 2


def test_merge_never_writes_rows_for_genes_that_were_not_submitted():
    good_tp53 = {"gene": "TP53", "insufficient_evidence": False, "cancer_associated": True}
    stored = _stored_run(annotations=[
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        good_tp53,
    ])

    # The pipeline also returned TP53 (equally ranked) and KRAS (new).
    merged = _merge(
        stored,
        [
            _annotation("ALK", gene_summary="Retried"),
            _annotation("TP53", gene_summary="Recomputed"),
            _annotation("KRAS"),
        ],
        {"ALK"},
    )

    assert [item["gene"] for item in merged["annotations"]] == ["ALK", "TP53"]
    assert merged["annotations"][1] == good_tp53
    assert merged["genes_annotated"] == 2


def test_merge_skips_a_target_that_is_no_longer_eligible():
    # Another retry fixed ALK while this one was running.
    fixed = {"gene": "ALK", "insufficient_evidence": False, "gene_summary": "Fixed"}
    stored = _stored_run(annotations=[fixed])

    merged = _merge(stored, [_annotation("ALK", gene_summary="Mine")], {"ALK"})

    assert merged["annotations"] == [fixed]


def test_merge_never_replaces_a_target_with_a_worse_result():
    stored = _stored_run(annotations=[
        {"gene": "BRAF", "insufficient_evidence": True},
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
    ])

    no_result = _merge(
        stored,
        [_annotation("BRAF", insufficient_evidence=True, error="Synthesis error: again")],
        {"BRAF"},
        kind="no_result",
    )
    errors = _merge(
        stored,
        [_annotation("ALK", insufficient_evidence=True, error="Synthesis error: rate limit")],
        {"ALK"},
    )

    assert no_result["annotations"] == stored["annotations"]
    # An error replaced by a newer error is fine: it is no worse.
    assert errors["annotations"][1]["error"] == "Synthesis error: rate limit"


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

    merged = _merge(stored, [retried], {"ENSG00000157764"})
    assert merged["annotations"][0]["resolved_inputs"][0]["input"] == "ENSG00000157764"

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
            "fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}],
            "mode": "core",
            "local_backend": "codex",
        },
    )
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry",
        json={"kind": "errors", "inputs": [{"fusion": "ALK", "tumor_type": "LUAD"}]},
    )

    assert response.status_code == 200
    body = _poll(client, response.json()["status_url"])
    assert body["status"] == "complete"
    assert body["result"]["run_id"] == "run-1"
    assert seen == [{
        "fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}],
        "force_refresh": False,
        "local_backend": "codex",
        "mode": "core",
    }]
    run_id, updated, sync_genes_annotated = store.updated_runs[-1]
    assert run_id == "run-1"
    assert sync_genes_annotated is True
    assert updated["annotations"][0]["gene_summary"] == "Retried"
    assert updated["annotations"][1]["gene"] == "TP53"
    assert updated["fusion_evidence"] == _stored_run()["fusion_evidence"]


@pytest.mark.parametrize("override,expected", [(None, True), (False, False)])
def test_no_result_retry_defaults_to_force_refresh(monkeypatch, fresh_jobs, override, expected):
    seen = []
    _ok_pipeline(monkeypatch, seen, annotations=[_annotation("BRAF")])
    _use_store(monkeypatch, _FakeRunStore(_stored_run(annotations=[
        {"gene": "BRAF", "insufficient_evidence": True},
    ])))
    client = TestClient(main.app)
    body = {"kind": "no_result", "inputs": ["BRAF"]}
    if override is not None:
        body["force_refresh"] = override

    response = client.post("/v1/runs/run-1/retry", json=body)

    assert _poll(client, response.json()["status_url"])["status"] == "complete"
    assert seen[0]["force_refresh"] is expected


def test_failed_retry_job_leaves_the_run_unchanged(monkeypatch, fresh_jobs):
    async def failing_pipeline(fusions, **kwargs):
        await kwargs["on_annotation"](_annotation("ALK", gene_summary="partial"))
        raise RuntimeError("upstream exploded")

    monkeypatch.setattr(main, "run_pipeline", failing_pipeline)
    store = _FakeRunStore(_stored_run())
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post("/v1/runs/run-1/retry", json={"kind": "errors", "inputs": ["ALK"]})

    body = _poll(client, response.json()["status_url"])
    assert body["status"] == "failed"
    assert store.updated_runs == []
    assert store.stored == _stored_run()


def _rejected(monkeypatch, stored, body):
    called = []

    async def fake_run_pipeline(*args, **kwargs):
        called.append(args)

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    response = TestClient(main.app).post("/v1/runs/run-1/retry", json=body)
    assert called == [], "no part of a rejected request is processed"
    assert store.updated_runs == []
    assert store.stored == stored
    return response


def test_fusion_with_a_permanently_rejected_partner_is_rejected(monkeypatch, fresh_jobs):
    # Codex review case 1: ALK failed transiently, BAD was rejected permanently.
    stored = _stored_run(annotations=[
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        {"gene": "BAD", "insufficient_evidence": True, "error": "'BAD' is not a valid human Ensembl ID."},
    ])

    response = _rejected(monkeypatch, stored, {"kind": "errors", "inputs": ["ALK::BAD"]})

    assert response.status_code == 422
    assert "'ALK::BAD' is a fusion" in response.json()["detail"]


def test_fusion_with_an_already_good_partner_is_rejected(monkeypatch, fresh_jobs):
    # Codex review case 2: ALK failed, TP53 already has a good result.
    response = _rejected(
        monkeypatch, _stored_run(), {"kind": "errors", "inputs": [{"fusion": "ALK::TP53"}]},
    )

    assert response.status_code == 422
    assert "'ALK::TP53' is a fusion" in response.json()["detail"]


@pytest.mark.parametrize(
    "inputs,fragment",
    [
        (["ALK", "TP53"], "'TP53' is not a failed gene"),
        (["ALK", "KRAS"], "'KRAS' is not a failed gene"),
        (["ALK", "BAD"], "'BAD' was rejected permanently"),
        ([{"fusion": "ALK", "five_exon": 20}], "has breakpoint fields"),
    ],
)
def test_any_ineligible_input_rejects_the_whole_request(monkeypatch, fresh_jobs, inputs, fragment):
    stored = _stored_run(annotations=[
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        {"gene": "BAD", "insufficient_evidence": True, "error": "'BAD' is not a valid human Ensembl ID."},
        {"gene": "TP53", "insufficient_evidence": False},
    ])

    response = _rejected(monkeypatch, stored, {"kind": "errors", "inputs": inputs})

    assert response.status_code == 422
    assert fragment in response.json()["detail"]


@pytest.mark.parametrize(
    "kind,gene,fragment",
    [("errors", "BRAF", "'BRAF' is not a failed gene"), ("no_result", "ALK", "'ALK' is not a no-result gene")],
)
def test_kind_selects_which_rows_are_eligible(monkeypatch, fresh_jobs, kind, gene, fragment):
    stored = _stored_run(annotations=[
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        {"gene": "BRAF", "insufficient_evidence": True},
    ])

    response = _rejected(monkeypatch, stored, {"kind": kind, "inputs": [gene]})

    assert response.status_code == 422
    assert fragment in response.json()["detail"]


def test_retry_applies_the_annotate_input_cap(monkeypatch, fresh_jobs):
    response = _rejected(
        monkeypatch,
        _stored_run(),
        {"kind": "errors", "inputs": ["ALK"] * (settings.annotate_max_inputs + 1)},
    )

    assert response.status_code == 422
    assert f"At most {settings.annotate_max_inputs} inputs" in response.json()["detail"]


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "errors", "inputs": []},
        {"kind": "errors", "inputs": [{"tumor_type": "LUAD"}]},
        {"kind": "errors"},
        {"inputs": ["ALK"]},
        {"kind": "everything", "inputs": ["ALK"]},
    ],
)
def test_retry_validates_the_request(monkeypatch, fresh_jobs, body):
    assert _rejected(monkeypatch, _stored_run(), body).status_code == 422


def test_retry_unknown_run_is_404(monkeypatch, fresh_jobs):
    _use_store(monkeypatch, _FakeRunStore(_stored_run()))
    client = TestClient(main.app)

    response = client.post("/v1/runs/missing/retry", json={"kind": "errors", "inputs": ["ALK"]})

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
            main._persist_retried_annotations(
                request, "run-1", retried, {"ALK": "ALK"}, "errors",
            ),
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
        f"/v1/runs/{run_id}/retry",
        json={"kind": "errors", "inputs": [{"fusion": "ALK"}]},
        headers=headers,
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


# ---------------------------------------------------------------------------
# Retrying a row by a spelling it was resolved from
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    ["CD246", "cd246", "ENSG00000171094", "ENST00000389048", "NM_004304.5"],
)
def test_retry_by_a_resolved_spelling_updates_the_saved_row(monkeypatch, fresh_jobs, spelling):
    # The run saved the failed ALK row under its canonical gene; the user had
    # submitted an alias, an Ensembl ID or a RefSeq accession.
    stored_resolution = {"input": spelling.upper(), "resolved": "ALK"}
    stored = _stored_run(annotations=[
        {
            "gene": "ALK",
            "insufficient_evidence": True,
            "error": "Synthesis error: timeout",
            "resolved_inputs": [stored_resolution],
        },
        {"gene": "TP53", "insufficient_evidence": False, "cancer_associated": True},
    ])
    seen = []
    _ok_pipeline(monkeypatch, seen, annotations=[_annotation(
        "ALK",
        gene_summary="Retried",
        resolved_inputs=[{"input": spelling, "resolved": "ALK"}],
    )])
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry", json={"kind": "errors", "inputs": [spelling]},
    )

    assert response.status_code == 200
    assert _poll(client, response.json()["status_url"])["status"] == "complete"
    assert [item["fusions"] for item in seen] == [[{"fusion": spelling}]]
    alk = store.stored["annotations"][0]
    assert alk["gene"] == "ALK"
    assert alk["gene_summary"] == "Retried"
    assert alk["error"] is None
    assert spelling.upper() in {item["input"].upper() for item in alk["resolved_inputs"]}
    assert store.stored["annotations"][1] == stored["annotations"][1]
    assert store.stored["genes_annotated"] == 2


def test_retry_by_spelling_keeps_the_rows_other_resolved_inputs(monkeypatch, fresh_jobs):
    stored = _stored_run(annotations=[{
        "gene": "ALK",
        "insufficient_evidence": True,
        "error": "Synthesis error: timeout",
        "resolved_inputs": [
            {"input": "CD246", "resolved": "ALK"},
            {"input": "ENSG00000171094", "resolved": "ALK"},
        ],
    }])
    _ok_pipeline(monkeypatch, annotations=[_annotation(
        "ALK", gene_summary="Retried", resolved_inputs=[{"input": "CD246", "resolved": "ALK"}],
    )])
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post("/v1/runs/run-1/retry", json={"kind": "errors", "inputs": ["CD246"]})

    assert _poll(client, response.json()["status_url"])["status"] == "complete"
    alk = store.stored["annotations"][0]
    assert alk["gene_summary"] == "Retried"
    assert [item["input"] for item in alk["resolved_inputs"]] == ["CD246", "ENSG00000171094"]


def test_spelling_matching_more_than_one_eligible_row_is_rejected(monkeypatch, fresh_jobs):
    stored = _stored_run(annotations=[
        {
            "gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout",
            "resolved_inputs": [{"input": "SHARED1", "resolved": "ALK"}],
        },
        {
            "gene": "LTK", "insufficient_evidence": True, "error": "Synthesis error: timeout",
            "resolved_inputs": [{"input": "SHARED1", "resolved": "LTK"}],
        },
    ])

    response = _rejected(monkeypatch, stored, {"kind": "errors", "inputs": ["ALK", "SHARED1"]})

    assert response.status_code == 422
    assert "'SHARED1' matches more than one failed row (ALK, LTK)" in response.json()["detail"]


def test_unmappable_spelling_is_rejected(monkeypatch, fresh_jobs):
    stored = _stored_run(annotations=[{
        "gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout",
        "resolved_inputs": [{"input": "CD246", "resolved": "ALK"}],
    }])

    response = _rejected(monkeypatch, stored, {"kind": "errors", "inputs": ["CD246", "ENSG00000000001"]})

    assert response.status_code == 422
    assert "'ENSG00000000001' is not a failed gene" in response.json()["detail"]


def test_retry_by_spelling_routes_by_row_key_without_resolved_inputs(monkeypatch, fresh_jobs):
    # Even if the retried annotation doesn't echo the submitted spelling, it is
    # written to the row the spelling was validated against.
    stored = _stored_run(annotations=[{
        "gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout",
        "resolved_inputs": [{"input": "CD246", "resolved": "ALK"}],
    }])
    _ok_pipeline(monkeypatch, annotations=[_annotation("ALK", gene_summary="Retried")])
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post("/v1/runs/run-1/retry", json={"kind": "errors", "inputs": ["CD246"]})

    assert _poll(client, response.json()["status_url"])["status"] == "complete"
    assert store.stored["annotations"][0]["gene_summary"] == "Retried"
    assert store.stored["annotations"][0]["resolved_inputs"] == [{"input": "CD246", "resolved": "ALK"}]


# ---------------------------------------------------------------------------
# A spelling that resolves to a different gene on retry
# ---------------------------------------------------------------------------


def test_spelling_that_now_resolves_to_another_gene_leaves_the_row_unchanged(
    monkeypatch, fresh_jobs,
):
    # The saved ALK row was submitted as CD246; on retry CD246 resolves to LTK.
    alk = {
        "gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout",
        "resolved_inputs": [{"input": "CD246", "resolved": "ALK"}],
    }
    stored = _stored_run(annotations=[
        alk,
        {"gene": "BRAF", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
        {"gene": "TP53", "insufficient_evidence": False},
    ])
    _ok_pipeline(monkeypatch, annotations=[
        _annotation("LTK", gene_summary="Wrong gene",
                    resolved_inputs=[{"input": "CD246", "resolved": "LTK"}]),
        _annotation("BRAF", gene_summary="Retried"),
    ])
    store = _FakeRunStore(stored)
    _use_store(monkeypatch, store)
    client = TestClient(main.app)

    response = client.post(
        "/v1/runs/run-1/retry", json={"kind": "errors", "inputs": ["CD246", "BRAF"]},
    )

    body = _poll(client, response.json()["status_url"])
    assert body["status"] == "complete"
    assert body["retry_outcomes"] == [{
        "input": "CD246",
        "row_gene": "ALK",
        "resolved_gene": "LTK",
        "message": "CD246 now resolves to LTK, not ALK; ALK was not updated",
    }]
    rows = store.stored["annotations"]
    assert rows[0] == alk, "the ALK row is left exactly as it was"
    assert rows[1]["gene_summary"] == "Retried", "other genes' results still apply"
    assert [row["gene"] for row in rows] == ["ALK", "BRAF", "TP53"], "no LTK row is created"
    assert store.stored["genes_annotated"] == 3


def test_merge_checks_gene_identity_for_a_row_retried_by_its_own_gene():
    stored = _stored_run(annotations=[
        {"gene": "ALK", "insufficient_evidence": True, "error": "Synthesis error: timeout"},
    ])
    outcomes = []

    merged = main._merge_retried_annotations(
        copy.deepcopy(stored),
        _dump([_annotation("LTK", resolved_inputs=[{"input": "ALK", "resolved": "LTK"}])]),
        [],
        {"ALK": "ALK"},
        "errors",
        outcomes,
    )

    assert merged["annotations"] == stored["annotations"]
    assert [outcome.message for outcome in outcomes] == [
        "ALK now resolves to LTK, not ALK; ALK was not updated",
    ]


def test_unresolved_row_takes_its_first_resolution():
    # A row stored under its raw input (its lookup failed) has no gene yet:
    # resolving it on retry is not a change of gene.
    stored = _stored_run(annotations=[{
        "gene": "ENSG00000157764", "insufficient_evidence": True,
        "error": "Unresolvable gene symbol — Ensembl lookup temporarily unavailable (503); retry later.",
    }])
    outcomes = []

    merged = main._merge_retried_annotations(
        copy.deepcopy(stored),
        _dump([_annotation("BRAF", resolved_inputs=[{"input": "ENSG00000157764", "resolved": "BRAF"}])]),
        [],
        {"ENSG00000157764": "ENSG00000157764"},
        "errors",
        outcomes,
    )

    assert merged["annotations"][0]["gene"] == "BRAF"
    assert outcomes == []
