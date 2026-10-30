"""In-memory UserStore with the same semantics as the MySQL UserStoreMixin.

For unit tests: a login never overwrites role or status, an email has at
most one pending curator request, and every role/status change writes its
role_changes row atomically (all under one lock, applied only after every
check passes). tests/test_user_store_contract.py runs the shared suite
against both implementations.

Set `unavailable = True` to make every call raise UserStoreUnavailable.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional

from src.auth import UserStoreUnavailable
from src.pipeline.user_store import (
    BOOTSTRAP_ACTOR,
    BOOTSTRAP_ALREADY_ADMIN,
    BOOTSTRAP_GRANTED,
    BOOTSTRAP_PREVIOUSLY_GRANTED,
    DEFAULT_PAGE_LIMIT,
    REQUEST_STATUSES,
    CuratorRequest,
    CuratorRequestAlreadyDecided,
    CuratorRequestNotFound,
    CuratorRequestPage,
    DuplicatePendingCuratorRequest,
    LoginIdentity,
    RoleChange,
    RoleChangePage,
    UserFilters,
    UserNotFound,
    UserPage,
    UserRecord,
    check_limit,
    check_role,
    check_status,
    decode_id_cursor,
    decode_user_cursor,
    encode_id_cursor,
    encode_user_cursor,
    normalize_actor,
    normalize_claims,
    normalize_email,
    normalize_groups,
    normalize_name,
    prepare_import,
    search_key,
    to_utc,
    utc_now,
)


def _db_time(value: Optional[datetime] = None) -> datetime:
    """Like a DATETIME(6) round trip: aware UTC, microsecond precision."""
    return to_utc(value or utc_now())


class FakeUserStore:
    def __init__(self) -> None:
        self._users: Dict[str, UserRecord] = {}
        self._requests: Dict[int, CuratorRequest] = {}
        self._role_changes: List[RoleChange] = []
        self._next_request_id = 1
        self._next_change_id = 1
        self._lock = asyncio.Lock()
        self.unavailable = False

    def _check_available(self, what: str) -> None:
        if self.unavailable:
            raise UserStoreUnavailable(f"User store {what} failed")

    async def get_user(self, email: str) -> Optional[UserRecord]:
        clean = normalize_email(email)
        self._check_available("get_user")
        async with self._lock:
            return copy.deepcopy(self._users.get(clean))

    async def upsert_login(
        self,
        identity: LoginIdentity,
        *,
        default_role: str,
        default_status: str,
        at: Optional[datetime] = None,
    ) -> UserRecord:
        email = normalize_email(identity.email)
        check_role(default_role)
        check_status(default_status)
        self._check_available("upsert_login")
        now = _db_time(at)
        domain = identity.domain or email.split("@")[-1]
        given_name = normalize_name(identity.name)
        name = given_name or email
        groups = normalize_groups(identity.groups)
        claims = normalize_claims(identity.claims)
        async with self._lock:
            existing = self._users.get(email)
            if existing is None:
                user = UserRecord(
                    email=email,
                    name=name,
                    picture=identity.picture or None,
                    domain=domain,
                    provider=identity.provider,
                    status=default_status,
                    role=default_role,
                    role_updated_by=None,
                    role_updated_at=None,
                    groups=groups,
                    claims=claims,
                    first_login_at=now,
                    last_login_at=now,
                    login_count=1,
                    annotation_count=0,
                    last_annotation_at=None,
                    created_at=now,
                    updated_at=now,
                )
            else:
                user = replace(
                    existing,
                    # A login without a name keeps the known one.
                    name=name if given_name else existing.name,
                    picture=identity.picture or existing.picture,
                    domain=domain,
                    provider=identity.provider,
                    groups=groups,
                    claims=claims,
                    last_login_at=max(existing.last_login_at, now),
                    login_count=existing.login_count + 1,
                    updated_at=now,
                )
            self._users[email] = user
            return copy.deepcopy(user)

    async def import_user(self, user: UserRecord) -> bool:
        user = prepare_import(user)
        self._check_available("import_user")
        async with self._lock:
            if user.email in self._users:
                return False
            self._users[user.email] = user
            return True

    async def grant_bootstrap_admin(self, email: str, *, reason: str) -> str:
        clean = normalize_email(email)
        self._check_available("grant_bootstrap_admin")
        async with self._lock:
            user = self._users.get(clean)
            if user is None:
                raise UserNotFound(clean)
            previously_granted = any(
                c.email == clean and c.field == "role" and c.new_value == "admin"
                and c.actor == BOOTSTRAP_ACTOR
                for c in self._role_changes
            )
            if user.role == "admin":
                if not previously_granted:
                    self._append_role_change(RoleChange(
                        id=self._next_change_id,
                        email=clean,
                        field="role",
                        old_value="admin",
                        new_value="admin",
                        actor=BOOTSTRAP_ACTOR,
                        reason=reason,
                        request_id=None,
                        created_at=_db_time(),
                    ))
                return BOOTSTRAP_ALREADY_ADMIN
            if previously_granted:
                return BOOTSTRAP_PREVIOUSLY_GRANTED
            self._apply_user_field(
                "role", clean, "admin", actor=BOOTSTRAP_ACTOR, reason=reason, request_id=None,
                now=_db_time(),
            )
            return BOOTSTRAP_GRANTED

    def _apply_user_field(
        self,
        field_name: str,
        email: str,
        value: str,
        *,
        actor: str,
        reason: Optional[str],
        request_id: Optional[int],
        now: datetime,
    ) -> Optional[RoleChange]:
        """Caller holds the lock. Validates first, then mutates both records."""
        user = self._users.get(email)
        if user is None:
            raise UserNotFound(email)
        old_value = getattr(user, field_name)
        if old_value == value:
            return None
        change = RoleChange(
            id=self._next_change_id,
            email=email,
            field=field_name,
            old_value=old_value,
            new_value=value,
            actor=actor,
            reason=reason,
            request_id=request_id,
            created_at=now,
        )
        if field_name == "role":
            updated = replace(user, role=value, role_updated_by=actor, role_updated_at=now, updated_at=now)
        else:
            updated = replace(user, status=value, updated_at=now)
        self._append_role_change(change)
        self._users[email] = updated
        return change

    def _append_role_change(self, change: RoleChange) -> None:
        # Separate so tests can make the audit write fail and check nothing changed.
        self._role_changes.append(change)
        self._next_change_id += 1

    async def set_role(
        self, email: str, role: str, *, actor: str, reason: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> Optional[RoleChange]:
        clean = normalize_email(email)
        check_role(role)
        actor = normalize_actor(actor)
        self._check_available("set_role")
        async with self._lock:
            return self._apply_user_field(
                "role", clean, role, actor=actor, reason=reason, request_id=request_id,
                now=_db_time(),
            )

    async def set_status(
        self, email: str, status: str, *, actor: str, reason: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> Optional[RoleChange]:
        clean = normalize_email(email)
        check_status(status)
        actor = normalize_actor(actor)
        self._check_available("set_status")
        async with self._lock:
            return self._apply_user_field(
                "status", clean, status, actor=actor, reason=reason, request_id=request_id,
                now=_db_time(),
            )

    async def incr_activity(self, email: str, n: int = 1, at: Optional[datetime] = None) -> bool:
        clean = normalize_email(email)
        if n < 1:
            raise ValueError("n must be at least 1")
        self._check_available("incr_activity")
        when = _db_time(at)
        async with self._lock:
            user = self._users.get(clean)
            if user is None:
                return False
            last = user.last_annotation_at
            self._users[clean] = replace(
                user,
                annotation_count=user.annotation_count + n,
                last_annotation_at=when if last is None else max(last, when),
            )
            return True

    async def list_users(
        self, filters: Optional[UserFilters] = None, limit: int = DEFAULT_PAGE_LIMIT,
        cursor: Optional[str] = None,
    ) -> UserPage:
        filters = filters or UserFilters()
        limit = check_limit(limit)
        if filters.role is not None:
            check_role(filters.role)
        if filters.status is not None:
            check_status(filters.status)
        after = decode_user_cursor(cursor) if cursor is not None else None
        self._check_available("list_users")
        needle = search_key(filters.search)
        async with self._lock:
            users = [
                u for u in self._users.values()
                if (filters.role is None or u.role == filters.role)
                and (filters.status is None or u.status == filters.status)
                and (not needle or needle in search_key(u.email) or needle in search_key(u.name))
                and (after is None or (u.last_login_at, u.email) < after)
            ]
            users.sort(key=lambda u: (u.last_login_at, u.email), reverse=True)
            page = copy.deepcopy(users[:limit])
        next_cursor = (
            encode_user_cursor(page[-1].last_login_at, page[-1].email)
            if len(users) > limit else None
        )
        return UserPage(users=page, next_cursor=next_cursor)

    async def count_admins(self, *, active_only: bool = True) -> int:
        self._check_available("count_admins")
        async with self._lock:
            return sum(
                1 for u in self._users.values()
                if u.role == "admin" and (not active_only or u.status == "active")
            )

    async def create_curator_request(
        self, email: str, *, reason: str, affiliation: str, expertise: str,
        orcid: Optional[str] = None,
    ) -> CuratorRequest:
        clean = normalize_email(email)
        self._check_available("create_curator_request")
        async with self._lock:
            if any(r.email == clean and r.status == "pending" for r in self._requests.values()):
                raise DuplicatePendingCuratorRequest(clean)
            request = CuratorRequest(
                id=self._next_request_id, email=clean, reason=reason, affiliation=affiliation,
                expertise=expertise, orcid=orcid or None, status="pending", reviewer_email=None,
                decided_at=None, review_note=None, created_at=_db_time(),
            )
            self._requests[request.id] = request
            self._next_request_id += 1
            return request

    async def get_curator_request(self, request_id: int) -> Optional[CuratorRequest]:
        self._check_available("get_curator_request")
        async with self._lock:
            return self._requests.get(request_id)

    async def get_pending_request(self, email: str) -> Optional[CuratorRequest]:
        clean = normalize_email(email)
        self._check_available("get_pending_request")
        async with self._lock:
            for request in self._requests.values():
                if request.email == clean and request.status == "pending":
                    return request
            return None

    async def list_requests(
        self, *, status: Optional[str] = None, email: Optional[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT, cursor: Optional[str] = None,
    ) -> CuratorRequestPage:
        limit = check_limit(limit)
        if status is not None and status not in REQUEST_STATUSES:
            raise ValueError(f"Unknown request status {status!r}")
        clean = normalize_email(email) if email is not None else None
        before = decode_id_cursor(cursor) if cursor is not None else None
        self._check_available("list_requests")
        async with self._lock:
            requests = [
                r for r in self._requests.values()
                if (status is None or r.status == status)
                and (clean is None or r.email == clean)
                and (before is None or r.id < before)
            ]
        requests.sort(key=lambda r: r.id, reverse=True)
        page = requests[:limit]
        next_cursor = encode_id_cursor(page[-1].id) if len(requests) > limit else None
        return CuratorRequestPage(requests=page, next_cursor=next_cursor)

    async def decide_request(
        self, request_id: int, approve: bool, reviewer: str, note: Optional[str] = None
    ) -> CuratorRequest:
        reviewer = normalize_actor(reviewer)
        self._check_available("decide_request")
        now = _db_time()
        async with self._lock:
            request = self._requests.get(request_id)
            if request is None:
                raise CuratorRequestNotFound(request_id)
            if request.status != "pending":
                raise CuratorRequestAlreadyDecided(request_id)
            if approve:
                user = self._users.get(request.email)
                if user is None:
                    raise UserNotFound(request.email)
                if user.role == "viewer":
                    self._apply_user_field(
                        "role", request.email, "curator", actor=reviewer, reason=note,
                        request_id=request_id, now=now,
                    )
            decided = replace(
                request,
                status="approved" if approve else "rejected",
                reviewer_email=reviewer,
                decided_at=now,
                review_note=note,
            )
            self._requests[request_id] = decided
            return decided

    async def list_role_changes(
        self, *, email: Optional[str] = None, limit: int = DEFAULT_PAGE_LIMIT,
        cursor: Optional[str] = None,
    ) -> RoleChangePage:
        limit = check_limit(limit)
        clean = normalize_email(email) if email is not None else None
        before = decode_id_cursor(cursor) if cursor is not None else None
        self._check_available("list_role_changes")
        async with self._lock:
            changes = [
                c for c in self._role_changes
                if (clean is None or c.email == clean) and (before is None or c.id < before)
            ]
        changes.sort(key=lambda c: c.id, reverse=True)
        page = changes[:limit]
        next_cursor = encode_id_cursor(page[-1].id) if len(changes) > limit else None
        return RoleChangePage(changes=page, next_cursor=next_cursor)
