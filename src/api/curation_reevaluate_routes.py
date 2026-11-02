"""Background draft jobs using shared status snapshots and owner checks."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from src.auth import require_curator
from src.config import settings
from src.job_store import JobStore, can_read_job, job_owner
from src.observability import record_user_action
from src.pipeline.curation_reevaluate import (
    InvalidPMIDs,
    UpstreamUnavailable,
    SubjectNotResolved,
    ReevaluateRequest,
    reevaluate,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/curation/reevaluate", tags=["curation"])
# Depends binds at route definition time; runtime reassignment does not update the routes.
_curator_dependency = require_curator


class JobCreated(BaseModel):
    job_id: str
    status_url: str


class JobStatus(BaseModel):
    job_id: str = Field(exclude=True)
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    error: Optional[str] = None
    invalid_pmids: Optional[dict[str, list[str]]] = None
    draft: Optional[dict[str, Any]] = None
    owner_email: Optional[str] = Field(default=None, exclude=True)
    finished_at: Optional[float] = Field(default=None, exclude=True)


_jobs = JobStore("curation_reevaluate", JobStatus)
_jobs_lock = asyncio.Lock()


def _audit(owner: str, action: str, details: dict):
    try:
        record_user_action(user_id=owner, action=action, details=details)
    except Exception:
        logger.exception("Failed to audit curator re-evaluation action %s", action)


async def _run_job(job_id: str, request: ReevaluateRequest, run_store):
    job = await _jobs.get(job_id)
    if job is None:
        return
    owner = job.owner_email
    result = JobStatus(
        job_id=job_id, status="failed", error="Re-evaluation failed; please retry.", owner_email=owner
    )
    try:
        job.status = "running"
        await _jobs.put(job)
        _audit(owner, "curation_reevaluate_start", {"job_id": job_id})
        draft = await reevaluate(request, run_store=run_store)
        result = JobStatus(job_id=job_id, status="succeeded", draft=draft, owner_email=owner)
    except InvalidPMIDs as exc:
        result = JobStatus(
            job_id=job_id, status="failed", error=str(exc), invalid_pmids=exc.reasons, owner_email=owner
        )
    except (SubjectNotResolved, UpstreamUnavailable) as exc:
        result = JobStatus(job_id=job_id, status="failed", error=str(exc), owner_email=owner)
    except asyncio.CancelledError:
        result.error = "Job cancelled"
        raise
    except Exception:
        logger.exception("Curator re-evaluation job %s failed", job_id)
    finally:
        result.finished_at = time.monotonic()
        await _jobs.put(result)
        _audit(
            owner,
            "curation_reevaluate_success"
            if result.status == "succeeded"
            else "curation_reevaluate_failure",
            {"job_id": job_id, "error": result.error},
        )


@router.post("/jobs", response_model=JobCreated)
async def create_job(
    request: ReevaluateRequest, http_request: Request, current_user=Depends(_curator_dependency)
):
    from src import main

    owner = job_owner(current_user)
    job_id = str(uuid.uuid4())
    async with _jobs_lock:
        if (
            sum(
                job.owner_email == owner and job.status in ("queued", "running")
                for job, _ in _jobs.local.values()
            )
            >= settings.curation_reevaluate_max_inflight_jobs
        ):
            raise HTTPException(status_code=429, detail="Too many in-flight re-evaluation jobs")
        await _jobs.put(JobStatus(job_id=job_id, owner_email=owner))
    main._track_background_task(
        _run_job(job_id, request, getattr(http_request.app.state, "run_store", None))
    )
    return JobCreated(job_id=job_id, status_url=f"/v1/curation/reevaluate/jobs/{job_id}")


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(job_id: str, current_user=Depends(_curator_dependency)):
    job = await _jobs.get(job_id)
    if job is None or not can_read_job(job.owner_email, current_user):
        raise HTTPException(status_code=404, detail="Job not found")
    return job
