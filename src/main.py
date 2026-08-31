"""
FastAPI application — manually invokable, Docker/K8s-ready.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine, Dict, List, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from benchmarks.run_benchmark import DEFAULT_HOLDOUT, run_benchmark
from src.config import settings
from src.logging_utils import install_secret_redaction_filter
from src.models.schema import (
    AnnotateRequest,
    AnnotationMode,
    AnnotationResult,
    FeedbackRequest,
    FeedbackResponse,
    FusionEvidenceResult,
    FusionInput,
    FusionPartnerEvidenceRequest,
    FusionPartnerEvidenceResult,
    FusionPositionContext,
    GeneAnnotateRequest,
    GeneAnnotation,
    LocalBackend,
)
from src.observability import record_user_seen, tag_current_span
from src.pipeline.cache import cached_call
from src.pipeline.enrichment import enrich_gene_annotations
from src.pipeline.fusion_context import annotate_fusion_position_contexts, parsed_input_from_fields
from src.pipeline.literature import retrieve_fusion_evidence, retrieve_fusion_partner_evidence
from src.pipeline.llm_client import complete_with_tool
from src.pipeline.normalization import is_fusion_input
from src.pipeline.orchestrator import run_pipeline
from src.pipeline.result_sanitizer import sanitize_annotation_result
from src.pipeline.run_store import RunStore

_log_record_factory = logging.getLogRecordFactory()


def _datadog_log_record_factory(*args, **kwargs):
    record = _log_record_factory(*args, **kwargs)
    defaults = {
        "dd.service": os.getenv("DD_SERVICE", "agentic-cancer-gene-classification"),
        "dd.env": os.getenv("DD_ENV", ""),
        "dd.version": os.getenv("DD_VERSION", ""),
        "dd.trace_id": "0",
        "dd.span_id": "0",
    }
    for key, value in defaults.items():
        if key not in record.__dict__:
            setattr(record, key, value)
    return record


logging.setLogRecordFactory(_datadog_log_record_factory)
logging.basicConfig(
    level=logging.INFO,
    format=(
        "%(asctime)s %(levelname)s %(name)s "
        "[dd.service=%(dd.service)s dd.env=%(dd.env)s dd.version=%(dd.version)s "
        "dd.trace_id=%(dd.trace_id)s dd.span_id=%(dd.span_id)s] — %(message)s"
    ),
    stream=sys.stdout,
)
install_secret_redaction_filter()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.run_store = await RunStore.create()
    yield
    await app.state.run_store.close()


app = FastAPI(
    title="Agentic Cancer Gene Classification",
    description=(
        "M0: LLM annotation engine for candidate cancer genes and gene fusions. "
        "Automates Nicole's MSK TARGET Gene Triaging workflow."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)

_STATIC_DIR = Path(__file__).parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.middleware("http")
async def no_cache_static(request: Request, call_next):
    # Without this, browsers apply heuristic caching to /static/* and to
    # / itself (neither StaticFiles nor FileResponse set an explicit
    # Cache-Control) and can silently keep serving a stale index.html/
    # app.js/styles.css after a deploy on a plain reload, not just a hard
    # refresh — force revalidation on every request for both.
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


class DevStatusResponse(BaseModel):
    enabled: bool


class AnnotationJobCreateResponse(BaseModel):
    job_id: str
    status_url: str


class AnnotationJobStatusResponse(BaseModel):
    job_id: str
    status: str
    fusions_processed: int
    genes_completed: int = 0
    genes_total: Optional[int] = None
    annotations: List[GeneAnnotation] = Field(default_factory=list)
    result: Optional[AnnotationResult] = None
    error: Optional[str] = None
    timings_ms: Dict[str, float] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.monotonic, exclude=True)


class FusionContextResponse(BaseModel):
    available: bool
    context: Optional[FusionPositionContext] = None


class _TransientFusionContextError(Exception):
    """Raised from the fusion-context cache compute step so cached_call never
    caches a transient upstream failure (unlike a real "no domain data" result,
    which is fine to cache)."""

    def __init__(self, context: FusionPositionContext) -> None:
        self.context = context


class EnrichmentJobCreateResponse(BaseModel):
    job_id: str
    status_url: str


class EnrichmentRequest(BaseModel):
    annotations: List[GeneAnnotation] = Field(
        ...,
        min_length=1,
        description="Core annotations to enrich lazily.",
    )
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description="Optional local agent backend for enrichment LLM calls.",
    )


class EnrichmentJobStatusResponse(BaseModel):
    job_id: str
    status: str
    annotations_completed: int = 0
    annotations_total: int = 0
    annotations: List[GeneAnnotation] = Field(default_factory=list)
    error: Optional[str] = None
    timings_ms: Dict[str, float] = Field(default_factory=dict)


class FusionEvidenceJobRequest(AnnotateRequest):
    run_id: Optional[str] = Field(
        default=None,
        description=(
            "Run ID to persist completed fusion evidence back onto, so a shared link "
            "for this run includes it instead of recomputing on every open."
        ),
    )


class FusionEvidenceJobCreateResponse(BaseModel):
    job_id: str
    status_url: str


class FusionEvidenceJobStatusResponse(BaseModel):
    job_id: str
    status: str
    fusions_completed: int = 0
    fusions_total: int = 0
    fusion_evidence: List[FusionEvidenceResult] = Field(default_factory=list)
    error: Optional[str] = None
    timings_ms: Dict[str, float] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.monotonic, exclude=True)


class BenchmarkRequest(BaseModel):
    no_judge: bool = Field(
        default=True,
        description="Skip the LLM-as-a-judge summary scoring step.",
    )
    max_genes: Optional[int] = Field(
        default=None,
        ge=1,
        description="Optional number of holdout genes to run for a quick smoke benchmark.",
    )
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description="Optional local agent backend for benchmark pipeline calls.",
    )
    mode: AnnotationMode = Field(
        default="full",
        description="Annotation mode passed to benchmark runs.",
    )
    route: Literal["direct", "local"] = Field(
        default="direct",
        description="Benchmark route: 'direct' pipeline call or local FastAPI /v1/annotate route.",
    )


def require_dev_mode() -> None:
    if not settings.acgc_dev_mode:
        raise HTTPException(status_code=404, detail="Not found")


_annotation_jobs: Dict[str, AnnotationJobStatusResponse] = {}
_annotation_jobs_lock = asyncio.Lock()
_enrichment_jobs: Dict[str, EnrichmentJobStatusResponse] = {}
_enrichment_jobs_lock = asyncio.Lock()
_fusion_evidence_jobs: Dict[str, FusionEvidenceJobStatusResponse] = {}
_fusion_evidence_jobs_lock = asyncio.Lock()

# asyncio.create_task() only keeps a weak reference to the task via the event
# loop — an unreferenced task can be garbage-collected mid-execution. This set
# holds a strong reference for the life of each background job, and the
# done-callback removes it once the task finishes (success or failure) so the
# set doesn't grow unbounded either.
_background_tasks: set[asyncio.Task] = set()


def _track_background_task(coro: Coroutine[Any, Any, None]) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def _store_annotation_job(job: AnnotationJobStatusResponse) -> None:
    async with _annotation_jobs_lock:
        _annotation_jobs[job.job_id] = job


async def _get_annotation_job(job_id: str) -> AnnotationJobStatusResponse:
    async with _annotation_jobs_lock:
        job = _annotation_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Annotation job not found")
    return job


async def _evict_stale_annotation_jobs() -> None:
    """Drop finished jobs older than the TTL so the in-memory job store
    doesn't grow unbounded over the life of the process. Jobs still queued
    or running are never evicted, regardless of age."""
    cutoff = time.monotonic() - settings.annotation_job_ttl_seconds
    async with _annotation_jobs_lock:
        stale = [
            job_id
            for job_id, job in _annotation_jobs.items()
            if job.status in ("complete", "failed") and job.created_at < cutoff
        ]
        for job_id in stale:
            del _annotation_jobs[job_id]


async def _store_enrichment_job(job: EnrichmentJobStatusResponse) -> None:
    async with _enrichment_jobs_lock:
        _enrichment_jobs[job.job_id] = job


async def _get_enrichment_job(job_id: str) -> EnrichmentJobStatusResponse:
    async with _enrichment_jobs_lock:
        job = _enrichment_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Enrichment job not found")
    return job


async def _store_fusion_evidence_job(job: FusionEvidenceJobStatusResponse) -> None:
    async with _fusion_evidence_jobs_lock:
        _fusion_evidence_jobs[job.job_id] = job


async def _get_fusion_evidence_job(job_id: str) -> FusionEvidenceJobStatusResponse:
    async with _fusion_evidence_jobs_lock:
        job = _fusion_evidence_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Fusion evidence job not found")
    return job


def _fusion_evidence_inputs(fusions: List[FusionInput]) -> List[FusionInput]:
    seen: set[tuple[str, Optional[str]]] = set()
    inputs: List[FusionInput] = []
    for item in fusions:
        if not is_fusion_input(item.fusion):
            continue
        key = (item.fusion, " ".join((item.tumor_type or "").strip().lower().split()) or None)
        if key in seen:
            continue
        seen.add(key)
        inputs.append(item)
    return inputs


async def _persist_run_result(
    http_request: Request,
    request_payload: dict,
    result: AnnotationResult,
) -> None:
    try:
        await http_request.app.state.run_store.save_run(
            result.run_id, result.timestamp, request_payload, result.model_dump()
        )
    except Exception:
        # A run's own result always returns even if it can't be persisted for
        # later sharing — the run store isn't on the critical path for the caller.
        logger.exception("Failed to persist run %s", result.run_id)


async def _persist_fusion_evidence(
    http_request: Request,
    run_id: str,
    fusion_evidence: List[FusionEvidenceResult],
) -> None:
    """Merge a completed fusion-evidence job back onto its run, so opening a
    shared link later shows it instead of a hidden/empty fusion evidence tab."""
    try:
        stored = await http_request.app.state.run_store.get_run(run_id)
        if stored is None:
            return
        stored["fusion_evidence"] = [item.model_dump() for item in fusion_evidence]
        await http_request.app.state.run_store.update_run_result(run_id, stored)
    except Exception:
        logger.exception("Failed to persist fusion evidence for run %s", run_id)


def _request_user_id(request: Request) -> Optional[str]:
    value = request.headers.get(settings.datadog_user_id_header)
    return value.strip() if value and value.strip() else None


def _record_annotation_request_metrics(
    request: AnnotateRequest | GeneAnnotateRequest,
    http_request: Request,
) -> None:
    user_id = _request_user_id(http_request)
    tags = [
        f"mode:{request.mode}",
        f"local_backend:{request.local_backend or 'sdk'}",
        f"skip_literature_for_oncokb:{request.skip_literature_for_oncokb}",
    ]
    record_user_seen(user_id, tags=tags)
    tag_current_span(
        {
            "acgc.user.present": bool(user_id),
            "acgc.mode": request.mode,
            "acgc.local_backend": request.local_backend or "sdk",
            "acgc.skip_literature_for_oncokb": request.skip_literature_for_oncokb,
        }
    )


@app.get("/")
async def root() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/dev/status", response_model=DevStatusResponse)
async def dev_status() -> DevStatusResponse:
    return DevStatusResponse(enabled=settings.acgc_dev_mode)


@app.post("/v1/annotate", response_model=AnnotationResult)
async def annotate(request: AnnotateRequest, http_request: Request) -> AnnotationResult:
    """
    Annotate a list of candidate genes or gene fusions.

    Each fusion is split into its partner genes; singleton genes are used directly. The unit of annotation
    is the gene. Returns one annotation row per unique gene, matching
    the MSK TARGET Gene Triaging schema.

    Input supports plain strings or structured objects with optional tumor_type and breakpoint fields:
    `{ "fusions": ["ALK", {"fusion": "EML4::ALK", "tumor_type": "LUAD"}] }`
    """
    _record_annotation_request_metrics(request, http_request)
    try:
        result = await run_pipeline(
            request.fusions,
            local_backend=request.local_backend,
            run_store=http_request.app.state.run_store,
            force_refresh=request.force_refresh,
            skip_literature_for_oncokb=request.skip_literature_for_oncokb,
            mode=request.mode,
        )
    except Exception as e:
        logger.exception("Pipeline error")
        raise HTTPException(status_code=500, detail=str(e)) from e

    await _persist_run_result(http_request, request.model_dump(), result)

    return result


@app.post("/v1/annotate/jobs", response_model=AnnotationJobCreateResponse)
async def create_annotation_job(
    request: AnnotateRequest,
    http_request: Request,
) -> AnnotationJobCreateResponse:
    _record_annotation_request_metrics(request, http_request)
    await _evict_stale_annotation_jobs()

    job_id = str(uuid.uuid4())
    job = AnnotationJobStatusResponse(
        job_id=job_id,
        status="queued",
        fusions_processed=len(request.fusions),
    )
    await _store_annotation_job(job)

    async def on_annotation(annotation: GeneAnnotation) -> None:
        current = await _get_annotation_job(job_id)
        current.annotations.append(annotation)
        current.annotations.sort(key=lambda item: item.gene)
        current.genes_completed = len(current.annotations)
        await _store_annotation_job(current)

    async def on_total_known(total: int) -> None:
        current = await _get_annotation_job(job_id)
        current.genes_total = total
        await _store_annotation_job(current)

    async def run_job() -> None:
        current = await _get_annotation_job(job_id)
        current.status = "running"
        await _store_annotation_job(current)
        try:
            result = await run_pipeline(
                request.fusions,
                local_backend=request.local_backend,
                run_store=http_request.app.state.run_store,
                force_refresh=request.force_refresh,
                skip_literature_for_oncokb=request.skip_literature_for_oncokb,
                mode=request.mode,
                on_annotation=on_annotation,
                on_total_known=on_total_known,
            )
            await _persist_run_result(http_request, request.model_dump(), result)
            current = await _get_annotation_job(job_id)
            current.status = "complete"
            current.result = result
            current.annotations = result.annotations
            current.genes_completed = result.genes_annotated
            current.genes_total = result.genes_annotated
            current.timings_ms = result.timings_ms
            await _store_annotation_job(current)
        except Exception as exc:
            logger.exception("Annotation job %s failed", job_id)
            current = await _get_annotation_job(job_id)
            current.status = "failed"
            current.error = str(exc)
            await _store_annotation_job(current)

    _track_background_task(run_job())
    return AnnotationJobCreateResponse(
        job_id=job_id,
        status_url=f"/v1/annotate/jobs/{job_id}",
    )


@app.get("/v1/annotate/jobs/{job_id}", response_model=AnnotationJobStatusResponse)
async def get_annotation_job(job_id: str) -> AnnotationJobStatusResponse:
    return await _get_annotation_job(job_id)


@app.post("/v1/annotate/enrichment/jobs", response_model=EnrichmentJobCreateResponse)
async def create_enrichment_job(
    request: EnrichmentRequest,
) -> EnrichmentJobCreateResponse:
    """Lazily enrich already-returned core annotations in the background."""
    job_id = str(uuid.uuid4())
    job = EnrichmentJobStatusResponse(
        job_id=job_id,
        status="queued",
        annotations_total=len(request.annotations),
    )
    await _store_enrichment_job(job)

    async def on_annotation(annotation: GeneAnnotation) -> None:
        current = await _get_enrichment_job(job_id)
        current.annotations.append(annotation)
        current.annotations.sort(key=lambda item: item.gene)
        current.annotations_completed = len(current.annotations)
        await _store_enrichment_job(current)

    async def run_job() -> None:
        current = await _get_enrichment_job(job_id)
        current.status = "running"
        await _store_enrichment_job(current)
        try:
            enriched = await enrich_gene_annotations(
                request.annotations,
                local_backend=request.local_backend,
                on_annotation=on_annotation,
            )
            current = await _get_enrichment_job(job_id)
            current.status = "complete"
            current.annotations = enriched
            current.annotations_completed = len(enriched)
            current.annotations_total = len(request.annotations)
            current.timings_ms = {
                "total": round(
                    sum(annotation.timings_ms.get("total", 0.0) for annotation in enriched),
                    2,
                )
            }
            await _store_enrichment_job(current)
        except Exception as exc:
            logger.exception("Enrichment job %s failed", job_id)
            current = await _get_enrichment_job(job_id)
            current.status = "failed"
            current.error = str(exc)
            await _store_enrichment_job(current)

    _track_background_task(run_job())
    return EnrichmentJobCreateResponse(
        job_id=job_id,
        status_url=f"/v1/annotate/enrichment/jobs/{job_id}",
    )


@app.get("/v1/annotate/enrichment/jobs/{job_id}", response_model=EnrichmentJobStatusResponse)
async def get_enrichment_job(job_id: str) -> EnrichmentJobStatusResponse:
    return await _get_enrichment_job(job_id)


@app.post("/v1/fusion-evidence/jobs", response_model=FusionEvidenceJobCreateResponse)
async def create_fusion_evidence_job(
    request: FusionEvidenceJobRequest,
    http_request: Request,
) -> FusionEvidenceJobCreateResponse:
    """Run exact fusion-pair PubMed evidence retrieval outside the annotation critical path."""
    fusion_inputs = _fusion_evidence_inputs(request.fusions)
    job_id = str(uuid.uuid4())
    job = FusionEvidenceJobStatusResponse(
        job_id=job_id,
        status="queued",
        fusions_total=len(fusion_inputs),
    )
    await _store_fusion_evidence_job(job)

    async def run_one(item: FusionInput, semaphore: asyncio.Semaphore) -> FusionEvidenceResult:
        async with semaphore:
            try:
                return await retrieve_fusion_evidence(item.fusion, tumor_type=item.tumor_type)
            except Exception as exc:
                logger.exception("Fusion evidence retrieval failed for %s", item.fusion)
                return FusionEvidenceResult(
                    fusion=item.fusion,
                    tumor_type=item.tumor_type,
                    interpretation=f"Fusion evidence retrieval failed: {exc}",
                )

    async def run_job() -> None:
        start = time.perf_counter()
        current = await _get_fusion_evidence_job(job_id)
        current.status = "running"
        await _store_fusion_evidence_job(current)
        try:
            semaphore = asyncio.Semaphore(max(1, settings.fusion_evidence_concurrency))
            tasks = [asyncio.create_task(run_one(item, semaphore)) for item in fusion_inputs]
            for task in asyncio.as_completed(tasks):
                result = await task
                current = await _get_fusion_evidence_job(job_id)
                current.fusion_evidence.append(result)
                current.fusion_evidence.sort(key=lambda item: item.fusion)
                current.fusions_completed = len(current.fusion_evidence)
                await _store_fusion_evidence_job(current)

            current = await _get_fusion_evidence_job(job_id)
            current.status = "complete"
            current.fusions_completed = len(current.fusion_evidence)
            current.timings_ms = {"total": round((time.perf_counter() - start) * 1000, 2)}
            await _store_fusion_evidence_job(current)
            if request.run_id:
                await _persist_fusion_evidence(http_request, request.run_id, current.fusion_evidence)
        except Exception as exc:
            logger.exception("Fusion evidence job %s failed", job_id)
            current = await _get_fusion_evidence_job(job_id)
            current.status = "failed"
            current.error = str(exc)
            await _store_fusion_evidence_job(current)

    _track_background_task(run_job())
    return FusionEvidenceJobCreateResponse(
        job_id=job_id,
        status_url=f"/v1/fusion-evidence/jobs/{job_id}",
    )


@app.get("/v1/fusion-evidence/jobs/{job_id}", response_model=FusionEvidenceJobStatusResponse)
async def get_fusion_evidence_job(job_id: str) -> FusionEvidenceJobStatusResponse:
    return await _get_fusion_evidence_job(job_id)


@app.post("/v1/annotate/gene", response_model=GeneAnnotation)
async def annotate_gene(request: GeneAnnotateRequest, http_request: Request) -> GeneAnnotation:
    """
    Annotate a single gene and return the result-card payload as JSON.

    This is a convenience endpoint for external REST clients. For batch runs or
    mixed gene/fusion inputs, use POST /v1/annotate.
    """
    _record_annotation_request_metrics(request, http_request)
    gene_input = FusionInput(gene=request.gene, tumor_type=request.tumor_type)
    try:
        result = await run_pipeline(
            [gene_input],
            local_backend=request.local_backend,
            run_store=http_request.app.state.run_store,
            force_refresh=request.force_refresh,
            skip_literature_for_oncokb=request.skip_literature_for_oncokb,
            mode=request.mode,
        )
    except Exception as e:
        logger.exception("Gene annotation pipeline error")
        raise HTTPException(status_code=500, detail=str(e)) from e

    await _persist_run_result(http_request, request.model_dump(), result)

    if not result.annotations:
        raise HTTPException(status_code=500, detail="No gene annotation was returned")
    return result.annotations[0]


@app.get("/v1/annotate/{run_id}", response_model=AnnotationResult)
async def get_annotation_run(run_id: str, http_request: Request) -> AnnotationResult:
    """Fetch a previously-computed annotation run by ID, without recomputing it."""
    stored = await http_request.app.state.run_store.get_run(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Run not found")
    result = AnnotationResult(**stored)
    try:
        result, changed = await sanitize_annotation_result(result)
        if changed:
            await http_request.app.state.run_store.update_run_result(run_id, result.model_dump())
    except Exception:
        logger.exception("Failed to sanitize stored run %s", run_id)
    return result


@app.post("/v1/fusion-context", response_model=FusionContextResponse)
async def fusion_context(request: FusionInput) -> FusionContextResponse:
    """
    On-demand protein-domain-retention and treatment-knowledge lookup for a single
    fusion, via the sibling fusion-annotation service. Deliberately NOT part of
    POST /v1/annotate — this only runs when a curator explicitly expands the
    "Domains & treatments" section for a fusion, so it never adds latency to the
    core annotation result.

    Structured data only, no LLM involvement. Returns {"available": false} (not
    an error) when the integration isn't configured, so the frontend can hide the
    section entirely rather than show a broken one.
    """
    if not settings.fusion_annotation_api_enabled or not settings.fusion_annotation_api_base_url.strip():
        return FusionContextResponse(available=False)

    try:
        parsed = parsed_input_from_fields(
            request.fusion,
            five_exon=request.five_exon,
            three_exon=request.three_exon,
            five_genomic=request.five_genomic,
            three_genomic=request.three_genomic,
            five_transcript=request.five_transcript,
            three_transcript=request.three_transcript,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    async def compute() -> dict:
        contexts = await annotate_fusion_position_contexts([parsed])
        context = contexts[0]
        if context.error is not None:
            raise _TransientFusionContextError(context)
        return context.model_dump()

    cache_key = "fusion_context:" + json.dumps(asdict(parsed), sort_keys=True, default=str)
    try:
        cached = await cached_call(cache_key, compute, ttl_seconds=settings.fusion_context_cache_ttl_seconds)
    except _TransientFusionContextError as exc:
        return FusionContextResponse(available=True, context=exc.context)

    return FusionContextResponse(available=True, context=FusionPositionContext(**cached))


@app.post("/v1/fusion-partner-evidence", response_model=FusionPartnerEvidenceResult)
async def fusion_partner_evidence(request: FusionPartnerEvidenceRequest) -> FusionPartnerEvidenceResult:
    """
    On-demand check for whether a fusion partner gene has precedent as an oncogenic
    fusion partner elsewhere — a different question from /v1/fusion-evidence/jobs,
    which checks the exact fusion pair. Deliberately NOT part of POST /v1/annotate or
    the background fusion-evidence job: this only runs when a curator explicitly
    expands the "Check fusion partner precedent" disclosure on a gene that came back
    insufficient_evidence, so it never adds cost to the core annotation run.
    """
    gene = request.gene.strip().upper()
    if not gene:
        raise HTTPException(status_code=400, detail="gene is required")
    tumor_type = request.tumor_type.strip() if request.tumor_type else None
    # No tumor type to scope by — agnostic is the only meaningful search.
    agnostic = request.agnostic or not tumor_type
    return await retrieve_fusion_partner_evidence(
        gene,
        tumor_type=tumor_type,
        agnostic=agnostic,
        exclude_pmids=set(request.exclude_pmids),
    )


FEEDBACK_ISSUE_TOOL = {
    "name": "draft_feedback_issue",
    "description": "Draft a concise GitHub issue from curator feedback.",
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "A concise GitHub issue title, 80 characters or fewer.",
            },
            "problem_summary": {
                "type": "string",
                "description": "A neutral summary of the reported bug, request, or annotation issue.",
            },
            "suggested_solution": {
                "type": "string",
                "description": "Concrete engineering guidance for how to address the feedback.",
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Short checklist items for resolving the issue.",
            },
        },
        "required": [
            "title",
            "problem_summary",
            "suggested_solution",
            "acceptance_criteria",
        ],
    },
}


def _fallback_feedback_issue(payload: FeedbackRequest) -> dict:
    first_line = payload.message.strip().splitlines()[0][:80] or "Curator feedback"
    return {
        "title": f"Feedback: {first_line}",
        "problem_summary": payload.message.strip(),
        "suggested_solution": "Review the original feedback and translate it into a scoped UI or pipeline change.",
        "acceptance_criteria": [
            "Original feedback is addressed or explicitly declined.",
            "Relevant UI/API behavior is covered by a focused test or smoke check.",
        ],
    }


def _feedback_issue_body(payload: FeedbackRequest, draft: dict, feedback_id: str) -> str:
    criteria = draft.get("acceptance_criteria") or []
    criteria_lines = "\n".join(f"- [ ] {item}" for item in criteria if str(item).strip())
    context_lines = [
        f"- Feedback ID: {feedback_id}",
        f"- Category: {payload.category}",
        f"- Run ID: {payload.run_id or ''}",
        f"- Gene: {payload.gene or ''}",
        f"- Page URL: {payload.page_url or ''}",
        f"- Contact email: {payload.contact_email or ''}",
    ]
    return "\n".join(
        [
            "## Parsed Feedback",
            str(draft.get("problem_summary") or "").strip(),
            "",
            "## Suggested Solution",
            str(draft.get("suggested_solution") or "").strip(),
            "",
            "## Acceptance Criteria",
            criteria_lines or "- [ ] Review and resolve this feedback.",
            "",
            "## Original Feedback",
            "```",
            payload.message.strip(),
            "```",
            "",
            "## Context",
            "\n".join(context_lines),
        ]
    )


async def _draft_feedback_issue(payload: FeedbackRequest, feedback_id: str) -> tuple[str, str]:
    system = (
        "You triage feedback for a cancer gene annotation web app. "
        "Turn the raw curator feedback into a small, actionable GitHub issue. "
        "Do not invent facts. Keep the title concise. Suggested solutions should be concrete "
        "engineering guidance, not vague product language."
    )
    user = (
        f"Raw feedback:\n{payload.message.strip()}\n\n"
        f"Category: {payload.category}\n"
        f"Run ID: {payload.run_id or ''}\n"
        f"Gene: {payload.gene or ''}\n"
        f"Page URL: {payload.page_url or ''}"
    )
    try:
        draft = await complete_with_tool(
            model=settings.feedback_model,
            system=system,
            user=user,
            tool=FEEDBACK_ISSUE_TOOL,
            max_tokens=1200,
            model_purpose="selection",
        )
    except Exception:
        logger.exception("Feedback issue LLM draft failed; using fallback draft")
        draft = _fallback_feedback_issue(payload)

    if not draft:
        draft = _fallback_feedback_issue(payload)

    title = str(draft.get("title") or "ACGC feedback").strip()[:120] or "ACGC feedback"
    return title, _feedback_issue_body(payload, draft, feedback_id)


@app.post("/v1/feedback", response_model=FeedbackResponse, status_code=201)
async def submit_feedback(payload: FeedbackRequest, http_request: Request) -> FeedbackResponse:
    """
    Beta feedback intake. Stores run_id/gene alongside the message so a
    reported issue can be traced back to the exact run that produced it,
    without needing the curator to describe what they did from memory.
    """
    feedback_id = str(uuid.uuid4())
    await http_request.app.state.run_store.save_feedback(
        feedback_id=feedback_id,
        created_at=datetime.now(timezone.utc),
        category=payload.category,
        message=payload.message,
        contact_email=payload.contact_email,
        run_id=payload.run_id,
        gene=payload.gene,
        page_url=payload.page_url,
        user_agent=http_request.headers.get("user-agent"),
    )
    issue_title, issue_body = await _draft_feedback_issue(payload, feedback_id)
    return FeedbackResponse(
        feedback_id=feedback_id,
        issue_title=issue_title,
        issue_body=issue_body,
    )


@app.post("/v1/dev/benchmark")
async def benchmark(request: BenchmarkRequest) -> dict:
    require_dev_mode()
    try:
        return await run_benchmark(
            holdout_path=DEFAULT_HOLDOUT,
            no_judge=request.no_judge,
            local_backend=request.local_backend,
            max_genes=request.max_genes,
            mode=request.mode,
            route=request.route,
        )
    except Exception as e:
        logger.exception("Benchmark error")
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)
