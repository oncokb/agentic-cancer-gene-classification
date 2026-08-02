"""API-level tests for run persistence + GET /v1/annotate/{run_id}.

Mocks run_pipeline (no real LLM calls) but exercises the real MySQL-backed
run store end-to-end via the app's lifespan, so these are skipped cleanly
if MySQL isn't reachable.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import main
from src.models.schema import AnnotationResult

_FAKE_RUN_ID = "22222222-2222-2222-2222-222222222222"


async def _fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
    return AnnotationResult(
        run_id=_FAKE_RUN_ID,
        timestamp="2026-07-30T19:24:12.406639+00:00",
        fusions_processed=1,
        genes_annotated=0,
        annotations=[],
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "run_pipeline", _fake_run_pipeline)
    try:
        with TestClient(main.app) as test_client:
            yield test_client
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")


def test_post_annotate_persists_and_get_returns_it(client):
    post_response = client.post("/v1/annotate", json={"fusions": ["TP53::BRAF"]})
    assert post_response.status_code == 200
    run_id = post_response.json()["run_id"]

    get_response = client.get(f"/v1/annotate/{run_id}")

    assert get_response.status_code == 200
    assert get_response.json()["run_id"] == run_id


def test_get_unknown_run_id_returns_404(client):
    response = client.get("/v1/annotate/does-not-exist")

    assert response.status_code == 404
