"""Curator applications and administrator review, backed by the MySQL user store."""

from dataclasses import asdict
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from src.admin_users_routes import (
    require_admin_role,
    require_admin_write,
    _store,
    _store_unavailable,
)
from src.auth import AuthenticatedUser, UserStoreUnavailable, mysql_user_store_enabled, require_auth
from src.pipeline.user_store import (
    CuratorRequestAlreadyDecided,
    CuratorRequestNotFound,
    CuratorRequestIneligible,
    DuplicatePendingCuratorRequest,
    UserNotFound,
)
from src.request_origin import require_same_origin_json

router = APIRouter(tags=["curator requests"])


class Application(BaseModel):
    reason: str = Field(min_length=1, max_length=4000)
    affiliation: str = Field(min_length=1, max_length=255)
    expertise: str = Field(min_length=1, max_length=4000)
    orcid: str | None = Field(
        default=None, max_length=19, pattern=r"^[0-9]{4}-[0-9]{4}-[0-9]{4}-[0-9]{3}[0-9X]$"
    )

    @field_validator("reason", "affiliation", "expertise", mode="before")
    @classmethod
    def trim(cls, value):
        return value.strip() if isinstance(value, str) else value


class Review(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class RequestInfo(Application):
    id: int
    email: str
    status: Literal["pending", "approved", "rejected"]
    reviewer_email: str | None
    decided_at: datetime | None
    review_note: str | None
    created_at: datetime


class RequestPage(BaseModel):
    requests: list[RequestInfo]
    next_cursor: str | None


class MyRequestInfo(Application):
    """Applicant-visible fields: deliberately excludes administrator identities."""

    created_at: datetime
    decided_at: datetime | None
    review_note: str | None


class MyRequest(BaseModel):
    status: Literal["none", "pending", "approved", "rejected"]
    request: MyRequestInfo | None = None


def mysql_store():
    if not mysql_user_store_enabled():
        raise HTTPException(
            409, "Curator requests require the MySQL user store (USER_STORE_BACKEND=mysql)."
        )
    return _store()


async def require_user_write(request: Request, user: AuthenticatedUser = Depends(require_auth)):
    if user.auth_method != "api_key":
        require_same_origin_json(request)
    return user


@router.post("/v1/curator-requests", response_model=RequestInfo, status_code=201)
async def apply(payload: Application, user: AuthenticatedUser = Depends(require_user_write)):
    store = mysql_store()
    if user.role != "viewer" or user.status != "active":
        raise HTTPException(409, "Only active viewers can request curator access.")
    try:
        return asdict(await store.create_curator_request(user.email, **payload.model_dump()))
    except DuplicatePendingCuratorRequest:
        raise HTTPException(409, "You already have a pending curator request.") from None
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None


@router.get("/v1/curator-requests/me", response_model=MyRequest)
async def mine(user: AuthenticatedUser = Depends(require_auth)):
    store = mysql_store()
    try:
        page = await store.list_requests(email=user.email, limit=1)
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None
    record = page.requests[0] if page.requests else None
    return MyRequest(
        status=record.status if record else "none", request=asdict(record) if record else None
    )


@router.get("/v1/admin/curator-requests", response_model=RequestPage)
async def queue(
    status: Literal["pending", "approved", "rejected"] | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=255),
    user: AuthenticatedUser = Depends(require_admin_role),
):
    store = mysql_store()
    try:
        page = await store.list_requests(status=status, limit=limit, cursor=cursor)
        return RequestPage(
            requests=[asdict(r) for r in page.requests], next_cursor=page.next_cursor
        )
    except ValueError:
        raise HTTPException(422, "Invalid cursor.") from None
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None


async def decide(request_id: int, approve: bool, payload: Review, user: AuthenticatedUser):
    store = mysql_store()
    try:
        return asdict(
            await store.decide_request(
                request_id, approve, user.email, (payload.note or "").strip() or None
            )
        )
    except (CuratorRequestNotFound, UserNotFound):
        raise HTTPException(404, "Request or applicant not found.") from None
    except CuratorRequestAlreadyDecided:
        raise HTTPException(409, "This request has already been reviewed.") from None
    except CuratorRequestIneligible:
        raise HTTPException(409, "The applicant is no longer an active viewer.") from None
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None


@router.post("/v1/admin/curator-requests/{request_id}/approve", response_model=RequestInfo)
async def approve(
    request_id: int, payload: Review, user: AuthenticatedUser = Depends(require_admin_write)
):
    return await decide(request_id, True, payload, user)


@router.post("/v1/admin/curator-requests/{request_id}/reject", response_model=RequestInfo)
async def reject(
    request_id: int, payload: Review, user: AuthenticatedUser = Depends(require_admin_write)
):
    return await decide(request_id, False, payload, user)
