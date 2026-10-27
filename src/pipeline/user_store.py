"""MySQL persistence for users, curator requests and role changes, mixed into RunStore.

`users` is the source of truth for who may use the app and with which role
(viewer, curator or admin) and status (pending, active or disabled). Roles
and statuses are app-authoritative: a login refreshes identity fields and
counters but never changes an existing user's role or status. Only
set_role, set_status and an approved curator request change them, and each
change appends a `role_changes` row in the same transaction.

`curator_requests` holds viewer requests for the curator role; the
`pending_email` generated column carries a unique index so an email has at
most one pending request, enforced by the database.

`role_changes` is insert-only: this module never updates or deletes it.

Emails and actors are normalized at every entry point (normalize_identity:
Unicode NFC, stripped, lowercased) and stored in utf8mb4_bin columns, so
MySQL compares them exactly like FakeUserStore does; an accent- or
case-insensitive collation would make `jose@` and `josé@` the same user.
Requires MySQL >= 8.0.19 (INSERT ... AS alias) and >= 8.0.16 (enforced CHECK).

FakeUserStore (src/pipeline/user_store_fake.py) implements the same
UserStore protocol in memory; tests/test_user_store_contract.py runs one
suite against both.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import unicodedata
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Tuple

import pymysql

from src.auth import UserStoreUnavailable

logger = logging.getLogger(__name__)

ROLES = ("viewer", "curator", "admin")
STATUSES = ("pending", "active", "disabled")
REQUEST_STATUSES = ("pending", "approved", "rejected")
ROLE_CHANGE_FIELDS = ("role", "status")

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 500

_ER_DUP_ENTRY = 1062

_CREATE_USERS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS users (
    email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    name VARCHAR(255) NOT NULL,
    picture VARCHAR(2048) NULL,
    domain VARCHAR(255) NOT NULL,
    provider VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL,
    role VARCHAR(16) NOT NULL,
    role_updated_by VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
    role_updated_at DATETIME(6) NULL,
    groups_json JSON NOT NULL,
    claims_json JSON NOT NULL,
    first_login_at DATETIME(6) NOT NULL,
    last_login_at DATETIME(6) NOT NULL,
    login_count INT UNSIGNED NOT NULL DEFAULT 0,
    annotation_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
    last_annotation_at DATETIME(6) NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (email),
    INDEX idx_users_last_login (last_login_at, email),
    INDEX idx_users_role_status (role, status),
    CONSTRAINT chk_users_email_lower CHECK (CAST(email AS BINARY) = CAST(LOWER(email) AS BINARY)),
    CONSTRAINT chk_users_role CHECK (role IN ('viewer', 'curator', 'admin')),
    CONSTRAINT chk_users_status CHECK (status IN ('pending', 'active', 'disabled'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

# pending_email is the email while the request is pending and NULL after it is
# decided; a unique index ignores NULLs, so it allows one pending row per email.
_CREATE_CURATOR_REQUESTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS curator_requests (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    reason TEXT NOT NULL,
    affiliation VARCHAR(255) NOT NULL,
    expertise TEXT NOT NULL,
    orcid VARCHAR(32) NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    reviewer_email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
    decided_at DATETIME(6) NULL,
    review_note TEXT NULL,
    created_at DATETIME(6) NOT NULL,
    pending_email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin
        GENERATED ALWAYS AS (CASE WHEN status = 'pending' THEN email ELSE NULL END) STORED,
    UNIQUE INDEX uq_curator_requests_pending_email (pending_email),
    INDEX idx_curator_requests_email (email, id),
    INDEX idx_curator_requests_status (status, id),
    CONSTRAINT chk_curator_requests_status CHECK (status IN ('pending', 'approved', 'rejected'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

_CREATE_ROLE_CHANGES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS role_changes (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    email VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    field VARCHAR(16) NOT NULL,
    old_value VARCHAR(16) NULL,
    new_value VARCHAR(16) NOT NULL,
    actor VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    reason TEXT NULL,
    request_id BIGINT NULL,
    created_at DATETIME(6) NOT NULL,
    INDEX idx_role_changes_email (email, id),
    INDEX idx_role_changes_request_id (request_id),
    CONSTRAINT chk_role_changes_field CHECK (field IN ('role', 'status'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

_USER_COLUMNS = (
    "email, name, picture, domain, provider, status, role, role_updated_by, role_updated_at, "
    "groups_json, claims_json, first_login_at, last_login_at, login_count, annotation_count, "
    "last_annotation_at, created_at, updated_at"
)
_REQUEST_COLUMNS = (
    "id, email, reason, affiliation, expertise, orcid, status, reviewer_email, decided_at, "
    "review_note, created_at"
)
_ROLE_CHANGE_COLUMNS = (
    "id, email, field, old_value, new_value, actor, reason, request_id, created_at"
)

_INSERT_ROLE_CHANGE_SQL = (
    "INSERT INTO role_changes (email, field, old_value, new_value, actor, reason, request_id, "
    "created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class UserNotFound(LookupError):
    """No user row for the given email."""


class CuratorRequestNotFound(LookupError):
    """No curator request with the given id."""


class CuratorRequestAlreadyDecided(Exception):
    """The curator request was already approved or rejected."""


class DuplicatePendingCuratorRequest(Exception):
    """The email already has a pending curator request."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserRecord:
    email: str
    name: str
    picture: Optional[str]
    domain: str
    provider: str
    status: str
    role: str
    role_updated_by: Optional[str]
    role_updated_at: Optional[datetime]
    groups: List[str]
    claims: Dict[str, Any]
    first_login_at: datetime
    last_login_at: datetime
    login_count: int
    annotation_count: int
    last_annotation_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class CuratorRequest:
    id: int
    email: str
    reason: str
    affiliation: str
    expertise: str
    orcid: Optional[str]
    status: str
    reviewer_email: Optional[str]
    decided_at: Optional[datetime]
    review_note: Optional[str]
    created_at: datetime


@dataclass(frozen=True)
class RoleChange:
    id: int
    email: str
    field: str
    old_value: Optional[str]
    new_value: str
    actor: str
    reason: Optional[str]
    request_id: Optional[int]
    created_at: datetime


@dataclass(frozen=True)
class UserFilters:
    role: Optional[str] = None
    status: Optional[str] = None
    # Case-insensitive substring of email or name.
    search: Optional[str] = None


@dataclass(frozen=True)
class UserPage:
    users: List[UserRecord]
    # Pass back as `cursor` for the next page; None on the last page.
    next_cursor: Optional[str]


@dataclass(frozen=True)
class CuratorRequestPage:
    requests: List[CuratorRequest]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class RoleChangePage:
    changes: List[RoleChange]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class LoginIdentity:
    """Identity fields from the IdP, refreshed on every login."""

    email: str
    name: str
    picture: Optional[str] = None
    domain: Optional[str] = None
    provider: str = "google"
    groups: List[str] = field(default_factory=list)
    claims: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class UserStore(Protocol):
    """Users, curator requests and role changes. Implemented by RunStore
    (UserStoreMixin, MySQL) and FakeUserStore (in memory).

    Storage failures raise auth.UserStoreUnavailable; callers fail closed."""

    async def get_user(self, email: str) -> Optional[UserRecord]: ...

    async def upsert_login(
        self,
        identity: LoginIdentity,
        *,
        default_role: str,
        default_status: str,
        at: Optional[datetime] = None,
    ) -> UserRecord: ...

    async def set_role(
        self, email: str, role: str, *, actor: str, reason: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> Optional[RoleChange]: ...

    async def set_status(
        self, email: str, status: str, *, actor: str, reason: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> Optional[RoleChange]: ...

    async def incr_activity(self, email: str, n: int = 1, at: Optional[datetime] = None) -> bool: ...

    async def list_users(
        self, filters: Optional[UserFilters] = None, limit: int = DEFAULT_PAGE_LIMIT,
        cursor: Optional[str] = None,
    ) -> UserPage: ...

    async def count_admins(self, *, active_only: bool = True) -> int: ...

    async def create_curator_request(
        self, email: str, *, reason: str, affiliation: str, expertise: str,
        orcid: Optional[str] = None,
    ) -> CuratorRequest: ...

    async def get_pending_request(self, email: str) -> Optional[CuratorRequest]: ...

    async def get_curator_request(self, request_id: int) -> Optional[CuratorRequest]: ...

    async def list_requests(
        self, *, status: Optional[str] = None, email: Optional[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT, cursor: Optional[str] = None,
    ) -> CuratorRequestPage: ...

    async def decide_request(
        self, request_id: int, approve: bool, reviewer: str, note: Optional[str] = None
    ) -> CuratorRequest: ...

    async def list_role_changes(
        self, *, email: Optional[str] = None, limit: int = DEFAULT_PAGE_LIMIT,
        cursor: Optional[str] = None,
    ) -> RoleChangePage: ...


# ---------------------------------------------------------------------------
# Shared helpers (used by both implementations so they behave identically)
# ---------------------------------------------------------------------------


def normalize_identity(value: str) -> str:
    """Canonical form of an email or actor: Unicode NFC, stripped, lowercased.

    Every email/actor entering either store goes through this, and MySQL
    compares the result byte for byte (utf8mb4_bin), so two addresses are the
    same user only when their canonical forms are identical: `jose@` and
    `josé@` stay distinct, while a decomposed `jose\u0301@` is `josé@`."""
    return unicodedata.normalize("NFC", value or "").strip().lower()


def normalize_email(email: str) -> str:
    clean = normalize_identity(email)
    if not clean or "@" not in clean:
        raise ValueError(f"Invalid email: {email!r}")
    return clean


def normalize_actor(actor: str) -> str:
    """An actor/reviewer: usually an email, but service actors (e.g. a
    migration) need not contain '@'."""
    clean = normalize_identity(actor)
    if not clean:
        raise ValueError("actor must not be empty")
    return clean


def normalize_name(name: Optional[str]) -> str:
    return unicodedata.normalize("NFC", name or "").strip()


def _json_round_trip(value: Any, what: str) -> Any:
    try:
        return json.loads(json.dumps(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{what} must be JSON-serializable") from exc


def normalize_groups(groups: Any) -> List[Any]:
    """Groups as MySQL's JSON column returns them (tuples become lists)."""
    return _json_round_trip(list(groups or []), "groups")


def normalize_claims(claims: Any) -> Dict[str, Any]:
    """Claims as MySQL's JSON column returns them; non-JSON values are rejected."""
    return _json_round_trip(dict(claims or {}), "claims")


def check_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"Unknown role {role!r}; expected one of {ROLES}")
    return role


def check_status(status: str) -> str:
    if status not in STATUSES:
        raise ValueError(f"Unknown status {status!r}; expected one of {STATUSES}")
    return status


def check_limit(limit: int) -> int:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    return min(limit, MAX_PAGE_LIMIT)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc(value: datetime) -> datetime:
    """An aware UTC datetime (naive values are taken as UTC)."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_db(value: Optional[datetime]) -> Optional[datetime]:
    return None if value is None else to_utc(value).replace(tzinfo=None)


def _from_db(value: Optional[datetime]) -> Optional[datetime]:
    return None if value is None else value.replace(tzinfo=timezone.utc)


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _unb64(cursor: str) -> str:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        return base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError("Invalid cursor") from exc


def encode_user_cursor(last_login_at: datetime, email: str) -> str:
    return _b64(f"{to_utc(last_login_at).isoformat()}|{email}")


def decode_user_cursor(cursor: str) -> Tuple[datetime, str]:
    """Inverse of encode_user_cursor; ValueError for anything it didn't produce."""
    raw = _unb64(cursor)
    at_text, sep, email = raw.partition("|")
    if not sep or not email:
        raise ValueError("Invalid cursor")
    try:
        return to_utc(datetime.fromisoformat(at_text)), email
    except ValueError as exc:
        raise ValueError("Invalid cursor") from exc


def encode_id_cursor(row_id: int) -> str:
    return _b64(str(row_id))


def decode_id_cursor(cursor: str) -> int:
    raw = _unb64(cursor)
    if not raw.isdigit():
        raise ValueError("Invalid cursor")
    return int(raw)


def escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _loads(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _user_from_row(row) -> UserRecord:
    return UserRecord(
        email=row[0],
        name=row[1],
        picture=row[2],
        domain=row[3],
        provider=row[4],
        status=row[5],
        role=row[6],
        role_updated_by=row[7],
        role_updated_at=_from_db(row[8]),
        groups=list(_loads(row[9]) or []),
        claims=dict(_loads(row[10]) or {}),
        first_login_at=_from_db(row[11]),
        last_login_at=_from_db(row[12]),
        login_count=int(row[13]),
        annotation_count=int(row[14]),
        last_annotation_at=_from_db(row[15]),
        created_at=_from_db(row[16]),
        updated_at=_from_db(row[17]),
    )


def _request_from_row(row) -> CuratorRequest:
    return CuratorRequest(
        id=int(row[0]),
        email=row[1],
        reason=row[2],
        affiliation=row[3],
        expertise=row[4],
        orcid=row[5],
        status=row[6],
        reviewer_email=row[7],
        decided_at=_from_db(row[8]),
        review_note=row[9],
        created_at=_from_db(row[10]),
    )


def _role_change_from_row(row) -> RoleChange:
    return RoleChange(
        id=int(row[0]),
        email=row[1],
        field=row[2],
        old_value=row[3],
        new_value=row[4],
        actor=row[5],
        reason=row[6],
        request_id=None if row[7] is None else int(row[7]),
        created_at=_from_db(row[8]),
    )


# Errors that mean MySQL couldn't be reached or the statement failed; domain
# errors (UserNotFound, ...) and ValueError are raised before or around SQL
# and pass through untouched.
_STORE_ERRORS = (pymysql.err.MySQLError, OSError, asyncio.TimeoutError)


@asynccontextmanager
async def _store_errors(what: str) -> AsyncIterator[None]:
    try:
        yield
    except _STORE_ERRORS as exc:
        logger.warning("User store %s failed: %s", what, exc)
        raise UserStoreUnavailable(f"User store {what} failed") from exc


# ---------------------------------------------------------------------------
# MySQL implementation
# ---------------------------------------------------------------------------


class UserStoreMixin:
    """User store methods for RunStore (expects `self._pool`, autocommit on)."""

    _pool: Any

    async def _ensure_user_schema(self, cursor) -> None:
        await cursor.execute(_CREATE_USERS_TABLE_SQL)
        await cursor.execute(_CREATE_CURATOR_REQUESTS_TABLE_SQL)
        await cursor.execute(_CREATE_ROLE_CHANGES_TABLE_SQL)

    @asynccontextmanager
    async def _user_transaction(self) -> AsyncIterator[Any]:
        """A cursor inside BEGIN/COMMIT on one pooled connection; rolls back
        on any exception (domain errors included) and re-raises it.

        A cancelled statement makes aiomysql close the connection, so the
        rollback then fails too. That failure must not replace the original
        exception (a CancelledError has to stay one), and the connection is
        closed so the pool discards it instead of reusing it."""
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    yield cursor
                await conn.commit()
            except BaseException:
                try:
                    await conn.rollback()
                except BaseException as rollback_exc:
                    logger.warning("User store rollback failed; discarding connection: %r", rollback_exc)
                    conn.close()
                    if not isinstance(rollback_exc, Exception):
                        raise
                raise

    async def _fetch_user(self, cursor, email: str, *, for_update: bool = False) -> Optional[UserRecord]:
        lock = " FOR UPDATE" if for_update else ""
        await cursor.execute(f"SELECT {_USER_COLUMNS} FROM users WHERE email = %s{lock}", (email,))
        row = await cursor.fetchone()
        return _user_from_row(row) if row else None

    async def get_user(self, email: str) -> Optional[UserRecord]:
        clean = normalize_email(email)
        async with _store_errors("get_user"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    return await self._fetch_user(cursor, clean)

    async def upsert_login(
        self,
        identity: LoginIdentity,
        *,
        default_role: str,
        default_status: str,
        at: Optional[datetime] = None,
    ) -> UserRecord:
        """Insert a new user with default_role/default_status, or refresh an
        existing user's identity fields, groups, claims and login counters.

        One INSERT ... ON DUPLICATE KEY UPDATE: concurrent first logins can't
        both insert, and the update clause never touches role or status."""
        email = normalize_email(identity.email)
        check_role(default_role)
        check_status(default_status)
        now = _to_db(at or utc_now())
        domain = identity.domain or email.split("@")[-1]
        name = normalize_name(identity.name) or email
        groups = normalize_groups(identity.groups)
        claims = normalize_claims(identity.claims)
        params = (
            email, name, identity.picture or None, domain, identity.provider,
            default_status, default_role, json.dumps(groups), json.dumps(claims), now, now, now, now,
        )
        async with _store_errors("upsert_login"):
            async with self._user_transaction() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO users (
                        email, name, picture, domain, provider, status, role,
                        groups_json, claims_json, first_login_at, last_login_at, login_count,
                        created_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s, %s) AS new
                    ON DUPLICATE KEY UPDATE
                        name = IF(new.name = new.email, users.name, new.name),
                        picture = COALESCE(new.picture, users.picture),
                        domain = new.domain,
                        provider = new.provider,
                        groups_json = new.groups_json,
                        claims_json = new.claims_json,
                        last_login_at = GREATEST(users.last_login_at, new.last_login_at),
                        login_count = users.login_count + 1,
                        updated_at = new.updated_at
                    """,
                    params,
                )
                user = await self._fetch_user(cursor, email)
        if user is None:
            raise UserStoreUnavailable(f"User {email} missing right after upsert")
        return user

    async def _set_user_field(
        self,
        field_name: str,
        email: str,
        value: str,
        *,
        actor: str,
        reason: Optional[str],
        request_id: Optional[int],
    ) -> Optional[RoleChange]:
        clean = normalize_email(email)
        actor = normalize_actor(actor)
        now = _to_db(utc_now())
        async with _store_errors(f"set_{field_name}"):
            async with self._user_transaction() as cursor:
                return await self._apply_user_field(
                    cursor, field_name, clean, value, actor=actor, reason=reason,
                    request_id=request_id, now=now,
                )

    async def _apply_user_field(
        self,
        cursor,
        field_name: str,
        email: str,
        value: str,
        *,
        actor: str,
        reason: Optional[str],
        request_id: Optional[int],
        now: datetime,
    ) -> Optional[RoleChange]:
        """Inside a transaction: change users.<field_name> and append the
        role_changes row. Returns None (and writes nothing) when unchanged."""
        user = await self._fetch_user(cursor, email, for_update=True)
        if user is None:
            raise UserNotFound(email)
        old_value = getattr(user, field_name)
        if old_value == value:
            return None
        if field_name == "role":
            await cursor.execute(
                "UPDATE users SET role = %s, role_updated_by = %s, role_updated_at = %s, "
                "updated_at = %s WHERE email = %s",
                (value, actor, now, now, email),
            )
        else:
            await cursor.execute(
                "UPDATE users SET status = %s, updated_at = %s WHERE email = %s",
                (value, now, email),
            )
        await cursor.execute(
            _INSERT_ROLE_CHANGE_SQL,
            (email, field_name, old_value, value, actor, reason, request_id, now),
        )
        return RoleChange(
            id=int(cursor.lastrowid),
            email=email,
            field=field_name,
            old_value=old_value,
            new_value=value,
            actor=actor,
            reason=reason,
            request_id=request_id,
            created_at=_from_db(now),
        )

    async def set_role(
        self, email: str, role: str, *, actor: str, reason: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> Optional[RoleChange]:
        """Set a user's role and audit it atomically; None when unchanged.
        Raises UserNotFound for an unknown email."""
        return await self._set_user_field(
            "role", email, check_role(role), actor=actor, reason=reason, request_id=request_id
        )

    async def set_status(
        self, email: str, status: str, *, actor: str, reason: Optional[str] = None,
        request_id: Optional[int] = None,
    ) -> Optional[RoleChange]:
        """Set a user's status and audit it atomically; None when unchanged.
        Raises UserNotFound for an unknown email."""
        return await self._set_user_field(
            "status", email, check_status(status), actor=actor, reason=reason,
            request_id=request_id,
        )

    async def incr_activity(self, email: str, n: int = 1, at: Optional[datetime] = None) -> bool:
        """Add n (>= 1) annotations to a user's counter in one UPDATE; False
        when the user doesn't exist. Doesn't touch updated_at (profile changes only)."""
        clean = normalize_email(email)
        if n < 1:
            raise ValueError("n must be at least 1")
        when = _to_db(at or utc_now())
        async with _store_errors("incr_activity"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    # n >= 1 always changes the row, so rowcount is 1 iff it exists.
                    await cursor.execute(
                        "UPDATE users SET annotation_count = annotation_count + %s, "
                        "last_annotation_at = GREATEST(COALESCE(last_annotation_at, %s), %s) "
                        "WHERE email = %s",
                        (n, when, when, clean),
                    )
                    return cursor.rowcount == 1

    async def list_users(
        self, filters: Optional[UserFilters] = None, limit: int = DEFAULT_PAGE_LIMIT,
        cursor: Optional[str] = None,
    ) -> UserPage:
        """Users by most recent login first (ties by email, descending)."""
        filters = filters or UserFilters()
        limit = check_limit(limit)
        where: List[str] = []
        params: List[Any] = []
        if filters.role is not None:
            where.append("role = %s")
            params.append(check_role(filters.role))
        if filters.status is not None:
            where.append("status = %s")
            params.append(check_status(filters.status))
        if filters.search:
            # Exact (binary) matching, like FakeUserStore's substring test.
            pattern = f"%{escape_like(normalize_identity(filters.search))}%"
            where.append("(email LIKE %s OR LOWER(name) COLLATE utf8mb4_bin LIKE %s)")
            params.extend([pattern, pattern])
        if cursor is not None:
            after_at, after_email = decode_user_cursor(cursor)
            where.append("(last_login_at < %s OR (last_login_at = %s AND email < %s))")
            params.extend([_to_db(after_at), _to_db(after_at), after_email])
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        params.append(limit + 1)
        async with _store_errors("list_users"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT {_USER_COLUMNS} FROM users {clause}"
                        "ORDER BY last_login_at DESC, email DESC LIMIT %s",
                        tuple(params),
                    )
                    rows = await cur.fetchall()
        users = [_user_from_row(row) for row in rows[:limit]]
        next_cursor = (
            encode_user_cursor(users[-1].last_login_at, users[-1].email)
            if len(rows) > limit else None
        )
        return UserPage(users=users, next_cursor=next_cursor)

    async def count_admins(self, *, active_only: bool = True) -> int:
        sql = "SELECT COUNT(*) FROM users WHERE role = 'admin'"
        if active_only:
            sql += " AND status = 'active'"
        async with _store_errors("count_admins"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(sql)
                    row = await cursor.fetchone()
        return int(row[0])

    async def create_curator_request(
        self, email: str, *, reason: str, affiliation: str, expertise: str,
        orcid: Optional[str] = None,
    ) -> CuratorRequest:
        """Record a pending request. Raises DuplicatePendingCuratorRequest when
        the email already has one (the unique index decides, so it's race-safe)."""
        clean = normalize_email(email)
        now = _to_db(utc_now())
        async with _store_errors("create_curator_request"):
            try:
                async with self._pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute(
                            "INSERT INTO curator_requests "
                            "(email, reason, affiliation, expertise, orcid, status, created_at) "
                            "VALUES (%s, %s, %s, %s, %s, 'pending', %s)",
                            (clean, reason, affiliation, expertise, orcid or None, now),
                        )
                        request_id = int(cursor.lastrowid)
            except pymysql.err.IntegrityError as exc:
                if exc.args and exc.args[0] == _ER_DUP_ENTRY:
                    raise DuplicatePendingCuratorRequest(clean) from exc
                raise
        return CuratorRequest(
            id=request_id, email=clean, reason=reason, affiliation=affiliation,
            expertise=expertise, orcid=orcid or None, status="pending", reviewer_email=None,
            decided_at=None, review_note=None, created_at=_from_db(now),
        )

    async def _fetch_request(self, cursor, request_id: int, *, for_update: bool = False):
        lock = " FOR UPDATE" if for_update else ""
        await cursor.execute(
            f"SELECT {_REQUEST_COLUMNS} FROM curator_requests WHERE id = %s{lock}", (request_id,)
        )
        row = await cursor.fetchone()
        return _request_from_row(row) if row else None

    async def get_curator_request(self, request_id: int) -> Optional[CuratorRequest]:
        async with _store_errors("get_curator_request"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    return await self._fetch_request(cursor, request_id)

    async def get_pending_request(self, email: str) -> Optional[CuratorRequest]:
        clean = normalize_email(email)
        async with _store_errors("get_pending_request"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT {_REQUEST_COLUMNS} FROM curator_requests WHERE pending_email = %s",
                        (clean,),
                    )
                    row = await cursor.fetchone()
        return _request_from_row(row) if row else None

    async def list_requests(
        self, *, status: Optional[str] = None, email: Optional[str] = None,
        limit: int = DEFAULT_PAGE_LIMIT, cursor: Optional[str] = None,
    ) -> CuratorRequestPage:
        """Requests newest first."""
        limit = check_limit(limit)
        where: List[str] = []
        params: List[Any] = []
        if status is not None:
            if status not in REQUEST_STATUSES:
                raise ValueError(f"Unknown request status {status!r}")
            where.append("status = %s")
            params.append(status)
        if email is not None:
            where.append("email = %s")
            params.append(normalize_email(email))
        if cursor is not None:
            where.append("id < %s")
            params.append(decode_id_cursor(cursor))
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        params.append(limit + 1)
        async with _store_errors("list_requests"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT {_REQUEST_COLUMNS} FROM curator_requests {clause}"
                        "ORDER BY id DESC LIMIT %s",
                        tuple(params),
                    )
                    rows = await cur.fetchall()
        requests = [_request_from_row(row) for row in rows[:limit]]
        next_cursor = encode_id_cursor(requests[-1].id) if len(rows) > limit else None
        return CuratorRequestPage(requests=requests, next_cursor=next_cursor)

    async def decide_request(
        self, request_id: int, approve: bool, reviewer: str, note: Optional[str] = None
    ) -> CuratorRequest:
        """Approve or reject a pending request in one transaction.

        Approval makes a viewer a curator with a role_changes row linked to
        the request; a curator or admin keeps their role (never a demotion).
        Raises CuratorRequestNotFound, CuratorRequestAlreadyDecided, or
        UserNotFound (approval for an email with no user); on any error
        nothing is written."""
        reviewer = normalize_actor(reviewer)
        now = _to_db(utc_now())
        new_status = "approved" if approve else "rejected"
        async with _store_errors("decide_request"):
            async with self._user_transaction() as cursor:
                request = await self._fetch_request(cursor, request_id, for_update=True)
                if request is None:
                    raise CuratorRequestNotFound(request_id)
                if request.status != "pending":
                    raise CuratorRequestAlreadyDecided(request_id)
                await cursor.execute(
                    "UPDATE curator_requests SET status = %s, reviewer_email = %s, "
                    "decided_at = %s, review_note = %s WHERE id = %s",
                    (new_status, reviewer, now, note, request_id),
                )
                if approve:
                    user = await self._fetch_user(cursor, request.email, for_update=True)
                    if user is None:
                        raise UserNotFound(request.email)
                    if user.role == "viewer":
                        await self._apply_user_field(
                            cursor, "role", request.email, "curator", actor=reviewer,
                            reason=note, request_id=request_id, now=now,
                        )
        return CuratorRequest(
            id=request.id, email=request.email, reason=request.reason,
            affiliation=request.affiliation, expertise=request.expertise, orcid=request.orcid,
            status=new_status, reviewer_email=reviewer, decided_at=_from_db(now),
            review_note=note, created_at=request.created_at,
        )

    async def list_role_changes(
        self, *, email: Optional[str] = None, limit: int = DEFAULT_PAGE_LIMIT,
        cursor: Optional[str] = None,
    ) -> RoleChangePage:
        """Audit rows newest first."""
        limit = check_limit(limit)
        where: List[str] = []
        params: List[Any] = []
        if email is not None:
            where.append("email = %s")
            params.append(normalize_email(email))
        if cursor is not None:
            where.append("id < %s")
            params.append(decode_id_cursor(cursor))
        clause = f"WHERE {' AND '.join(where)} " if where else ""
        params.append(limit + 1)
        async with _store_errors("list_role_changes"):
            async with self._pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"SELECT {_ROLE_CHANGE_COLUMNS} FROM role_changes {clause}"
                        "ORDER BY id DESC LIMIT %s",
                        tuple(params),
                    )
                    rows = await cur.fetchall()
        changes = [_role_change_from_row(row) for row in rows[:limit]]
        next_cursor = encode_id_cursor(changes[-1].id) if len(rows) > limit else None
        return RoleChangePage(changes=changes, next_cursor=next_cursor)
