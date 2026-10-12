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
from src.observability import record_user_action
from src.pipeline.curation_reevaluate import (
    InvalidPMIDs,
    NCBIUnavailable,
    SubjectNotResolved,
    ReevaluateRequest,
    reevaluate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/curation/reevaluate", tags=["curation"])
# TODO(curator-overrides merge): edit this line to use require_curator.
# Depends binds at route definition time; runtime reassignment does not update the routes.
_curator_dependency = require_auth


class JobCreated(BaseModel):
    job_id: str
    status_url: str


class JobStatus(BaseModel):
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    error: Optional[str] = None
    invalid_pmids: Optional[dict[str, list[str]]] = None
    draft: Optional[dict[str, Any]] = None
    owner_email: str = Field(default="", exclude=True)
    finished_at: Optional[float] = Field(default=None, exclude=True)


_jobs: dict[str, JobStatus] = {}
_jobs_lock = asyncio.Lock()


async def _run_job(job_id: str, request: ReevaluateRequest, run_store):
    async with _jobs_lock:
        owner = _jobs[job_id].owner_email
        _jobs[job_id].status = "running"
    record_user_action(
        user_id=owner, action="curation_reevaluate_start", details={"job_id": job_id}
    )
    try:
        draft = await reevaluate(request, run_store=run_store)
        result = JobStatus(status="succeeded", draft=draft)
    except InvalidPMIDs as exc:
        result = JobStatus(status="failed", error=str(exc), invalid_pmids=exc.reasons)
    except (SubjectNotResolved, NCBIUnavailable) as exc:
        result = JobStatus(status="failed", error=str(exc))
    except asyncio.CancelledError:
        async with _jobs_lock:
            _jobs[job_id] = JobStatus(
                status="failed",
                error="Job cancelled",
                owner_email=owner,
                finished_at=time.monotonic(),
            )
        record_user_action(
            user_id=owner,
            action="curation_reevaluate_failure",
            details={"job_id": job_id, "error": "Job cancelled"},
        )
        raise
    except Exception:
        logger.exception("Curator re-evaluation job %s failed", job_id)
        result = JobStatus(status="failed", error="Re-evaluation failed; please retry.")
    result.owner_email = owner
    result.finished_at = time.monotonic()
    record_user_action(
        user_id=owner,
        action="curation_reevaluate_success"
        if result.status == "succeeded"
        else "curation_reevaluate_failure",
        details={"job_id": job_id, "error": result.error},
    )
    async with _jobs_lock:
        _jobs[job_id] = result


@router.post("/jobs", response_model=JobCreated)
async def create_job(
    request: ReevaluateRequest, http_request: Request, current_user=Depends(_curator_dependency)
):
    from src import main

    owner = current_user.email.strip().lower()
    job_id = str(uuid.uuid4())
    async with _jobs_lock:
        cutoff = time.monotonic() - settings.annotation_job_ttl_seconds
        for expired in [
            key
            for key, job in _jobs.items()
            if job.finished_at is not None and job.finished_at < cutoff
        ]:
            del _jobs[expired]
        if (
            sum(
                job.owner_email == owner and job.status in ("queued", "running")
                for job in _jobs.values()
            )
            >= settings.curation_reevaluate_max_inflight_jobs
        ):
            raise HTTPException(status_code=429, detail="Too many in-flight re-evaluation jobs")
        _jobs[job_id] = JobStatus(owner_email=owner)
    main._track_background_task(
        _run_job(job_id, request, getattr(http_request.app.state, "run_store", None))
    )
    return JobCreated(job_id=job_id, status_url=f"/v1/curation/reevaluate/jobs/{job_id}")


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, current_user=Depends(_curator_dependency)):
    async with _jobs_lock:
        job = _jobs.get(job_id)
        if job is None or (
            job.owner_email != current_user.email.strip().lower() and current_user.role != "admin"
        ):
            raise HTTPException(status_code=404, detail="Job not found")
        return job
