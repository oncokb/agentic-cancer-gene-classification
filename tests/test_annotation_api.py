"""REST API tests for annotation endpoints."""

from __future__ import annotations

import asyncio
import time

import pytest

from fastapi.testclient import TestClient

from src import auth as auth_module
from src import main
from src.auth import AuthenticatedUser, create_session_token
from src.models.schema import AnnotationResult, FusionEvidenceResult, GeneAnnotation, ResolvedGene
from src.pipeline import orchestrator, result_sanitizer


class FakeRunStore:
    def __init__(self):
        self.saved_runs = []
        # Owner kwargs (owner_email/source) each run was saved with, by run id.
        self.owners = {}

    async def get_run(self, run_id):
        return next((row[3] for row in self.saved_runs if row[0] == run_id), None)

    async def close(self):
        pass

    async def save_run(self, run_id, timestamp, request_payload, result_payload, **owner):
        self.saved_runs.append((run_id, timestamp, request_payload, result_payload))
        self.owners[run_id] = owner


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


@pytest.mark.parametrize("path", ["/v1/annotate", "/v1/annotate/jobs"])
@pytest.mark.parametrize("include", [None, False, True])
def test_inline_fusion_evidence_is_opt_in_and_persisted(monkeypatch, path, include):
    store = FakeRunStore()
    monkeypatch.setattr(main.app.state, "run_store", store, raising=False)
    async def create_store():
        return store

    monkeypatch.setattr(main.RunStore, "create", create_store)
    seen = []
    active = 0
    peak = 0

    async def fake_pipeline(fusions, **kwargs):
        return AnnotationResult(
            run_id="inline", timestamp="2026-10-26", fusions_processed=len(fusions),
            genes_annotated=0, annotations=[],
        )

    async def fake_retrieve(fusion, tumor_type=None):
        nonlocal active, peak
        if path.endswith("/jobs"):
            assert any(job.phase == "fusion_evidence" and job.status == "running"
                       for job, _ in main._annotation_jobs.local.values())
        active += 1
        peak = max(peak, active)
        seen.append((fusion, tumor_type))
        await asyncio.sleep(0.001)
        active -= 1
        if fusion == "BCR::ABL1":
            raise RuntimeError("upstream unavailable")
        return FusionEvidenceResult(
            fusion=fusion, tumor_type=tumor_type, pmids=["123"], retrieved_count=1,
            evidence_cards=[{"pmid": "123", "title": "EML4-ALK fusion in lung cancer", "fusion": fusion}],
        )

    async def no_retractions(pmids):
        return set()

    async def no_aliases(*args):
        return [], []

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", no_retractions)
    monkeypatch.setattr(result_sanitizer, "resolve_fusion_partner_aliases", no_aliases)
    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve)
    monkeypatch.setattr(main.settings, "fusion_evidence_concurrency", 1)
    request = {"fusions": [
        {"fusion": "EML4::ALK", "tumor_type": "LUAD"},
        {"fusion": "EML4::ALK", "tumor_type": "LUAD"},
        "BRAF", "BCR::ABL1",
    ]}
    if include is not None:
        request["include_fusion_evidence"] = include
    with TestClient(main.app) as client:
        response = client.post(path, json=request)
        assert response.status_code == 200
        payload = response.json()
        if path.endswith("/jobs"):
            for _ in range(100):
                status = client.get(payload["status_url"]).json()
                if status["status"] in ("complete", "failed"):
                    break
                time.sleep(0.01)
            assert status["status"] == "complete"
            payload = status["result"]
            assert status["phase"] == "complete"
        if include:
            assert seen == [("EML4::ALK", "LUAD"), ("BCR::ABL1", None)]
            assert payload["timings_ms"]["fusion_evidence"] > 0
            assert payload["timings_ms"]["total"] >= payload["timings_ms"]["fusion_evidence"]
            assert peak == 1
            assert payload["fusion_evidence"][0]["pmids"] == ["123"]
            assert "upstream unavailable" in payload["fusion_evidence"][1]["interpretation"]
        else:
            assert seen == []
            assert payload["fusion_evidence"] == []
            assert "fusion_evidence" not in payload["timings_ms"]
        restored = client.get(f"/v1/annotate/{payload['run_id']}")
        assert restored.status_code == 200
        assert restored.json()["fusion_evidence"] == payload["fusion_evidence"]
        assert store.saved_runs[-1][3]["fusion_evidence"] == payload["fusion_evidence"]


@pytest.mark.parametrize("path", ["/v1/annotate", "/v1/annotate/jobs"])
def test_inline_fusion_evidence_run_is_saved_with_its_owner(monkeypatch, path):
    store = FakeRunStore()

    async def create_store():
        return store

    async def fake_pipeline(fusions, **kwargs):
        return AnnotationResult(
            run_id="inline-owned", timestamp="2026-10-26", fusions_processed=len(fusions),
            genes_annotated=0, annotations=[],
        )

    async def fake_retrieve(fusion, tumor_type=None):
        return FusionEvidenceResult(fusion=fusion, pmids=["123"], retrieved_count=1)

    async def no_retractions(pmids):
        return set()

    async def no_aliases(*args):
        return [], []

    monkeypatch.setattr(main.settings, "auth_enabled", True)
    monkeypatch.setattr(main.settings, "auth_secret_key", "inline-owner-test-secret-123456")
    monkeypatch.setattr(main.settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(main.settings, "allowed_emails", "")
    monkeypatch.setattr(main.settings, "redis_url", "")
    monkeypatch.setattr(auth_module, "_redis_client", None)
    monkeypatch.setattr(main.RunStore, "create", create_store)
    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", no_retractions)
    monkeypatch.setattr(result_sanitizer, "resolve_fusion_partner_aliases", no_aliases)
    monkeypatch.setattr(main, "run_pipeline", fake_pipeline)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_retrieve)
    token = create_session_token(
        AuthenticatedUser(email="Curator@MSKCC.org", name="Curator", domain="mskcc.org")
    )
    with TestClient(main.app) as client:
        client.cookies.set(main.settings.auth_cookie_name, token)
        response = client.post(path, json={"fusions": ["EML4::ALK"], "include_fusion_evidence": True})
        assert response.status_code == 200
        if path.endswith("/jobs"):
            for _ in range(100):
                status = client.get(response.json()["status_url"]).json()
                if status["status"] in ("complete", "failed"):
                    break
                time.sleep(0.01)
            assert status["status"] == "complete"

    # One save, after the evidence was attached, carrying the caller as owner.
    assert [row[0] for row in store.saved_runs] == ["inline-owned"]
    assert store.saved_runs[0][3]["fusion_evidence"][0]["pmids"] == ["123"]
    assert store.owners["inline-owned"] == {"owner_email": "curator@mskcc.org", "source": "session"}


@pytest.mark.parametrize("path", [
    "/v1/annotate", "/v1/annotate/jobs", "/v1/fusion-evidence/jobs",
])
def test_annotation_input_cap_returns_422_before_work(monkeypatch, path):
    monkeypatch.setattr(main.settings, "annotate_max_inputs", 2)

    async def unexpected(*args, **kwargs):
        pytest.fail("Over-limit request must be rejected before work starts")

    monkeypatch.setattr(main, "run_pipeline", unexpected)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", unexpected)
    response = TestClient(main.app).post(path, json={"fusions": ["ALK"] * 3})
    assert response.status_code == 422
    assert "At most 2 inputs may be annotated per request" in response.text


@pytest.mark.parametrize("path", [
    "/v1/annotate", "/v1/annotate/jobs", "/v1/fusion-evidence/jobs",
])
def test_annotation_input_cap_accepts_exact_limit(monkeypatch, path):
    monkeypatch.setattr(main.settings, "annotate_max_inputs", 2)
    store = FakeRunStore()

    async def create_store():
        return store

    async def pipeline(fusions, **kwargs):
        return AnnotationResult(run_id="limit", timestamp="2026-10-26", annotations=[],
                                fusions_processed=len(fusions), genes_annotated=0)

    async def retrieve(fusion, tumor_type=None):
        return FusionEvidenceResult(fusion=fusion)

    monkeypatch.setattr(main.RunStore, "create", create_store)
    monkeypatch.setattr(main, "run_pipeline", pipeline)
    monkeypatch.setattr(main, "retrieve_fusion_evidence", retrieve)
    with TestClient(main.app) as client:
        response = client.post(path, json={"fusions": ["EML4::ALK", "BCR::ABL1"]})
        assert response.status_code == 200
        if path.endswith("/jobs"):
            for _ in range(100):
                status = client.get(response.json()["status_url"]).json()
                if status["status"] in ("complete", "failed"):
                    break
                time.sleep(0.01)
            assert status["status"] == "complete"


def test_fusion_evidence_request_hides_inline_flag():
    schema = main.FusionEvidenceJobRequest.model_json_schema()
    assert "include_fusion_evidence" not in schema["properties"]
    request = main.FusionEvidenceJobRequest(fusions=["EML4::ALK"], include_fusion_evidence=True)
    assert "include_fusion_evidence" not in request.model_dump()


def test_annotation_api_returns_tumor_conflict_warning(monkeypatch):
    monkeypatch.setattr(main.settings, "gene_cache_enabled", False)
    monkeypatch.setattr(main.app.state, "run_store", FakeRunStore(), raising=False)

    async def normalize(inputs):
        return {"ALK": (ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True), inputs)}

    async def annotate(gene, fusions, **kwargs):
        return GeneAnnotation(gene=gene, fusions=fusions)

    monkeypatch.setattr(orchestrator, "normalize_fusions", normalize)
    monkeypatch.setattr(orchestrator, "_annotate_gene", annotate)
    response = TestClient(main.app).post("/v1/annotate", json={"fusions": [
        {"fusion": "ALK", "tumor_type": "LUAD"},
        {"fusion": "ALK", "tumor_type": "melanoma"},
    ]})
    assert response.status_code == 200
    assert response.json()["warnings"] == [
        "ALK: using tumor type 'LUAD'; ignored tumor types: 'melanoma'."
    ]
