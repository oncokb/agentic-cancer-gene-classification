"""Tests for latency optimization plumbing."""

import asyncio
import time

from fastapi.testclient import TestClient

from src import main
from src.main import app
from src.models.schema import AnnotationResult
from src.models.schema import GeneAnnotation, ResolvedGene
from src.models.schema import AnnotateRequest, LiteratureRecord
from src.pipeline import orchestrator
from src.pipeline.selection import select_papers_for_synthesis
from src.pipeline import synthesis


async def test_run_pipeline_parallelizes_genes_and_reports_timings(monkeypatch):
    active = 0
    max_active = 0
    completed = []

    async def fake_normalize_fusions(inputs):
        return {
            "AAA": (
                ResolvedGene(input_symbol="AAA", canonical_symbol="AAA", resolved=True),
                ["AAA::BBB"],
            ),
            "BBB": (
                ResolvedGene(input_symbol="BBB", canonical_symbol="BBB", resolved=True),
                ["AAA::BBB"],
            ),
        }

    async def fake_annotate_gene(*, gene, **kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return GeneAnnotation(
            gene=gene,
            cancer_associated=True,
            evidence_support_score=0.9,
            timings_ms={"total": 10.0},
        )

    async def on_annotation(annotation):
        completed.append(annotation.gene)

    monkeypatch.setattr(orchestrator.settings, "annotation_gene_concurrency", 2)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await orchestrator.run_pipeline(["AAA::BBB"], on_annotation=on_annotation)

    assert max_active == 2
    assert sorted(completed) == ["AAA", "BBB"]
    assert result.genes_annotated == 2
    assert result.timings_ms["normalization"] >= 0
    assert result.timings_ms["annotation"] >= 0
    assert result.timings_ms["total"] >= 0


def test_annotate_request_accepts_core_mode():
    request = AnnotateRequest(fusions=["TP53::BRAF"], mode="core")

    assert request.mode == "core"


async def test_selection_skips_llm_above_threshold(monkeypatch):
    async def fail_complete_with_tool(**kwargs):
        raise AssertionError("selection LLM should not be called")

    monkeypatch.setattr("src.pipeline.selection.complete_with_tool", fail_complete_with_tool)
    monkeypatch.setattr("src.pipeline.selection.settings.selection_llm_threshold", 3)
    records = [
        LiteratureRecord(pmid=str(i), title=f"Paper {i}", abstract=f"Abstract {i}")
        for i in range(5)
    ]

    selected = await select_papers_for_synthesis("GENE", records, max_papers=2)

    assert [record.pmid for record in selected] == ["0", "1"]


def test_annotation_job_endpoint_streams_partial_results(monkeypatch):
    async def fake_run_pipeline(
        fusions,
        local_backend=None,
        run_store=None,
        force_refresh=False,
        mode="full",
        on_annotation=None,
    ):
        annotation = GeneAnnotation(
            gene="TP53",
            fusions=["TP53::BRAF"],
            cancer_associated=True,
            cancer_association_rationale="Known tumor suppressor.",
            gene_summary="TP53 is recurrently altered in cancer.",
            evidence_support_score=0.95,
            timings_ms={"total": 1.0},
        )
        if on_annotation:
            await on_annotation(annotation)
        return AnnotationResult(
            run_id="run-1",
            timestamp="2026-08-01T00:00:00+00:00",
            fusions_processed=1,
            genes_annotated=1,
            annotations=[annotation],
            timings_ms={"total": 2.0},
        )

    async def fake_persist_run_result(*args, **kwargs):
        return None

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    monkeypatch.setattr(main, "_persist_run_result", fake_persist_run_result)
    app.state.run_store = None
    client = TestClient(app)

    create_response = client.post(
        "/v1/annotate/jobs",
        json={"fusions": ["TP53::BRAF"], "mode": "core"},
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
    assert status_payload["genes_completed"] == 1
    assert status_payload["annotations"][0]["gene"] == "TP53"
    assert status_payload["result"]["timings_ms"]["total"] == 2.0


async def test_evict_stale_annotation_jobs_removes_only_finished_jobs_past_ttl(monkeypatch):
    from src.main import AnnotationJobStatusResponse

    main._annotation_jobs.clear()
    monkeypatch.setattr(main.settings, "annotation_job_ttl_seconds", 100)

    now = time.monotonic()
    stale_complete = AnnotationJobStatusResponse(
        job_id="stale-complete", status="complete", fusions_processed=1, created_at=now - 200
    )
    stale_failed = AnnotationJobStatusResponse(
        job_id="stale-failed", status="failed", fusions_processed=1, created_at=now - 200
    )
    fresh_complete = AnnotationJobStatusResponse(
        job_id="fresh-complete", status="complete", fusions_processed=1, created_at=now
    )
    stale_but_running = AnnotationJobStatusResponse(
        job_id="stale-running", status="running", fusions_processed=1, created_at=now - 200
    )
    for job in (stale_complete, stale_failed, fresh_complete, stale_but_running):
        await main._store_annotation_job(job)

    await main._evict_stale_annotation_jobs()

    assert set(main._annotation_jobs.keys()) == {"fresh-complete", "stale-running"}
    main._annotation_jobs.clear()


async def test_track_background_task_keeps_strong_reference_until_done():
    main._background_tasks.clear()
    started = asyncio.Event()
    finish = asyncio.Event()

    async def coro():
        started.set()
        await finish.wait()

    task = main._track_background_task(coro())
    await started.wait()

    assert task in main._background_tasks

    finish.set()
    await task

    assert task not in main._background_tasks


async def test_synthesis_fast_model_is_used_without_escalation(monkeypatch):
    calls = []

    async def fake_complete_with_tool(**kwargs):
        calls.append((kwargs["model"], kwargs["model_purpose"]))
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Supported by a retrieved PMID.",
            "gene_summary": "GENE is associated with cancer (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", True)
    monkeypatch.setattr(synthesis.settings, "synthesis_fast_model", "fast-model")
    monkeypatch.setattr(synthesis.settings, "synthesis_model", "deep-model")
    records = [LiteratureRecord(pmid="1", title="Paper 1", abstract="GENE cancer")]

    result = await synthesis.synthesize_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=records,
        retrieval_tier=1,
    )

    assert result["citations"] == ["1"]
    assert calls == [("fast-model", "synthesis_fast")]


async def test_synthesis_escalates_weak_fast_result(monkeypatch):
    calls = []

    async def fake_complete_with_tool(**kwargs):
        calls.append((kwargs["model"], kwargs["model_purpose"]))
        if len(calls) == 1:
            return {
                "cancer_associated": True,
                "insufficient_evidence": False,
                "gene_summary": "Too thin.",
                "citations": [],
            }
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Supported by retrieved PMIDs.",
            "gene_summary": "GENE is associated with cancer (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", True)
    monkeypatch.setattr(synthesis.settings, "synthesis_fast_model", "fast-model")
    monkeypatch.setattr(synthesis.settings, "synthesis_model", "deep-model")
    records = [
        LiteratureRecord(pmid=str(index), title=f"Paper {index}", abstract="GENE cancer")
        for index in range(1, 5)
    ]

    result = await synthesis.synthesize_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=records,
        retrieval_tier=1,
    )

    assert result["cancer_association_rationale"] == "Supported by retrieved PMIDs."
    assert calls == [("fast-model", "synthesis_fast"), ("deep-model", "synthesis")]


async def test_core_synthesis_accepts_complete_low_support_fast_result(monkeypatch):
    calls = []

    async def fake_complete_with_tool(**kwargs):
        calls.append((kwargs["model"], kwargs["model_purpose"]))
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Supported by limited retrieved evidence.",
            "gene_summary": "GENE has limited cancer evidence (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", True)
    monkeypatch.setattr(synthesis.settings, "synthesis_fast_model", "fast-model")
    monkeypatch.setattr(synthesis.settings, "synthesis_model", "deep-model")
    monkeypatch.setattr(synthesis.settings, "core_synthesis_escalation_min_support_score", 0.0)
    records = [
        LiteratureRecord(pmid=str(index), title=f"Paper {index}", abstract="GENE cancer")
        for index in range(1, 5)
    ]

    result = await synthesis.synthesize_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=records,
        retrieval_tier=1,
        mode="core",
    )

    assert result["citations"] == ["1"]
    assert calls == [("fast-model", "synthesis_fast")]


async def test_core_synthesis_uses_tight_prompt_and_token_budget(monkeypatch):
    seen = {}

    async def fake_complete_with_tool(**kwargs):
        seen.update(kwargs)
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Supported by retrieved evidence.",
            "gene_summary": "GENE has cancer evidence (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", False)
    monkeypatch.setattr(synthesis.settings, "core_synthesis_max_tokens", 512)
    monkeypatch.setattr(synthesis.settings, "core_synthesis_abstract_chars", 24)
    monkeypatch.setattr(synthesis.settings, "core_synthesis_max_papers", 2)
    records = [
        LiteratureRecord(
            pmid=str(index),
            title=f"Paper {index}",
            abstract=f"GENE cancer abstract {index} with extra details",
        )
        for index in range(1, 5)
    ]

    await synthesis.synthesize_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=records,
        retrieval_tier=1,
        mode="core",
    )

    assert seen["max_tokens"] == 512
    assert "supporting quotes" in seen["system"].lower()
    assert "PMID: 1" in seen["user"]
    assert "PMID: 2" in seen["user"]
    assert "PMID: 3" not in seen["user"]
    assert "GENE cancer abstract 1 w" in seen["user"]
    assert "ith extra details" not in seen["user"]
