"""
FastAPI application — manually invokable, Docker/K8s-ready.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from src.models.schema import AnnotateRequest, AnnotationResult
from src.pipeline.orchestrator import run_pipeline
from src.pipeline.run_store import RunStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.run_store = await RunStore.create()
    yield
    await app.state.run_store.close()


app = FastAPI(
    title="Agentic Cancer Gene Classification",
    description=(
        "M0: LLM annotation engine for candidate cancer gene fusions. "
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


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/v1/annotate", response_model=AnnotationResult)
async def annotate(request: AnnotateRequest, http_request: Request) -> AnnotationResult:
    """
    Annotate a list of candidate gene fusions.

    Each fusion is split into its partner genes. The unit of annotation
    is the gene. Returns one annotation row per unique gene, matching
    the MSK TARGET Gene Triaging schema.

    Input supports plain strings or structured objects with optional tumor_type and breakpoint fields:
    `{ "fusions": [{"fusion": "GENE1::GENE2", "tumor_type": "LUAD"}] }`
    """
    try:
        result = await run_pipeline(
            request.fusions,
            local_backend=request.local_backend,
            run_store=http_request.app.state.run_store,
            force_refresh=request.force_refresh,
        )
    except Exception as e:
        logger.exception("Pipeline error")
        raise HTTPException(status_code=500, detail=str(e)) from e

    try:
        await http_request.app.state.run_store.save_run(
            result.run_id, result.timestamp, request.model_dump(), result.model_dump()
        )
    except Exception:
        # A run's own result always returns even if it can't be persisted for
        # later sharing — the run store isn't on the critical path for the caller.
        logger.exception("Failed to persist run %s", result.run_id)

    return result


@app.get("/v1/annotate/{run_id}", response_model=AnnotationResult)
async def get_annotation_run(run_id: str, http_request: Request) -> AnnotationResult:
    """Fetch a previously-computed annotation run by ID, without recomputing it."""
    stored = await http_request.app.state.run_store.get_run(run_id)
    if stored is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return AnnotationResult(**stored)


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    logger.exception("Unhandled exception")
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


if __name__ == "__main__":
    uvicorn.run("src.main:app", host="0.0.0.0", port=8000, reload=False)
