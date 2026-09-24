"""End-to-end additivity check: with default settings, the OpenEvidence
sidecar and PMID distillation are off everywhere.

Boots the real FastAPI app (TestClient, lifespan not run, so no MySQL) and
drives POST /v1/annotate through the real run_pipeline -> _annotate_gene ->
synthesize_gene_annotation path. Only the external edges are faked (HGNC
normalization, OncoKB, PubMed retrieval, paper selection, the synthesis LLM
call, the retraction check), and any outbound httpx.AsyncClient request is
recorded and refused, so a stray OpenEvidence call would show up here.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import Settings, settings
from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import openevidence, orchestrator, result_sanitizer, synthesis

_RAW_ABSTRACT = "ALK rearrangements drive oncogenic signaling in lung adenocarcinoma."


class _RecordingRunStore:
    def __init__(self):
        self.saved_runs = []
        self.saved_gene_annotations = []
        self.pmid_evidence_reads = []
        self.pmid_evidence_writes = []

    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        self.saved_runs.append(run_id)

    async def get_gene_annotation(self, gene, tumor_type=None):
        return None

    async def save_gene_annotation(self, annotation, started_at, tumor_type=None):
        self.saved_gene_annotations.append(annotation.gene)

    async def get_pmid_evidence_batch(self, pmids):
        self.pmid_evidence_reads.append(list(pmids))
        return {}

    async def save_pmid_evidence_batch(self, records):
        self.pmid_evidence_writes.append(list(records))


@pytest.fixture
def default_off_app(monkeypatch):
    # Pin the flags to the Settings class defaults so a developer's local
    # .env can't flip them on and mask a regression.
    fields = Settings.model_fields
    monkeypatch.setattr(settings, "openevidence_enabled", fields["openevidence_enabled"].default)
    monkeypatch.setattr(
        settings, "pmid_distillation_enabled", fields["pmid_distillation_enabled"].default
    )

    seen = {
        "openevidence_clients": 0,
        "distillation_calls": 0,
        "http_requests": [],
        "synthesis_prompts": [],
    }

    def _record_openevidence_client(self, *args, **kwargs):
        seen["openevidence_clients"] += 1
        raise AssertionError("OpenEvidenceClient must not be constructed when disabled")

    monkeypatch.setattr(openevidence.OpenEvidenceClient, "__init__", _record_openevidence_client)

    async def _refuse_http(self, request, *args, **kwargs):
        seen["http_requests"].append(str(request.url))
        raise httpx.ConnectError("network disabled in test", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _refuse_http)

    async def _record_distillation(*args, **kwargs):
        seen["distillation_calls"] += 1

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", _record_distillation)

    async def fake_normalize_fusions(inputs):
        return {
            "ALK": (
                ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
                list(inputs),
            )
        }

    async def fake_check_oncokb_membership(gene, lookup=None):
        return True

    async def fake_retrieve_literature(*args, **kwargs):
        return (
            [
                LiteratureRecord(
                    pmid="30902613",
                    title="ALK fusions in NSCLC",
                    abstract=_RAW_ABSTRACT,
                    journal="J Clin Oncol",
                    publication_types=["Journal Article"],
                )
            ],
            1,
        )

    async def fake_select_papers(gene, records, *args, **kwargs):
        return records

    async def fake_complete_with_tool(*, system, user, **kwargs):
        seen["synthesis_prompts"].append((system, user))
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "ALK rearrangements are oncogenic drivers.",
            "gene_summary": "ALK is an oncogenic receptor tyrosine kinase (PMID 30902613).",
            "citations": ["30902613"],
        }

    async def no_retractions(*args, **kwargs):
        return set()

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "get_msk_genie_prevalence", lambda gene: None)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(orchestrator, "find_retracted_annotation_pmids", no_retractions)
    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", no_retractions)
    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)

    run_store = _RecordingRunStore()
    monkeypatch.setattr(main.app.state, "run_store", run_store, raising=False)
    return TestClient(main.app), run_store, seen


def test_default_settings_disable_openevidence_and_pmid_distillation():
    assert Settings.model_fields["openevidence_enabled"].default is False
    assert Settings.model_fields["pmid_distillation_enabled"].default is False


def test_default_off_annotation_never_touches_openevidence_or_pmid_evidence(default_off_app):
    client, run_store, seen = default_off_app

    response = client.post(
        "/v1/annotate",
        json={"fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}]},
    )

    assert response.status_code == 200, response.text
    annotation = response.json()["annotations"][0]
    assert annotation["gene"] == "ALK"
    assert annotation["error"] is None
    assert annotation["citations"] == ["30902613"]
    assert seen["synthesis_prompts"], "the real synthesis path should have run"

    # (a) no OpenEvidence client and no outbound HTTP at all
    assert seen["openevidence_clients"] == 0
    assert seen["http_requests"] == []
    assert "openevidence" not in annotation["timings_ms"]
    assert not any("openevidence" in key for key in annotation)

    # (b) no distillation task and no pmid_evidence read or write
    assert seen["distillation_calls"] == 0
    assert run_store.pmid_evidence_reads == []
    assert run_store.pmid_evidence_writes == []

    # The synthesis prompt carries the raw abstract, not a distilled summary,
    # and no OpenEvidence section or instruction.
    system_prompt, user_prompt = seen["synthesis_prompts"][0]
    assert f"Abstract: {_RAW_ABSTRACT}" in user_prompt
    assert "Summary:" not in user_prompt
    assert "OpenEvidence" not in user_prompt
    assert "OpenEvidence" not in system_prompt

    # Core writes still happen exactly as on main.
    assert run_store.saved_gene_annotations == ["ALK"]
    assert len(run_store.saved_runs) == 1


def test_default_off_openevidence_sidecar_endpoint_is_unavailable(default_off_app):
    client, _, seen = default_off_app

    response = client.get(
        "/v1/genes/ALK/openevidence",
        params={"tumor_type": "LUAD", "fusion": "EML4::ALK", "cancer_associated": "true"},
    )

    # (c)
    assert response.status_code == 200
    assert response.json() == {"available": False, "distilled": None, "error": None}
    assert seen["openevidence_clients"] == 0
    assert seen["http_requests"] == []


def test_default_off_config_endpoint_reports_openevidence_disabled(default_off_app):
    client, _, _ = default_off_app

    response = client.get("/v1/dev/status")

    # (d)
    assert response.status_code == 200
    assert response.json()["openevidence_enabled"] is False
