"""HTTP API for the expression-context module (mounted only when enabled)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from src.auth import AuthenticatedUser, require_auth
from src.admin_users_routes import require_admin_role, require_admin_write
from src.expression_context.catalog import BundleGone, BundleNotLoaded
from src.expression_context.models import (
    BatchAccepted,
    BatchCreateRequest,
    BatchDetail,
    BatchListPage,
    BundleStatus,
    GeneResult,
    GeneStatsView,
    HistoryPage,
    JobAccepted,
    JobCreateRequest,
    JobStatus,
)
from src.expression_context.csv_export import UnmappedCancerTypeError
from src.expression_context.service import (
    BatchNotFound,
    ExpressionContextService,
    GeneNotFound,
    GenesNotChecked,
    InvalidGene,
)
from src.expression_context.store import InvalidCursor
from src.job_store import can_read_job, job_owner
from src.request_origin import require_same_origin_json

PREFIX = "/v1/expression-context"
STATE_ATTR = "expression_context"
NOT_LOADED_RETRY_AFTER_SECONDS = 60

router = APIRouter(prefix=PREFIX, tags=["expression-context"])


def _check_browser_csrf(request: Request, user: AuthenticatedUser) -> None:
    """Enforce same-origin JSON check when browser CSRF headers are present."""
    if user.auth_method == "api_key":
        return
    if any(
        request.headers.get(h) is not None
        for h in ("sec-fetch-site", "origin", "referer")
    ):
        require_same_origin_json(request)


def get_service(request: Request) -> ExpressionContextService:
    service = getattr(request.app.state, STATE_ATTR, None)
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Expression context is not ready.",
        )
    return service


def _not_loaded(exc: BundleNotLoaded) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=(
            "Expression statistics are not loaded yet (no release bundle has been imported). "
            "Try again in a few minutes."
        ),
        headers={"Retry-After": str(NOT_LOADED_RETRY_AFTER_SECONDS)},
    )


@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobAccepted)
async def create_job(
    request: Request,
    body: JobCreateRequest,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> JobAccepted:
    try:
        if body.refresh:
            await require_admin_role(user)
            await require_admin_write(request, user)
        job = await service.submit(body.gene, owner=job_owner(user), refresh=body.refresh)
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Gene {exc} is not in the expression statistics bundle."
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
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
    try:
        return await service.get_job_status(job)
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


@router.get("/bundle", response_model=BundleStatus)
async def get_bundle_status(
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> BundleStatus:
    """Provenance of the active bundle plus the cluster's refresh bookkeeping."""
    return await service.bundle_status()


@router.get("/stats/{gene}", response_model=GeneStatsView)
async def get_gene_stats(
    gene: str,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> GeneStatsView:
    """Per-cancer-type statistics from the active bundle, with stored verdicts."""
    try:
        return await service.stats_view(gene)
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Gene {exc} is not in the expression statistics bundle."
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


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
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


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
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


@router.get("/genes/{gene}/export.csv")
async def export_gene_csv(
    gene: str,
    include_excluded: bool = Query(default=False),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> Response:
    try:
        filename, body = await service.export_gene_csv(gene, include_excluded=include_excluded)
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"No stored expression-context result for {exc}."
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/export.csv")
async def export_all_csv(
    include_excluded: bool = Query(default=False),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> StreamingResponse:
    try:
        filename, stream = await service.export_bundle_csv(include_excluded=include_excluded)
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return StreamingResponse(
        stream,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/golden-set.csv")
async def export_golden_set_csv(
    genes: Optional[str] = Query(default=None),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> Response:
    try:
        if genes is not None:
            filename, body = await service.export_golden_set_genes_csv(genes)
            return Response(
                content=body,
                media_type="text/csv; charset=utf-8",
                headers={"Content-Disposition": f'attachment; filename="{filename}"'},
            )
        filename, stream = await service.export_golden_set_bundle_csv()
        return StreamingResponse(
            stream,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except GenesNotChecked as exc:
        raise HTTPException(
            status_code=422,
            detail={"not_yet_checked": exc.genes},
        ) from exc
    except (InvalidGene, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except UnmappedCancerTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


@router.get("/genes/{gene}/curator-sheet.csv")
async def export_gene_curator_sheet_csv(
    gene: str,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> Response:
    try:
        filename, body = await service.export_gene_curator_sheet_csv(gene)
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except GeneNotFound as exc:
        raise HTTPException(
            status_code=404, detail=f"Gene {exc} is not in the expression statistics bundle."
        ) from exc
    except UnmappedCancerTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/batches", status_code=status.HTTP_202_ACCEPTED, response_model=BatchAccepted)
async def create_batch(
    request: Request,
    body: BatchCreateRequest,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> BatchAccepted:
    _check_browser_csrf(request, user)
    try:
        batch = await service.submit_batch(body.genes, owner=job_owner(user))
    except InvalidGene as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return BatchAccepted(
        batch_id=batch.batch_id,
        status=batch.status,
        status_url=f"{PREFIX}/batches/{batch.batch_id}",
    )


@router.get("/batches", response_model=BatchListPage)
async def list_batches(
    limit: int = Query(default=25, ge=1, le=100),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> BatchListPage:
    try:
        await service.catalog.active()
        owner = None if user.role == "admin" else job_owner(user)
        return await service.list_user_batches(owner, limit=limit)
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


@router.get("/batches/{batch_id}", response_model=BatchDetail)
async def get_batch(
    batch_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=2000),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> BatchDetail:
    try:
        await service.catalog.active()
        detail = await service.get_batch_detail(batch_id, offset=offset, limit=limit)
    except BatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    if not can_read_job(detail.created_by, user):
        raise HTTPException(status_code=404, detail="Batch not found.")
    return detail


@router.post("/batches/{batch_id}/cancel", response_model=BatchDetail)
async def cancel_batch(
    batch_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> BatchDetail:
    _check_browser_csrf(request, user)
    try:
        await service.catalog.active()
        existing = await service.get_batch_detail(batch_id, offset=0, limit=1)
    except BatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    if not can_read_job(existing.created_by, user):
        raise HTTPException(status_code=404, detail="Batch not found.")
    try:
        return await service.cancel_batch(batch_id)
    except BatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc


@router.get("/batches/{batch_id}/export.csv")
async def export_batch_csv(
    batch_id: str,
    include_excluded: bool = Query(default=False),
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> Response:
    try:
        batch = await service.store.get_batch(batch_id)
        if batch is None or not can_read_job(batch.created_by, user):
            raise HTTPException(status_code=404, detail="Batch not found.")
        filename, body = await service.export_batch_per_pair_csv(
            batch_id, include_excluded=include_excluded
        )
    except BatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/batches/{batch_id}/golden-set.csv")
async def export_batch_golden_set_csv(
    batch_id: str,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> Response:
    try:
        batch = await service.store.get_batch(batch_id)
        if batch is None or not can_read_job(batch.created_by, user):
            raise HTTPException(status_code=404, detail="Batch not found.")
        filename, body = await service.export_batch_golden_set_csv(batch_id)
    except BatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except UnmappedCancerTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/batches/{batch_id}/curator-sheet.csv")
async def export_batch_curator_sheet_csv(
    batch_id: str,
    user: AuthenticatedUser = Depends(require_auth),
    service: ExpressionContextService = Depends(get_service),
) -> Response:
    try:
        batch = await service.store.get_batch(batch_id)
        if batch is None or not can_read_job(batch.created_by, user):
            raise HTTPException(status_code=404, detail="Batch not found.")
        filename, body = await service.export_batch_curator_sheet_csv(batch_id)
    except BatchNotFound as exc:
        raise HTTPException(status_code=404, detail="Batch not found.") from exc
    except UnmappedCancerTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc
    except (BundleNotLoaded, BundleGone) as exc:
        raise _not_loaded(exc) from exc
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
