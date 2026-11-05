"""HTTP routes for the expression-context module (mounted only when enabled)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from src.auth import AuthenticatedUser, require_auth
from src.expression_context.artifact_client import ArtifactContractError, ArtifactError
from src.expression_context.models import (
    GeneResult,
    HistoryPage,
    JobAccepted,
    JobCreateRequest,
    JobStatus,
)
from src.expression_context.service import ExpressionContextService, GeneNotFound, InvalidGene
from src.expression_context.store import InvalidCursor
from src.job_store import can_read_job, job_owner

PREFIX = "/v1/expression-context"
STATE_ATTR = "expression_context"

router = APIRouter(prefix=PREFIX, tags=["expression-context"])


def get_service(request: Request) -> ExpressionContextService:
    service = getattr(request.app.state, STATE_ATTR, None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Expression context is not ready.",
        )
    return service


def _artifact_http_error(exc: ArtifactError) -> HTTPException:
    if isinstance(exc, ArtifactContractError):
        detail = f"Expression artifact violates the v1 contract: {exc}"
    else:
        detail = f"Expression artifact unavailable: {exc}"
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def create_job(
    body: JobCreateRequest,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> JobAccepted:
    try:
        job = await service.submit(body.gene, owner=job_owner(user))
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Gene {exc} is not in the expression artifact."
        ) from exc
    except ArtifactError as exc:
        raise _artifact_http_error(exc) from exc
    return JobAccepted(
        job_id=job.job_id, status=job.status, status_url=f"{PREFIX}/jobs/{job.job_id}"
    )


@router.get("/jobs/{job_id}", response_model=JobStatus)
async def get_job(
    job_id: str,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> JobStatus:
    job = await service.jobs.get(job_id)
    if job is None or not can_read_job(job.owner, user):
        raise HTTPException(status_code=404, detail="Job not found.")
    return job.public()


@router.get("/genes", response_model=HistoryPage)
async def list_genes(
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=512),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> HistoryPage:
    try:
        return await service.history(limit, cursor)
    except InvalidCursor as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor.") from exc


@router.get("/genes/{gene}", response_model=GeneResult, response_model_exclude_none=False)
async def get_gene(
    gene: str,
    include_excluded: bool = Query(default=False),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> GeneResult:
    try:
        return await service.latest_result(gene, include_excluded=include_excluded)
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"No stored expression-context result for {exc}."
        ) from exc
