"""REST API tests for annotation endpoints."""

from __future__ import annotations

import time

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

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        **kwargs,
    ):
        seen["fusions"] = fusions
        seen["local_backend"] = local_backend
        seen["run_store"] = run_store
        seen["force_refresh"] = force_refresh
        seen["skip_literature_for_oncokb"] = kwargs.get("skip_literature_for_oncokb")
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
            "skip_literature_for_oncokb": True,
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
    assert seen["skip_literature_for_oncokb"] is True
    assert run_store.saved_runs[0][0] == "run-1"


def test_batch_annotation_endpoint_returns_annotation_result_json(monkeypatch):
    seen = {}
    main.app.state.run_store = FakeRunStore()

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        **kwargs,
    ):
        seen["skip_literature_for_oncokb"] = kwargs.get("skip_literature_for_oncokb")
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
        json={"fusions": ["BRAF", "TP53::BRAF"], "skip_literature_for_oncokb": True},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_id"] == "run-2"
    assert payload["fusions_processed"] == 2
    assert payload["annotations"][0]["gene"] == "BRAF"
    assert payload["annotations"][0]["fusions"] == ["TP53::BRAF"]
    assert seen["skip_literature_for_oncokb"] is True


def test_batch_annotation_endpoint_defaults_to_core_and_oncokb_skip(monkeypatch):
    seen = {}
    main.app.state.run_store = FakeRunStore()

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        **kwargs,
    ):
        seen["mode"] = kwargs.get("mode")
        seen["skip_literature_for_oncokb"] = kwargs.get("skip_literature_for_oncokb")
        return AnnotationResult(
            run_id="run-defaults",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="BRAF")],
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client = TestClient(main.app)

    response = client.post("/v1/annotate", json={"fusions": ["BRAF"]})

    assert response.status_code == 200
    assert seen["mode"] == "core"
    assert seen["skip_literature_for_oncokb"] is True


def test_batch_annotation_endpoint_supports_full_mode_and_full_literature_opt_out(
    monkeypatch,
):
    seen = {}
    main.app.state.run_store = FakeRunStore()

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        **kwargs,
    ):
        seen["mode"] = kwargs.get("mode")
        seen["skip_literature_for_oncokb"] = kwargs.get("skip_literature_for_oncokb")
        return AnnotationResult(
            run_id="run-full",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="BRAF")],
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client = TestClient(main.app)

    response = client.post(
        "/v1/annotate",
        json={
            "fusions": ["BRAF"],
            "mode": "full",
            "skip_literature_for_oncokb": False,
        },
    )

    assert response.status_code == 200
    assert seen["mode"] == "full"
    assert seen["skip_literature_for_oncokb"] is False


def test_single_gene_endpoint_defaults_to_core_and_oncokb_skip(monkeypatch):
    seen = {}
    main.app.state.run_store = FakeRunStore()

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        **kwargs,
    ):
        seen["mode"] = kwargs.get("mode")
        seen["skip_literature_for_oncokb"] = kwargs.get("skip_literature_for_oncokb")
        return AnnotationResult(
            run_id="run-gene-defaults",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="ALK")],
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client = TestClient(main.app)

    response = client.post("/v1/annotate/gene", json={"gene": "ALK"})

    assert response.status_code == 200
    assert seen["mode"] == "core"
    assert seen["skip_literature_for_oncokb"] is True


def test_single_gene_endpoint_supports_full_mode_opt_out(monkeypatch):
    seen = {}
    main.app.state.run_store = FakeRunStore()

    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        **kwargs,
    ):
        seen["mode"] = kwargs.get("mode")
        seen["skip_literature_for_oncokb"] = kwargs.get("skip_literature_for_oncokb")
        return AnnotationResult(
            run_id="run-gene-full",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[GeneAnnotation(gene="ALK")],
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client = TestClient(main.app)

    response = client.post(
        "/v1/annotate/gene",
        json={"gene": "ALK", "mode": "full", "skip_literature_for_oncokb": False},
    )

    assert response.status_code == 200
    assert seen["mode"] == "full"
    assert seen["skip_literature_for_oncokb"] is False


def test_enrichment_job_endpoint_streams_enriched_annotations(monkeypatch):
    async def fake_enrich_gene_annotations(
        annotations,
        local_backend=None,
        on_annotation=None,
    ):
        enriched = [
            GeneAnnotation(
                gene=annotation.gene,
                fusions=annotation.fusions,
                cancer_associated=annotation.cancer_associated,
                gene_class="Tumor suppressor",
                signaling_pathways="p53 pathway",
                supporting_quotes=[{"pmid": "12345", "quote": "TP53 cancer"}],
                evidence_cards=[
                    {
                        "pmid": "12345",
                        "title": "TP53 cancer evidence",
                        "evidence_type": "clinical",
                        "selected_reason": "Verified PMID selected as clinical evidence.",
                    }
                ],
                quality_flags=[
                    {
                        "code": "low_evidence_score",
                        "label": "Low evidence score",
                        "severity": "warning",
                        "detail": "Evidence support score is 0.30.",
                    }
                ],
                timings_ms={"total": 1.5},
            )
            for annotation in annotations
        ]
        for annotation in enriched:
            if on_annotation:
                await on_annotation(annotation)
        return enriched

    monkeypatch.setattr(main, "enrich_gene_annotations", fake_enrich_gene_annotations)
    client = TestClient(main.app)

    create_response = client.post(
        "/v1/annotate/enrichment/jobs",
        json={
            "local_backend": "codex",
            "annotations": [
                {
                    "gene": "TP53",
                    "fusions": ["TP53::BRAF"],
                    "cancer_associated": True,
                    "citations": ["12345"],
                }
            ],
        },
    )

    assert create_response.status_code == 200
    status_url = create_response.json()["status_url"]

    status_payload = None
    for _ in range(20):
        status_response = client.get(status_url)
        assert status_response.status_code == 200
        status_payload = status_response.json()
        if status_payload["status"] == "complete":
            break
        time.sleep(0.01)

    assert status_payload is not None
    assert status_payload["status"] == "complete"
    assert status_payload["annotations_completed"] == 1
    assert status_payload["annotations_total"] == 1
    assert status_payload["annotations"][0]["gene_class"] == "Tumor suppressor"
    assert status_payload["annotations"][0]["supporting_quotes"][0]["quote"] == "TP53 cancer"
    assert status_payload["annotations"][0]["evidence_cards"][0]["evidence_type"] == "clinical"
    assert status_payload["annotations"][0]["quality_flags"][0]["code"] == "low_evidence_score"
    assert status_payload["timings_ms"]["total"] == 1.5
