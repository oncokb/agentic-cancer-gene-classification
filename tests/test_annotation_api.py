"""REST API tests for annotation endpoints."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src import main
from src.models.schema import AnnotationResult, GeneAnnotation


class FakeRunStore:
    def __init__(self):
        self.saved_runs = []

    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        self.saved_runs.append((run_id, timestamp, request_payload, result_payload))


def test_single_gene_annotation_endpoint_returns_result_card_json(monkeypatch):
    seen = {}
    run_store = FakeRunStore()
    main.app.state.run_store = run_store

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False):
        seen["fusions"] = fusions
        seen["local_backend"] = local_backend
        seen["run_store"] = run_store
        seen["force_refresh"] = force_refresh
        return AnnotationResult(
            run_id="run-1",
            timestamp="2026-07-31T14:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[
                GeneAnnotation(
                    gene="ALK",
                    fusions=[],
                    in_oncokb=True,
                    cancer_associated=True,
                    cancer_association_rationale="Known oncogenic kinase.",
                    gene_class="Receptor tyrosine kinase",
                    signaling_pathways="MAPK; PI3K",
                    gene_summary="ALK is a receptor tyrosine kinase with oncogenic alterations.",
                    citations=["12345"],
                    retrieval_count=3,
                    retrieved_pmids=["12345", "67890"],
                    evidence_support_score=0.92,
                    cache_status="refreshed",
                )
            ],
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client = TestClient(main.app)

    response = client.post(
        "/v1/annotate/gene",
        json={
            "gene": "ALK",
            "tumor_type": "LUAD",
            "local_backend": "codex",
            "force_refresh": True,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["gene"] == "ALK"
    assert payload["fusions"] == []
    assert payload["in_oncokb"] is True
    assert payload["cancer_associated"] is True
    assert payload["gene_class"] == "Receptor tyrosine kinase"
    assert payload["signaling_pathways"] == "MAPK; PI3K"
    assert payload["gene_summary"] == "ALK is a receptor tyrosine kinase with oncogenic alterations."
    assert payload["citations"] == ["12345"]
    assert payload["retrieval_count"] == 3
    assert payload["retrieved_pmids"] == ["12345", "67890"]
    assert payload["evidence_support_score"] == 0.92
    assert payload["cache_status"] == "refreshed"

    assert seen["fusions"][0].fusion == "ALK"
    assert seen["fusions"][0].tumor_type == "LUAD"
    assert seen["local_backend"] == "codex"
    assert seen["run_store"] is run_store
    assert seen["force_refresh"] is True
    assert run_store.saved_runs[0][0] == "run-1"


def test_batch_annotation_endpoint_returns_annotation_result_json(monkeypatch):
    main.app.state.run_store = FakeRunStore()

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False):
        return AnnotationResult(
            run_id="run-2",
            timestamp="2026-07-31T14:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="BRAF", fusions=["TP53::BRAF"])],
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client = TestClient(main.app)

    response = client.post(
        "/v1/annotate",
        json={"fusions": ["BRAF", "TP53::BRAF"]},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-2"
    assert payload["fusions_processed"] == 2
    assert payload["annotations"][0]["gene"] == "BRAF"
    assert payload["annotations"][0]["fusions"] == ["TP53::BRAF"]
