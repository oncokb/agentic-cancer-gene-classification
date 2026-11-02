"""Authenticated metadata-only listing of the caller's saved runs."""

from datetime import timezone
from typing import List, Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError

from src.api.run_persistence import can_modify_run, run_owner
from src.auth import AuthenticatedUser, require_auth
from src.models.schema import FusionInput
from src.pipeline.run_store import _decode_runs_cursor

router = APIRouter()


class RunSummaryResponse(BaseModel):
    """Saved run metadata, without request or result payloads."""

    run_id: str = Field(description="Unique saved run identifier.")
    created_at: str = Field(description="Run creation time as an ISO-8601 UTC timestamp.")
    source: Optional[str] = Field(description="Run origin: session, api_key, local, rerun, or null.")
    input_summary: Optional[str] = Field(description="Short summary of the run's inputs.")
    input_count: Optional[int] = Field(description="Number of inputs, or null for older runs.")
    genes_annotated: Optional[int] = Field(description="Number of genes annotated, if recorded.")
    view_url: str = Field(description="Relative URL for opening the saved run.")


class RunListResponse(BaseModel):
    """One page of the authenticated caller's runs, newest first."""

    runs: List[RunSummaryResponse] = Field(description="Run summaries in descending creation order.")
    next_cursor: Optional[str] = Field(description="Opaque cursor for the next page; null at the end.")


class RunAccessResponse(BaseModel):
    """Whether the caller may modify this saved run (owner or admin)."""

    run_id: str
    can_modify: bool


class RunInputsResponse(BaseModel):
    """The original inputs a run was submitted with, for a retry request."""

    run_id: str
    inputs: List[FusionInput] = Field(
        default_factory=list,
        description="Inputs exactly as submitted (including tumor type and breakpoint fields).",
    )


@router.get("/v1/runs/{run_id}/inputs", response_model=RunInputsResponse)
async def get_run_inputs(
    run_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> RunInputsResponse:
    """Return a run's original submitted inputs to its owner or an admin, so a
    retry can preserve tumor type and breakpoint context instead of guessing it
    from the stored annotations."""
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Run storage is unavailable.")
    metadata = await store.get_run_metadata(run_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if not can_modify_run(metadata.get("owner_email"), current_user):
        raise HTTPException(
            status_code=403,
            detail="Only this run's owner or an administrator may retry its annotations.",
        )
    request_payload = await store.get_run_request(run_id) or {}
    raw_inputs = request_payload.get("fusions")
    if raw_inputs is None and isinstance(request_payload.get("gene"), str):
        # Saved by POST /v1/annotate/gene.
        raw_inputs = [
            {"fusion": request_payload["gene"], "tumor_type": request_payload.get("tumor_type")}
        ]
    inputs: List[FusionInput] = []
    for item in raw_inputs if isinstance(raw_inputs, list) else []:
        try:
            inputs.append(
                FusionInput.model_validate({"fusion": item} if isinstance(item, str) else item)
            )
        except ValidationError:
            # An older or malformed saved input: the UI falls back to the gene.
            continue
    return RunInputsResponse(run_id=run_id, inputs=inputs)


@router.get("/v1/runs/{run_id}/access", response_model=RunAccessResponse)
async def get_run_access(
    run_id: str,
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
) -> RunAccessResponse:
    """Tell the UI whether to offer owner-only actions (e.g. retrying failed
    fusion-evidence lookups) without exposing the run's request or result."""
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Run storage is unavailable.")
    metadata = await store.get_run_metadata(run_id)
    if metadata is None:
        raise HTTPException(status_code=404, detail="Run not found")
    return RunAccessResponse(
        run_id=run_id,
        can_modify=can_modify_run(metadata.get("owner_email"), current_user),
    )


@router.get("/v1/runs", response_model=RunListResponse)
async def list_runs(
    request: Request,
    current_user: AuthenticatedUser = Depends(require_auth),
    limit: int = Query(20, ge=1, le=100, description="Maximum number of runs per page (1–100)."),
    cursor: Optional[str] = Query(None, description="Opaque next_cursor from the previous page."),
) -> RunListResponse:
    """List only the caller's saved runs, without loading request/result blobs."""
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="Run storage is unavailable.")
    if cursor is not None:
        try:
            _decode_runs_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid cursor") from exc
    page = await store.list_runs_for_owner(
        run_owner(current_user)["owner_email"], limit=limit, cursor=cursor,
    )
    return RunListResponse(
        runs=[
            RunSummaryResponse(
                run_id=run.run_id,
                created_at=run.created_at.astimezone(timezone.utc).isoformat(),
                source=run.source,
                input_summary=run.input_summary,
                input_count=run.input_count,
                genes_annotated=run.genes_annotated,
                view_url=f"/?run={quote(run.run_id, safe='')}",
            )
            for run in page.runs
        ],
        next_cursor=page.next_cursor,
    )
