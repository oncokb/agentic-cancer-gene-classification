"""Process-local background draft jobs, following the annotation job lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth import require_auth
from src.config import settings
from src.pipeline.curation_reevaluate import InvalidPMIDs, ReevaluateRequest, reevaluate

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/curation/reevaluate", tags=["curation"])
# TODO(curator-overrides merge): replace require_auth with require_curator here.
_curator_dependency = require_auth


class JobCreated(BaseModel):
    job_id: str
    status_url: str


class JobStatus(BaseModel):
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    error: Optional[str] = None
    invalid_pmids: Optional[dict[str, list[str]]] = None
    draft: Optional[dict[str, Any]] = None
    finished_at: Optional[float] = Field(default=None, exclude=True)


_jobs: dict[str, JobStatus] = {}
_jobs_lock = asyncio.Lock()


async def _run_job(job_id: str, request: ReevaluateRequest, run_store):
    async with _jobs_lock:
        _jobs[job_id] = JobStatus(status="running")
    try:
        draft = await reevaluate(request, run_store=run_store)
        result = JobStatus(status="succeeded", draft=draft)
    except InvalidPMIDs as exc:
        result = JobStatus(status="failed", error=str(exc), invalid_pmids=exc.reasons)
    except asyncio.CancelledError:
        async with _jobs_lock:
            _jobs[job_id] = JobStatus(
                status="failed", error="Job cancelled", finished_at=time.monotonic()
            )
        raise
    except Exception:
        logger.exception("Curator re-evaluation job %s failed", job_id)
        result = JobStatus(status="failed", error="Re-evaluation failed; please retry.")
    result.finished_at = time.monotonic()
    async with _jobs_lock:
        _jobs[job_id] = result


@router.post("/jobs", response_model=JobCreated)
async def create_job(
    request: ReevaluateRequest, http_request: Request, current_user=Depends(_curator_dependency)
):
    from src import main

    job_id = str(uuid.uuid4())
    async with _jobs_lock:
        cutoff = time.monotonic() - settings.annotation_job_ttl_seconds
        for expired in [
            key
            for key, job in _jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]:
            del _jobs[expired]
        _jobs[job_id] = JobStatus()
    main._track_background_task(
        _run_job(job_id, request, getattr(http_request.app.state, "run_store", None))
    )
    return JobCreated(job_id=job_id, status_url=f"/v1/curation/reevaluate/jobs/{job_id}")


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, current_user=Depends(_curator_dependency)):
    async with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return job
