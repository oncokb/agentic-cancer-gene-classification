"""Admin user management (/v1/admin): list users, change roles and statuses,
read the change log.

Admins only (an API key acts with its owner's current role). Changes go
through UserStore.change_user_access, which writes one role_changes row per
changed field with the acting admin as actor, and refuses (in the same
transaction) to leave no active admin. Changes and the change log need
USER_STORE_BACKEND=mysql: on redis the user list is read-only and the other
endpoints answer 409. Every store failure is a 503; nothing fails open.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from src import auth
from src.auth import (
    AuthenticatedUser,
    UserProfile,
    UserStoreUnavailable,
    describe_store_error,
    load_user_profile_strict,
    mysql_user_store_enabled,
    require_auth,
)
from src.pipeline.user_store import (
    DEFAULT_PAGE_LIMIT,
    MAX_PAGE_LIMIT,
    LastActiveAdmin,
    RoleChange,
    UserFilters,
    UserNotFound,
    decode_user_cursor,
    encode_user_cursor,
    normalize_actor,
    normalize_email,
    search_key,
    to_utc,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/admin", tags=["admin"])

READ_ONLY_DETAIL = (
    "User management requires USER_STORE_BACKEND=mysql; with the Redis user store "
    "users are read-only and there is no change log."
)
LAST_ADMIN_DETAIL = "This change would leave no active admin. Promote or re-enable another admin first."
SELF_DISABLE_DETAIL = "You can't disable your own account. Ask another admin."
UNAVAILABLE_DETAIL = "The user store is unavailable right now. Please retry shortly."
USER_NOT_FOUND_DETAIL = "User not found."


class AdminUser(BaseModel):
    email: str
    name: str
    domain: str
    provider: str
    role: str
    status: str
    role_updated_by: Optional[str] = None
    role_updated_at: Optional[datetime] = None
    first_login_at: Optional[datetime] = None
    last_login_at: Optional[datetime] = None
    login_count: int = 0
    annotation_count: int = 0
    last_annotation_at: Optional[datetime] = None


class AdminUserPage(BaseModel):
    users: List[AdminUser]
    next_cursor: Optional[str] = Field(description="Opaque cursor for the next page; null at the end.")
    backend: Literal["mysql", "redis"] = Field(description="The USER_STORE_BACKEND in use.")
    writable: bool = Field(description="False on the Redis user store: changes answer 409.")


class AccessChangeRequest(BaseModel):
    role: Optional[Literal["viewer", "curator", "admin"]] = None
    # Approve (pending -> active), disable, re-enable; nobody is moved back to pending.
    status: Optional[Literal["active", "disabled"]] = None
    reason: Optional[str] = Field(default=None, max_length=1000)


class RoleChangeInfo(BaseModel):
    id: int
    email: str
    field: str
    old_value: Optional[str] = None
    new_value: str
    actor: str
    reason: Optional[str] = None
    request_id: Optional[int] = None
    created_at: datetime


class AccessChangeResponse(BaseModel):
    user: AdminUser
    changes: List[RoleChangeInfo] = Field(description="One entry per changed field; [] for a no-op.")


class RoleChangePageResponse(BaseModel):
    changes: List[RoleChangeInfo]
    next_cursor: Optional[str] = Field(description="Opaque cursor for the next page; null at the end.")


async def require_admin_role(current_user: AuthenticatedUser = Depends(require_auth)) -> AuthenticatedUser:
    """The caller's current role must be admin. Unlike require_admin, ACGC dev
    mode doesn't open this up: it changes who may use the app."""
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required for this action.",
        )
    return current_user


def _store_unavailable(exc: BaseException) -> HTTPException:
    logger.error("Admin user management: user store failed: %s", describe_store_error(exc))
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=UNAVAILABLE_DETAIL)


def _require_mysql() -> None:
    if not mysql_user_store_enabled():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=READ_ONLY_DETAIL)


def _store():
    try:
        return auth._mysql_store()
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None


def _user_from_record(record) -> AdminUser:
    return AdminUser(
        email=record.email, name=record.name, domain=record.domain, provider=record.provider,
        role=record.role, status=record.status, role_updated_by=record.role_updated_by,
        role_updated_at=record.role_updated_at, first_login_at=record.first_login_at,
        last_login_at=record.last_login_at, login_count=record.login_count,
        annotation_count=record.annotation_count, last_annotation_at=record.last_annotation_at,
    )


def _user_from_profile(profile: UserProfile) -> AdminUser:
    return AdminUser(
        email=profile.email, name=profile.name, domain=profile.domain, provider=profile.provider,
        role=profile.role, status=profile.status, first_login_at=profile.first_login_at,
        last_login_at=profile.last_login_at, login_count=profile.login_count,
        annotation_count=profile.annotation_count, last_annotation_at=profile.last_annotation_at,
    )


def _change_info(change: RoleChange) -> RoleChangeInfo:
    return RoleChangeInfo(**asdict(change))


def _target_email(email: str) -> str:
    try:
        return normalize_email(email)
    except ValueError:
        # A malformed address can't name a user.
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND_DETAIL) from None


async def _redis_profiles() -> List[UserProfile]:
    """Every Redis-backend profile, read strictly: any Redis error raises
    UserStoreUnavailable instead of falling back to the in-process copy."""
    redis_client = await auth._profile_store_redis()
    if redis_client is None:
        async with auth._user_store_lock:
            return [
                auth._with_activity(p, auth._user_activity.get(p.email, {}))
                for p in auth._user_store.values()
            ]
    try:
        emails = sorted(await redis_client.smembers("agcg:users"))
        profiles = []
        for email in emails:
            email = email.decode() if isinstance(email, bytes) else email
            profile = await load_user_profile_strict(email)
            if profile is not None:
                activity = await redis_client.hgetall(auth._activity_key(profile.email))
                profiles.append(auth._with_activity(profile, activity))
        return profiles
    except UserStoreUnavailable:
        raise
    except Exception as exc:
        raise UserStoreUnavailable("Failed to list users from Redis") from exc


async def _list_redis_users(filters: UserFilters, limit: int, cursor: Optional[str]) -> AdminUserPage:
    """The MySQL listing's order, filters, search and cursors over Redis profiles."""
    after = decode_user_cursor(cursor) if cursor is not None else None
    needle = search_key(filters.search)
    users = []
    for profile in await _redis_profiles():
        user = _user_from_profile(profile)
        key = (to_utc(user.last_login_at), user.email)
        if (
            (filters.role is None or user.role == filters.role)
            and (filters.status is None or user.status == filters.status)
            and (not needle or needle in search_key(user.email) or needle in search_key(user.name))
            and (after is None or key < after)
        ):
            users.append((key, user))
    users.sort(key=lambda item: item[0], reverse=True)
    page = [user for _, user in users[:limit]]
    next_cursor = encode_user_cursor(page[-1].last_login_at, page[-1].email) if len(users) > limit else None
    return AdminUserPage(users=page, next_cursor=next_cursor, backend="redis", writable=False)


@router.get("/users", response_model=AdminUserPage)
async def list_users(
    search: Optional[str] = Query(default=None, max_length=255, description="Substring of email or name."),
    role: Optional[Literal["viewer", "curator", "admin"]] = Query(default=None),
    user_status: Optional[Literal["pending", "active", "disabled"]] = Query(default=None, alias="status"),
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: Optional[str] = Query(default=None),
    current_user: AuthenticatedUser = Depends(require_admin_role),
) -> AdminUserPage:
    """Users by most recent login first, filtered by role/status and searched
    by email or name (case-insensitive substring)."""
    filters = UserFilters(role=role, status=user_status, search=search)
    try:
        if not mysql_user_store_enabled():
            return await _list_redis_users(filters, limit, cursor)
        page = await _store().list_users(filters, limit=limit, cursor=cursor)
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid cursor.") from None
    return AdminUserPage(
        users=[_user_from_record(u) for u in page.users], next_cursor=page.next_cursor,
        backend="mysql", writable=True,
    )


@router.post("/users/{email}/changes", response_model=AccessChangeResponse)
async def change_user_access(
    email: str,
    payload: AccessChangeRequest,
    current_user: AuthenticatedUser = Depends(require_admin_role),
) -> AccessChangeResponse:
    """Set a user's role and/or status in one transaction, logged in
    role_changes with the acting admin as actor. A no-op writes nothing."""
    if payload.role is None and payload.status is None:
        raise HTTPException(status_code=422, detail="Give a role, a status, or both.")
    _require_mysql()
    target = _target_email(email)
    actor = normalize_actor(current_user.email)
    if payload.status == "disabled" and target == actor:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=SELF_DISABLE_DETAIL)
    reason = (payload.reason or "").strip() or None
    store = _store()
    try:
        changes = await store.change_user_access(
            target, role=payload.role, status=payload.status, actor=actor, reason=reason,
        )
        record = await store.get_user(target)
    except UserNotFound:
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND_DETAIL) from None
    except LastActiveAdmin:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=LAST_ADMIN_DETAIL) from None
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None
    if record is None:
        raise HTTPException(status_code=404, detail=USER_NOT_FOUND_DETAIL)
    for change in changes:
        logger.info(
            "Admin %s changed %s of %s from %s to %s",
            actor, change.field, target, change.old_value, change.new_value,
        )
    return AccessChangeResponse(user=_user_from_record(record), changes=[_change_info(c) for c in changes])


async def _role_change_page(email: Optional[str], limit: int, cursor: Optional[str]) -> RoleChangePageResponse:
    _require_mysql()
    store = _store()
    try:
        if email is not None and await store.get_user(email) is None:
            raise HTTPException(status_code=404, detail=USER_NOT_FOUND_DETAIL)
        page = await store.list_role_changes(email=email, limit=limit, cursor=cursor)
    except UserStoreUnavailable as exc:
        raise _store_unavailable(exc) from None
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid cursor.") from None
    return RoleChangePageResponse(
        changes=[_change_info(c) for c in page.changes], next_cursor=page.next_cursor,
    )


@router.get("/role-changes", response_model=RoleChangePageResponse)
async def list_role_changes(
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: Optional[str] = Query(default=None),
    current_user: AuthenticatedUser = Depends(require_admin_role),
) -> RoleChangePageResponse:
    """Every role/status change, newest first."""
    return await _role_change_page(None, limit, cursor)


@router.get("/users/{email}/changes", response_model=RoleChangePageResponse)
async def list_user_role_changes(
    email: str,
    limit: int = Query(default=DEFAULT_PAGE_LIMIT, ge=1, le=MAX_PAGE_LIMIT),
    cursor: Optional[str] = Query(default=None),
    current_user: AuthenticatedUser = Depends(require_admin_role),
) -> RoleChangePageResponse:
    """One user's role/status changes, newest first."""
    return await _role_change_page(_target_email(email), limit, cursor)
