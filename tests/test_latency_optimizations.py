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


async def test_run_pipeline_reports_total_before_any_gene_completes(monkeypatch):
    totals_seen = []
    completed_at_total_time = []

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
        await asyncio.sleep(0.01)
        return GeneAnnotation(
            gene=gene,
            cancer_associated=True,
            evidence_support_score=0.9,
            timings_ms={"total": 10.0},
        )

    async def on_total_known(total):
        totals_seen.append(total)

    async def on_annotation(annotation):
        completed_at_total_time.append(len(totals_seen))

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    await orchestrator.run_pipeline(
        ["AAA::BBB"], on_annotation=on_annotation, on_total_known=on_total_known
    )

    assert totals_seen == [2]
    assert completed_at_total_time == [1, 1]


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
        on_total_known=None,
    ):
        if on_total_known:
            await on_total_known(1)
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
    assert status_payload["genes_total"] == 1
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
