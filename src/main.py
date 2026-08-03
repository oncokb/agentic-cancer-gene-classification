"""
FastAPI application — manually invokable, Docker/K8s-ready.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Literal, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from benchmarks.run_benchmark import DEFAULT_HOLDOUT, run_benchmark
from src.config import settings
from src.logging_utils import install_secret_redaction_filter
from src.models.schema import (
    AnnotateRequest,
    AnnotationMode,
    AnnotationResult,
    FusionInput,
    GeneAnnotateRequest,
    GeneAnnotation,
    LocalBackend,
)
from src.pipeline.enrichment import enrich_gene_annotations
from src.pipeline.orchestrator import run_pipeline
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
    if not settings.agcg_dev_mode:
        raise HTTPException(status_code=404, detail="Not found")


_annotation_jobs: Dict[str, AnnotationJobStatusResponse] = {}
_annotation_jobs_lock = asyncio.Lock()
_enrichment_jobs: Dict[str, EnrichmentJobStatusResponse] = {}
_enrichment_jobs_lock = asyncio.Lock()


async def _store_annotation_job(job: AnnotationJobStatusResponse) -> None:
    async with _annotation_jobs_lock:
        _annotation_jobs[job.job_id] = job


async def _get_annotation_job(job_id: str) -> AnnotationJobStatusResponse:
    async with _annotation_jobs_lock:
        job = _annotation_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Annotation job not found")
    return job


async def _store_enrichment_job(job: EnrichmentJobStatusResponse) -> None:
    async with _enrichment_jobs_lock:
        _enrichment_jobs[job.job_id] = job


async def _get_enrichment_job(job_id: str) -> EnrichmentJobStatusResponse:
    async with _enrichment_jobs_lock:
        job = _enrichment_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Enrichment job not found")
    return job


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


@app.get("/")
async def root() -> RedirectResponse:
    return RedirectResponse(url="/static/index.html")


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/v1/dev/status", response_model=DevStatusResponse)
async def dev_status() -> DevStatusResponse:
    return DevStatusResponse(enabled=settings.agcg_dev_mode)


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
    try:
        result = await run_pipeline(
            request.fusions,
            local_backend=request.local_backend,
            run_store=http_request.app.state.run_store,
            force_refresh=request.force_refresh,
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
                mode=request.mode,
                on_annotation=on_annotation,
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

    asyncio.create_task(run_job())
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

    asyncio.create_task(run_job())
    return EnrichmentJobCreateResponse(
        job_id=job_id,
        status_url=f"/v1/annotate/enrichment/jobs/{job_id}",
    )


@app.get("/v1/annotate/enrichment/jobs/{job_id}", response_model=EnrichmentJobStatusResponse)
async def get_enrichment_job(job_id: str) -> EnrichmentJobStatusResponse:
    return await _get_enrichment_job(job_id)


@app.post("/v1/annotate/gene", response_model=GeneAnnotation)
async def annotate_gene(request: GeneAnnotateRequest, http_request: Request) -> GeneAnnotation:
    """
    Annotate a single gene and return the result-card payload as JSON.

    This is a convenience endpoint for external REST clients. For batch runs or
    mixed gene/fusion inputs, use POST /v1/annotate.
    """
    gene_input = FusionInput(gene=request.gene, tumor_type=request.tumor_type)
    try:
        result = await run_pipeline(
            [gene_input],
            local_backend=request.local_backend,
            run_store=http_request.app.state.run_store,
            force_refresh=request.force_refresh,
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
    return AnnotationResult(**stored)


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
