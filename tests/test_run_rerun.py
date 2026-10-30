"""POST /v1/runs/{run_id}/rerun: rerun a saved run's request as a new run.

Uses an in-memory run store and a fake pipeline (no MySQL, no LLM calls), so
it asserts exactly what request the new job ran with and what was saved.
"""

from __future__ import annotations

import copy
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pytest
from fastapi.testclient import TestClient

from src import api_keys as api_keys_module
from src import auth as auth_module
from src import main
from src.api_keys import ApiKeyRecord, InMemoryRateLimiter, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser
from src.config import settings
from src.models.schema import AnnotateRequest, AnnotationResult, FusionEvidenceResult, GeneAnnotation
from src.pipeline.run_store import RunStore
from tests.auth_helpers import session_token_for
from tests.test_run_owner import FakeCursor, FakePool

ORIGINAL_ID = "11111111-1111-1111-1111-111111111111"
ORIGINAL_REQUEST = {
    "include_fusion_evidence": True,
    "fusions": [
        {
            "fusion": "EML4::ALK",
            "tumor_type": "LUAD",
            "five_exon": 13,
            "three_exon": 20,
            "five_transcript": "ENST00000318522",
            "three_transcript": "ENST00000389048",
        },
        {"fusion": "TP53", "tumor_type": "breast cancer"},
    ],
    "local_backend": None,
    "force_refresh": True,
    "skip_literature_for_oncokb": True,
    "mode": "core",
}
ORIGINAL_RESULT = {
    "run_id": ORIGINAL_ID,
    "timestamp": "2026-10-01T12:00:00+00:00",
    "fusions_processed": 2,
    "genes_annotated": 0,
    "annotations": [],
}


class MemoryRunStore:
    """RunStore stand-in: saved runs (request, result, owner) plus API keys."""

    def __init__(self):
        self.runs: Dict[str, dict] = {}
        self.keys: Dict[str, ApiKeyRecord] = {}
        self.result_updates: list[str] = []

    @classmethod
    async def create(cls):
        return _STORE

    async def close(self):
        pass

    def seed(self, run_id, request, result=None, owner_email="owner@mskcc.org", source="session"):
        self.runs[run_id] = {
            "request": copy.deepcopy(request),
            "result": copy.deepcopy(result or {**ORIGINAL_RESULT, "run_id": run_id}),
            "owner_email": owner_email,
            "source": source,
        }

    async def save_run(self, run_id, timestamp, request_payload, result_payload, owner_email=None, source=None):
        self.runs[run_id] = {
            "request": request_payload,
            "result": result_payload,
            "owner_email": owner_email,
            "source": source,
        }

    async def get_run(self, run_id):
        run = self.runs.get(run_id)
        return copy.deepcopy(run["result"]) if run else None

    async def get_run_request(self, run_id):
        run = self.runs.get(run_id)
        if run is None:
            return None
        request = copy.deepcopy(run["request"])
        return request if isinstance(request, dict) else {}

    async def update_run_result(self, run_id, result_payload):
        self.result_updates.append(run_id)
        self.runs[run_id]["result"] = result_payload

    async def get_gene_annotation(self, gene, tumor_type=None):
        return None

    async def save_gene_annotation(self, annotation, now, tumor_type=None):
        pass

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        return next((k for k in self.keys.values() if k.key_hash == key_hash), None)

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        pass


_STORE = MemoryRunStore()


@pytest.fixture
def store(monkeypatch):
    global _STORE
    _STORE = MemoryRunStore()
    _STORE.seed(ORIGINAL_ID, ORIGINAL_REQUEST)
    monkeypatch.setattr(main, "RunStore", MemoryRunStore)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "run-rerun-test-secret-123456")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 1000)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth_module, "_redis_client", None)
    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())
    _STORE.pipeline_calls = []

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
        _STORE.pipeline_calls.append(
            {
                "fusions": [item.model_dump() for item in fusions],
                "local_backend": local_backend,
                "force_refresh": force_refresh,
                "skip_literature_for_oncokb": kwargs.get("skip_literature_for_oncokb"),
                "mode": kwargs.get("mode"),
            }
        )
        annotations = [GeneAnnotation(gene=item.fusion.upper(), in_oncokb=True) for item in fusions]
        return AnnotationResult(
            run_id=f"rerun-{len(_STORE.pipeline_calls)}",
            timestamp="2026-10-29T12:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=len(annotations),
            annotations=annotations,
        )

    async def fake_retrieve_fusion_evidence(fusion, tumor_type=None, force_refresh=False):
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, interpretation="ok")

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve_fusion_evidence)
    return _STORE


def _session(client, email="Rerunner@mskcc.org", **overrides):
    user = AuthenticatedUser(email=email, name="Rerunner", domain="mskcc.org", **overrides)
    client.cookies.set(settings.auth_cookie_name, session_token_for(user))


def _api_key_headers(store, owner_email="Scripter@mskcc.org"):
    key = generate_api_key()
    store.keys["key-1"] = ApiKeyRecord(
        id="key-1",
        key_hash=hash_api_key(key),
        owner_email=owner_email,
        name="nightly",
        created_at=datetime.now(timezone.utc),
    )
    return {"Authorization": f"Bearer {key}"}


def _poll(client, status_url, headers=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(status_url, headers=headers).json()
        if body["status"] in ("complete", "failed") or time.monotonic() > deadline:
            return body
        time.sleep(0.02)


def _rerun(client, run_id=ORIGINAL_ID, headers=None, **kwargs):
    response = client.post(f"/v1/runs/{run_id}/rerun", headers=headers, **kwargs)
    assert response.status_code == 200, response.text
    job = _poll(client, response.json()["status_url"], headers=headers)
    assert job["status"] == "complete", job
    return response.json(), job


def test_rerun_starts_a_job_with_the_original_request(store):
    original = copy.deepcopy(store.runs[ORIGINAL_ID])
    with TestClient(main.app) as client:
        _session(client)
        created, job = _rerun(client)

    new_run_id = job["result"]["run_id"]
    assert created["job_id"] == job["job_id"]
    assert created["status_url"] == f"/v1/annotate/jobs/{created['job_id']}"
    assert created["rerun_of"] == ORIGINAL_ID
    assert created["mode"] == "core"
    expected = AnnotateRequest.model_validate({**ORIGINAL_REQUEST, "force_refresh": False})
    assert created["fusions"] == [item.model_dump() for item in expected.fusions]

    # The pipeline ran with the original inputs (tumor types, transcripts,
    # exons) and options; force_refresh defaults to cached results.
    assert store.pipeline_calls == [
        {
            "fusions": [item.model_dump() for item in expected.fusions],
            "local_backend": None,
            "force_refresh": False,
            "skip_literature_for_oncokb": True,
            "mode": "core",
        }
    ]
    saved = store.runs[new_run_id]
    assert {k: v for k, v in saved["request"].items() if k != "rerun_of"} == expected.model_dump()
    assert saved["request"]["include_fusion_evidence"] is True
    assert saved["request"]["fusions"][0]["five_transcript"] == "ENST00000318522"
    assert saved["request"]["fusions"][0]["tumor_type"] == "LUAD"
    assert saved["request"]["rerun_of"] == ORIGINAL_ID
    # include_fusion_evidence was honored inline, like a fresh job.
    assert [item["fusion"] for item in saved["result"]["fusion_evidence"]] == ["EML4::ALK"]

    # The original run is untouched.
    assert store.runs[ORIGINAL_ID] == original
    assert ORIGINAL_ID not in store.result_updates


def test_rerun_new_run_is_owned_by_the_caller(store):
    with TestClient(main.app) as client:
        _session(client, email="Rerunner@mskcc.org")
        _, job = _rerun(client)

    saved = store.runs[job["result"]["run_id"]]
    assert saved["owner_email"] == "rerunner@mskcc.org"
    assert saved["source"] == "rerun"
    assert store.runs[ORIGINAL_ID]["owner_email"] == "owner@mskcc.org"


@pytest.mark.parametrize("force_refresh", [True, False])
def test_rerun_propagates_force_refresh(store, force_refresh):
    with TestClient(main.app) as client:
        _session(client)
        _, job = _rerun(client, json={"force_refresh": force_refresh})

    assert store.pipeline_calls[0]["force_refresh"] is force_refresh
    assert store.runs[job["result"]["run_id"]]["request"]["force_refresh"] is force_refresh


def test_rerun_unknown_run_is_404(store):
    with TestClient(main.app) as client:
        _session(client)
        response = client.post("/v1/runs/does-not-exist/rerun")

    assert response.status_code == 404
    assert response.json()["detail"] == "Run not found"
    assert store.pipeline_calls == []


@pytest.mark.parametrize(
    "stored_request",
    [{}, None, [], {"fusions": []}, {"fusions": [{"tumor_type": "LUAD"}]}, {"something": "else"}],
    ids=["empty", "null", "list", "no-inputs", "malformed-input", "unknown-shape"],
)
def test_rerun_without_a_rerunnable_request_is_a_clear_422(store, stored_request):
    store.seed("old-run", stored_request)
    with TestClient(main.app) as client:
        _session(client)
        response = client.post("/v1/runs/old-run/rerun")

    assert response.status_code == 422
    assert "can't be rerun" in response.json()["detail"]
    assert store.pipeline_calls == []


def test_rerun_of_a_single_gene_run(store):
    store.seed("gene-run", {"gene": "ALK", "tumor_type": "LUAD", "local_backend": None,
                            "force_refresh": False, "skip_literature_for_oncokb": False, "mode": "full"})
    with TestClient(main.app) as client:
        _session(client)
        created, _ = _rerun(client, run_id="gene-run")

    assert [(item["fusion"], item["tumor_type"]) for item in created["fusions"]] == [("ALK", "LUAD")]
    assert store.pipeline_calls[0]["fusions"][0]["fusion"] == "ALK"


def test_rerun_of_a_rerun_reuses_the_inputs(store):
    with TestClient(main.app) as client:
        _session(client)
        _, first = _rerun(client)
        first_id = first["result"]["run_id"]
        _, second = _rerun(client, run_id=first_id)

    assert store.pipeline_calls[1]["fusions"] == store.pipeline_calls[0]["fusions"]
    assert store.runs[second["result"]["run_id"]]["request"]["rerun_of"] == first_id


def test_rerun_enforces_the_input_cap(store, monkeypatch):
    monkeypatch.setattr(settings, "annotate_max_inputs", 1)
    with TestClient(main.app) as client:
        _session(client)
        response = client.post(f"/v1/runs/{ORIGINAL_ID}/rerun")

    assert response.status_code == 422
    assert "At most 1 inputs" in response.json()["detail"]
    assert store.pipeline_calls == []


def test_rerun_unauthenticated_is_401(store):
    with TestClient(main.app) as client:
        response = client.post(f"/v1/runs/{ORIGINAL_ID}/rerun")

    assert response.status_code == 401
    assert store.pipeline_calls == []


@pytest.mark.parametrize("status", ["pending", "disabled"])
def test_rerun_inactive_account_is_403(store, status):
    with TestClient(main.app) as client:
        _session(client, status=status)
        response = client.post(f"/v1/runs/{ORIGINAL_ID}/rerun")

    assert response.status_code == 403
    assert store.pipeline_calls == []


def test_rerun_with_api_key_is_owned_by_the_key_owner(store):
    with TestClient(main.app) as client:
        headers = _api_key_headers(store, owner_email="Scripter@mskcc.org")
        _, job = _rerun(client, headers=headers)

    saved = store.runs[job["result"]["run_id"]]
    assert saved["owner_email"] == "scripter@mskcc.org"
    assert saved["source"] == "rerun"


# ---------------------------------------------------------------------------
# RunStore.get_run_request against a fake cursor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rows,expected",
    [
        ([], None),
        ([('{"fusions": [{"fusion": "ALK"}]}',)], {"fusions": [{"fusion": "ALK"}]}),
        ([("null",)], {}),
        ([("[1, 2]",)], {}),
        ([(None,)], {}),
    ],
    ids=["unknown", "object", "json-null", "non-object", "sql-null"],
)
async def test_get_run_request_reads_only_the_request(rows, expected):
    cursor = FakeCursor(rows=rows)

    assert await RunStore(FakePool(cursor)).get_run_request("run-1") == expected
    assert cursor.executed == [("SELECT request_json FROM runs WHERE run_id = %s", ("run-1",))]
