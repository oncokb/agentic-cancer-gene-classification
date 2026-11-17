"""
SSO & Google OAuth authentication, Enterprise SAML 2.0 claims processing,
JIT (Just-In-Time) provisioning, and domain-based access control.
Restricts access to @mskcc.org and @openevidence.com accounts.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
import uuid
import xml.etree.ElementTree as ET
import zlib
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from src.api_keys import (
    api_key_inactive_reason,
    api_key_matches,
    bearer_token,
    check_api_key_rate_limit,
    hash_api_key,
    looks_like_api_key,
    parse_api_key,
    should_touch_last_used,
)
from src.config import settings

if TYPE_CHECKING:  # src.pipeline.user_store imports this module
    from src.pipeline.user_store import UserRecord, UserStore

logger = logging.getLogger(__name__)

# Fallback in-memory secret if AUTH_SECRET_KEY is omitted in non-production environments
_ephemeral_secret: Optional[str] = None
_redis_client = None

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_ENDPOINT = "https://openidconnect.googleapis.com/v1/userinfo"

SAML_NS = {
    "saml": "urn:oasis:names:tc:SAML:2.0:assertion",
    "samlp": "urn:oasis:names:tc:SAML:2.0:protocol",
    "md": "urn:oasis:names:tc:SAML:2.0:metadata",
}


class AuthenticatedUser(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    domain: str
    role: str = "viewer"
    status: str = "active"
    provider: str = "google"
    groups: List[str] = Field(default_factory=list)
    # "session" (cookie / session bearer token) or "api_key" (Authorization: Bearer acgc_...)
    auth_method: str = "session"
    api_key_id: Optional[str] = None
    session_generation: int = 1


class UserProfile(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    domain: str
    role: str = "viewer"
    status: str = "active"
    provider: str = "google"
    groups: List[str] = Field(default_factory=list)
    claims: Dict[str, Any] = Field(default_factory=dict)
    first_login_at: str
    last_login_at: str
    login_count: int = 1
    annotation_count: int = 0
    last_annotation_at: Optional[str] = None
    session_generation: int = 1


class AuthUserResponse(BaseModel):
    email: str
    name: str
    picture: Optional[str] = None
    domain: str
    role: str = "viewer"
    status: str = "active"
    provider: str = "google"
    groups: List[str] = Field(default_factory=list)
    first_login_at: Optional[str] = None
    last_login_at: Optional[str] = None
    login_count: int = 1
    annotation_count: int = 0
    last_annotation_at: Optional[str] = None


class AuthMeResponse(BaseModel):
    auth_enabled: bool
    authenticated: bool
    user: Optional[AuthUserResponse] = None
    allowed_domains: List[str] = Field(default_factory=list)
    saml_enabled: bool = False
    jit_provisioning_enabled: bool = True
    dev_login_enabled: bool = False
    keycloak_enabled: bool = False
    # Whether the current user may edit curated overrides (see require_curator).
    can_curate: bool = False


class SAMLAssertionData(BaseModel):
    name_id: str
    attributes: Dict[str, List[str]] = Field(default_factory=dict)
    groups: List[str] = Field(default_factory=list)
    display_name: str = ""
    issuer: str = ""
    session_index: Optional[str] = None


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------

# Roles allowed to edit curated overrides. Admins are included so the
# curation flow can be exercised without a separate curator account. Roles are
# granted on /admin/users (recorded in role_changes); sign-in never derives a
# role from IdP groups.
CURATION_ROLES = frozenset({"curator", "admin"})


def can_curate(user: Optional["AuthenticatedUser"]) -> bool:
    """True when `user` may edit curated overrides (always, with auth disabled).

    `user` must carry the CURRENT role and status, as require_auth returns
    them (re-read from the user store on every request)."""
    if not settings.auth_enabled:
        return True
    return bool(user) and user.status == "active" and user.role in CURATION_ROLES


# ---------------------------------------------------------------------------
# JIT User Store (Redis + In-Memory Fallback)
# ---------------------------------------------------------------------------

_user_store: Dict[str, UserProfile] = {}
# Usage counters kept beside (not in) the profile; see record_user_annotation_activity.
_user_activity: Dict[str, Dict[str, Any]] = {}
_user_store_lock = asyncio.Lock()


async def _get_auth_redis_client():
    global _redis_client
    redis_url = settings.redis_url.strip()
    if not redis_url:
        return None
    if _redis_client is None:
        try:
            import redis.asyncio as redis
            _redis_client = redis.from_url(redis_url, decode_responses=True)
        except (ImportError, ConnectionError, TimeoutError, OSError, ValueError, RuntimeError) as exc:
            logger.debug("Failed to initialize Redis auth client: %s", exc)
            return None
    return _redis_client


class UserStoreUnavailable(RuntimeError):
    """The user profile store could not be read; callers must fail closed.

    Messages never name a user: they're logged and shown to operators."""


def describe_store_error(exc: BaseException) -> str:
    """A log-safe description of a user-store failure: exception classes and
    MySQL error codes along the cause chain, never a driver's message or args
    (they can quote row data such as emails). Messages of our own
    UserStoreUnavailable / UserStoreStartupError are kept: they never name a user."""
    parts = []
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, (UserStoreUnavailable, UserStoreStartupError)):
            parts.append(f"{type(current).__name__}: {current}")
        else:
            code = current.args[0] if current.args and isinstance(current.args[0], int) else None
            parts.append(type(current).__name__ + (f" (MySQL error {code})" if code is not None else ""))
        current = current.__cause__ or current.__context__
    return " <- ".join(parts)


# ---------------------------------------------------------------------------
# MySQL user store (USER_STORE_BACKEND=mysql)
# ---------------------------------------------------------------------------

# New users signing in with the MySQL store; only admin actions and bootstrap
# grants change a role after that.
MYSQL_NEW_USER_ROLE = "viewer"
BOOTSTRAP_ADMIN_REASON = "bootstrap admin grant (BOOTSTRAP_ADMIN_EMAILS)"
# Logged when a usage counter write to the MySQL user store fails.
ACTIVITY_WRITE_FAILED_LOG_KEY = "user_store.activity_write_failed"

_mysql_user_store: Optional["UserStore"] = None


def mysql_user_store_enabled() -> bool:
    return settings.user_store_backend == "mysql"


def configure_mysql_user_store(store: Optional["UserStore"]) -> None:
    """Set the store used when USER_STORE_BACKEND=mysql (the app's RunStore)."""
    global _mysql_user_store
    _mysql_user_store = store


def _mysql_store() -> "UserStore":
    if _mysql_user_store is None:
        raise UserStoreUnavailable("USER_STORE_BACKEND=mysql but no MySQL user store is configured")
    return _mysql_user_store


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _profile_from_record(record: "UserRecord") -> UserProfile:
    return UserProfile(
        email=record.email,
        name=record.name,
        picture=record.picture,
        domain=record.domain,
        role=record.role,
        status=record.status,
        provider=record.provider,
        groups=[str(g) for g in record.groups],
        claims=dict(record.claims),
        first_login_at=_iso(record.first_login_at),
        last_login_at=_iso(record.last_login_at),
        login_count=record.login_count,
        annotation_count=record.annotation_count,
        last_annotation_at=_iso(record.last_annotation_at),
        session_generation=record.session_generation,
    )


async def _mysql_get_user(email: str) -> Optional["UserRecord"]:
    """One users-table read, failing closed.

    The email is validated first: a malformed one can't name a user (None,
    so 401). After that, any exception from the store, including a
    ValueError from decoding a stored row, is UserStoreUnavailable (503).
    """
    from src.pipeline.user_store import normalize_email

    try:
        clean_email = normalize_email(email)
    except ValueError:
        return None
    store = _mysql_store()
    try:
        return await store.get_user(clean_email)
    except UserStoreUnavailable:
        raise
    except Exception as exc:
        raise UserStoreUnavailable("User store get_user failed") from exc


async def grant_bootstrap_admin_if_listed(email: str) -> Optional[str]:
    """Grant the admin role once to a BOOTSTRAP_ADMIN_EMAILS user (MySQL store).

    grant_bootstrap_admin logs the grant in role_changes under the same
    actor as the migration script, and refuses when any earlier grant was
    logged, so an admin's later demotion is never undone. Returns the store's
    outcome, or None when the email isn't listed.
    """
    clean_email = email.strip().lower()
    if clean_email not in settings.bootstrap_admin_emails_list:
        return None
    outcome = await _mysql_store().grant_bootstrap_admin(clean_email, reason=BOOTSTRAP_ADMIN_REASON)
    logger.info("Bootstrap admin for %s: %s", clean_email, outcome)
    return outcome


class UserStoreStartupError(RuntimeError):
    """The MySQL user store isn't usable at startup; the app must not serve.

    Raised without its cause: the server prints a startup failure's whole
    traceback, and a driver's message there can quote row data (emails). The
    message carries the redacted cause (describe_store_error) instead."""


async def bootstrap_admins_at_startup() -> None:
    """Check the MySQL user store and apply BOOTSTRAP_ADMIN_EMAILS to listed
    users already in it. Raises UserStoreStartupError, so startup fails, when
    the store can't be read or a grant fails: serving would leave the operator
    without the admins they configured. Listed users who haven't signed in
    yet are skipped and granted at their first sign-in.
    """
    from src.pipeline.user_store import UserNotFound

    store = _mysql_store()
    try:
        admins = await store.count_admins()
    except Exception as exc:
        raise UserStoreStartupError(
            "USER_STORE_BACKEND=mysql but the MySQL user store can't be read at startup; "
            f"refusing to start ({describe_store_error(exc)})"
        ) from None
    logger.info("MySQL user store ready: %d active admin(s)", admins)
    for position, email in enumerate(settings.bootstrap_admin_emails_list, start=1):
        try:
            await grant_bootstrap_admin_if_listed(email)
        except UserNotFound:
            logger.info("Bootstrap admin %s has not signed in yet; granting at first sign-in", email)
        except Exception as exc:
            raise UserStoreStartupError(
                f"Granting the bootstrap admin role to BOOTSTRAP_ADMIN_EMAILS entry {position} "
                f"failed; refusing to start ({describe_store_error(exc)})"
            ) from None


async def _profile_store_redis():
    """The Redis client backing authoritative profile reads.

    Returns None only when Redis is intentionally unconfigured (REDIS_URL
    empty), in which case the in-process store is the profile store. Raises
    UserStoreUnavailable when Redis is configured but the client failed to
    initialize, rather than silently degrading to the in-process copy.
    """
    if not settings.redis_url.strip():
        return None
    redis_client = await _get_auth_redis_client()
    if redis_client is None:
        raise UserStoreUnavailable("Redis is configured but the auth client could not be initialized")
    return redis_client


async def load_user_profile_strict(email: str) -> Optional[UserProfile]:
    """Read a user profile from the authoritative store, failing closed.

    With USER_STORE_BACKEND=mysql the users table alone decides, the same
    way. With Redis configured, Redis alone decides: a miss means the profile
    was deleted (None) and any error raises UserStoreUnavailable. The in-process
    store, which save_user_profile also populates, may hold a stale copy and
    is consulted only when Redis is intentionally unconfigured.
    """
    clean_email = email.strip().lower()
    if mysql_user_store_enabled():
        record = await _mysql_get_user(clean_email)
        return _profile_from_record(record) if record is not None else None
    redis_client = await _profile_store_redis()
    if redis_client is None:
        async with _user_store_lock:
            return _user_store.get(clean_email)
    try:
        payload = await redis_client.get(f"agcg:user:{clean_email}")
        return UserProfile.model_validate_json(payload) if payload else None
    except Exception as exc:
        raise UserStoreUnavailable("Failed to read a user profile from Redis") from exc


async def get_user_profile(email: str) -> Optional[UserProfile]:
    if mysql_user_store_enabled():
        return await load_user_profile_strict(email)
    clean_email = email.strip().lower()
    redis_client = await _get_auth_redis_client()
    if redis_client is not None:
        try:
            payload = await redis_client.get(f"agcg:user:{clean_email}")
            if payload:
                return UserProfile.model_validate_json(payload)
        except Exception as exc:
            logger.debug("Redis error reading user %s: %s", clean_email, exc)

    async with _user_store_lock:
        return _user_store.get(clean_email)


async def save_user_profile(profile: UserProfile) -> None:
    clean_email = profile.email.strip().lower()
    redis_client = await _get_auth_redis_client()
    if redis_client is not None:
        try:
            await redis_client.set(f"agcg:user:{clean_email}", profile.model_dump_json())
            await redis_client.sadd("agcg:users", clean_email)
        except Exception as exc:
            logger.debug("Redis error saving user %s: %s", clean_email, exc)

    async with _user_store_lock:
        _user_store[clean_email] = profile


def _with_activity(profile: UserProfile, activity: Dict[str, Any]) -> UserProfile:
    """Overlay the separately stored usage counters on a profile.

    A profile's own annotation_count/last_annotation_at are the totals from
    before usage moved to its own record; new usage only lands in `activity`.
    """
    if not activity:
        return profile
    return profile.model_copy(
        update={
            "annotation_count": profile.annotation_count + int(activity.get("annotation_count") or 0),
            "last_annotation_at": activity.get("last_annotation_at") or profile.last_annotation_at,
        }
    )


async def _list_mysql_user_profiles() -> List[UserProfile]:
    from src.pipeline.user_store import MAX_PAGE_LIMIT, UserFilters

    store = _mysql_store()
    profiles: List[UserProfile] = []
    cursor: Optional[str] = None
    try:
        while True:
            page = await store.list_users(UserFilters(), limit=MAX_PAGE_LIMIT, cursor=cursor)
            profiles.extend(_profile_from_record(record) for record in page.users)
            cursor = page.next_cursor
            if cursor is None:
                return profiles  # already most recent login first
    except UserStoreUnavailable:
        raise
    except Exception as exc:
        raise UserStoreUnavailable("Failed to list users") from exc


async def list_user_profiles() -> List[UserProfile]:
    if mysql_user_store_enabled():
        return await _list_mysql_user_profiles()
    redis_client = await _get_auth_redis_client()
    if redis_client is not None:
        try:
            emails = await redis_client.smembers("agcg:users")
            users = []
            for email in emails:
                u = await get_user_profile(email)
                if u:
                    users.append(_with_activity(u, await redis_client.hgetall(_activity_key(u.email))))
            if users:
                users.sort(key=lambda x: x.last_login_at, reverse=True)
                return users
        except Exception as exc:
            logger.debug("Redis error listing users: %s", exc)

    async with _user_store_lock:
        users = [_with_activity(u, _user_activity.get(u.email, {})) for u in _user_store.values()]
        users.sort(key=lambda x: x.last_login_at, reverse=True)
        return users


async def provision_or_update_user(
    email: str,
    name: str,
    picture: Optional[str] = None,
    domain: Optional[str] = None,
    provider: str = "google",
    groups: Optional[List[str]] = None,
    claims: Optional[Dict[str, Any]] = None,
    role: Optional[str] = None,
    require_approval: Optional[bool] = None,
) -> UserProfile:
    """
    Just-In-Time (JIT) provisioning: creates a new user profile on first login,
    or updates login timestamp, groups, and claims for returning users.

    Reads the profile from the authoritative store (load_user_profile_strict):
    with Redis configured, a profile missing there is a new user subject to the
    new-user rules (incl. approval), even if a stale in-process copy exists, and
    a store error raises UserStoreUnavailable instead of provisioning from that
    copy. `require_approval` overrides JIT_REQUIRE_ADMIN_APPROVAL for new users
    (logins admitted without JIT provisioning pass False); returning users
    always get the same merge rules.
    """
    clean_email = email.strip().lower()
    extracted_domain = domain or clean_email.split("@")[-1]
    if mysql_user_store_enabled():
        return await _provision_mysql_user(
            clean_email, name=name, picture=picture, domain=extracted_domain, provider=provider,
            groups=groups or [], claims=claims or {}, require_approval=require_approval,
        )
    now_iso = datetime.now(timezone.utc).isoformat()
    user_groups = groups or []
    user_claims = claims or {}

    # Derive role from SAML/OIDC groups or defaults
    effective_role = role or settings.jit_default_role
    if any(g in settings.saml_admin_groups_list for g in user_groups):
        effective_role = "admin"
    if any(g in settings.keycloak_admin_roles_list for g in user_groups):
        effective_role = "admin"

    existing = await load_user_profile_strict(clean_email)
    if existing is None:
        if require_approval is None:
            require_approval = settings.jit_require_admin_approval
        status_val = "pending" if require_approval else "active"
        logger.info(
            "JIT provisioning new user: %s (domain=%s, role=%s, provider=%s, status=%s, groups=%s)",
            clean_email, extracted_domain, effective_role, provider, status_val, user_groups,
        )
        profile = UserProfile(
            email=clean_email,
            name=name or clean_email,
            picture=picture,
            domain=extracted_domain,
            role=effective_role,
            status=status_val,
            provider=provider,
            groups=user_groups,
            claims=user_claims,
            first_login_at=now_iso,
            last_login_at=now_iso,
            login_count=1,
        )
    else:
        logger.info("JIT sync for returning user %s (login_count=%d)", clean_email, existing.login_count + 1)
        resolved_role = "admin" if (effective_role == "admin" or existing.role == "admin") else existing.role
        merged_groups = list(dict.fromkeys(existing.groups + user_groups))
        profile = existing.model_copy(
            update={
                "name": name or existing.name,
                "picture": picture or existing.picture,
                "last_login_at": now_iso,
                "login_count": existing.login_count + 1,
                "groups": merged_groups,
                "claims": {**existing.claims, **user_claims},
                "role": resolved_role,
            }
        )

    await save_user_profile(profile)
    return profile


async def _provision_mysql_user(
    email: str,
    *,
    name: str,
    picture: Optional[str],
    domain: str,
    provider: str,
    groups: List[str],
    claims: Dict[str, Any],
    require_approval: Optional[bool],
) -> UserProfile:
    """Sign-in against the MySQL users table.

    upsert_login creates a new user as an active viewer (pending when approval
    is required) and, for an existing user, refreshes identity, groups, claims
    and login counters but never role or status: IdP groups (Keycloak roles,
    SAML admin groups) are stored, not mapped to a role. A listed bootstrap
    admin is then granted admin once. Store errors raise UserStoreUnavailable.
    """
    from src.pipeline.user_store import BOOTSTRAP_GRANTED, LoginIdentity

    if require_approval is None:
        require_approval = settings.jit_require_admin_approval
    identity = LoginIdentity(
        email=email, name=name, picture=picture, domain=domain, provider=provider,
        groups=groups, claims=claims,
    )
    store = _mysql_store()
    try:
        record = await store.upsert_login(
            identity,
            default_role=MYSQL_NEW_USER_ROLE,
            default_status="pending" if require_approval else "active",
        )
        logger.info(
            "Sign-in for %s (provider=%s, role=%s, status=%s, login_count=%d)",
            record.email, provider, record.role, record.status, record.login_count,
        )
        if await grant_bootstrap_admin_if_listed(record.email) == BOOTSTRAP_GRANTED:
            record = await store.get_user(record.email) or record
    except UserStoreUnavailable:
        raise
    except Exception as exc:
        raise UserStoreUnavailable("User store sign-in failed") from exc
    return _profile_from_record(record)


def _activity_key(email: str) -> str:
    return f"agcg:user_activity:{email}"


async def record_user_annotation_activity(email: str, count: int = 1) -> None:
    """Records usage metrics upon annotation completion (shown by list_user_profiles).

    Write-only: the counters live beside the profile instead of in it, so
    recording usage neither adds a profile read to the request nor rewrites
    the profile (a read-modify-write here could undo a concurrent demotion).
    """
    clean_email = email.strip().lower()
    if mysql_user_store_enabled():
        # Usage counters aren't an access check, and this runs after the work:
        # on failure the response still succeeds, the count is dropped (never
        # buffered in-process) and an error is logged under a stable key.
        # Only the exception type is logged, never the email.
        try:
            await _mysql_store().incr_activity(clean_email, max(1, count))
        except Exception as exc:
            logger.error(
                "%s: dropped an annotation usage count of %d (%s)",
                ACTIVITY_WRITE_FAILED_LOG_KEY, max(1, count), type(exc).__name__,
            )
        return
    now_iso = datetime.now(timezone.utc).isoformat()
    if settings.redis_url.strip():
        # Redis is the store: on failure drop the increment rather than buffer
        # it in-process, where it would never reach Redis after recovery.
        redis_client = await _get_auth_redis_client()
        if redis_client is None:
            logger.warning("Dropped annotation activity for %s: Redis auth client unavailable", clean_email)
            return
        try:
            pipe = redis_client.pipeline()
            pipe.hincrby(_activity_key(clean_email), "annotation_count", max(1, count))
            pipe.hset(_activity_key(clean_email), "last_annotation_at", now_iso)
            await pipe.execute()
        except Exception as exc:
            logger.warning("Dropped annotation activity for %s: Redis error: %s", clean_email, exc)
        return

    async with _user_store_lock:
        activity = _user_activity.setdefault(clean_email, {})
        activity["annotation_count"] = int(activity.get("annotation_count") or 0) + max(1, count)
        activity["last_annotation_at"] = now_iso


# ---------------------------------------------------------------------------
# Signing & State
# ---------------------------------------------------------------------------

def get_auth_secret_key() -> str:
    global _ephemeral_secret
    configured = settings.auth_secret_key.strip()
    if configured:
        return configured
    if _ephemeral_secret is None:
        _ephemeral_secret = secrets.token_hex(32)
        logger.warning(
            "AUTH_SECRET_KEY not set. Using generated ephemeral secret. "
            "Sessions will expire upon application restart. Configure AUTH_SECRET_KEY in production."
        )
    return _ephemeral_secret


def is_email_allowed(email: str) -> Tuple[bool, str]:
    """
    Validates whether an email is permitted access based on domain whitelist
    (@mskcc.org and @openevidence.com) and optional explicit user allowlist.
    """
    clean_email = email.strip().lower()
    if "@" not in clean_email:
        return False, "Invalid email address format."

    domain = clean_email.split("@")[-1]
    allowed_domains = settings.allowed_domains_list

    if allowed_domains and domain not in allowed_domains:
        allowed_desc = ", ".join(f"@{d}" for d in allowed_domains)
        return (
            False,
            f"Domain '@{domain}' is not authorized. Access is strictly limited to {allowed_desc} accounts.",
        )

    allowed_emails = settings.allowed_emails_list
    if allowed_emails and clean_email not in allowed_emails:
        return (
            False,
            f"User '{clean_email}' is not on the authorized user list. Please request access from an administrator.",
        )

    return True, "Authorized"


def is_saml_group_allowed(groups: List[str]) -> Tuple[bool, str]:
    """
    Checks if SAML/Enterprise claims satisfy the required group constraints.
    """
    allowed_groups = settings.saml_allowed_groups_list
    if not allowed_groups:
        return True, "No group restriction configured"

    matched = set(groups) & set(allowed_groups)
    if not matched:
        return (
            False,
            f"Access requires membership in one of: {', '.join(allowed_groups)}. Your account groups: {', '.join(groups) or 'none'}.",
        )
    return True, f"Authorized via group(s): {', '.join(matched)}"


def create_session_token(user: AuthenticatedUser, ttl_seconds: Optional[int] = None) -> str:
    """
    Creates a cryptographically signed HMAC-SHA256 session token.
    Token format: base64url(payload).base64url(signature)
    """
    ttl = ttl_seconds if ttl_seconds is not None else settings.auth_session_ttl_seconds
    now = int(time.time())
    payload = {
        "email": user.email.strip().lower(),
        "name": user.name,
        "picture": user.picture,
        "domain": user.domain.strip().lower(),
        "role": user.role,
        "status": user.status,
        "provider": user.provider,
        "groups": user.groups,
        "gen": getattr(user, "session_generation", 1),
        "iat": now,
        "exp": now + ttl,
    }
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")

    secret = get_auth_secret_key().encode("utf-8")
    sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    return f"{payload_b64}.{sig_b64}"


def decode_session_token(token: str) -> Optional[AuthenticatedUser]:
    """
    Verifies signature, expiration, and domain authorization of a session token.
    Returns AuthenticatedUser if valid, None otherwise.
    """
    if not token or "." not in token:
        return None

    try:
        payload_b64, sig_b64 = token.split(".", 1)
        secret = get_auth_secret_key().encode("utf-8")

        expected_sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            logger.warning("Session token HMAC verification failed.")
            return None

        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)

        now = int(time.time())
        if payload.get("exp", 0) < now:
            logger.info("Session token expired for %s", payload.get("email"))
            return None

        email = payload.get("email", "").strip().lower()
        allowed, reason = is_email_allowed(email)
        if not allowed:
            logger.warning("Session token rejected for %s: %s", email, reason)
            return None

        # role/status are login-time snapshots, informational only: session
        # requests are authorized with the live profile's (_verify_session).
        return AuthenticatedUser(
            email=email,
            name=payload.get("name") or email,
            picture=payload.get("picture"),
            domain=payload.get("domain") or email.split("@")[-1],
            role=payload.get("role", settings.jit_default_role),
            status=payload.get("status", "active"),
            provider=payload.get("provider", "google"),
            groups=payload.get("groups", []),
            session_generation=int(payload.get("gen", 1)),
        )
    except Exception:
        logger.exception("Failed to decode session token")
        return None


def validate_redirect_to(value: Any) -> str:
    """Accept only local absolute paths, never browser-normalized external URLs."""
    if (
        not isinstance(value, str)
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return "/"
    return value


def create_oauth_state(
    redirect_to: str = "/",
    provider: Optional[str] = None,
    txn: Optional[str] = None,
    nonce: Optional[str] = None,
) -> str:
    """
    Generates a tamper-proof state token for CSRF protection during OAuth / SAML flow.
    """
    now = int(time.time())
    payload = {
        "nonce": nonce or secrets.token_hex(16),
        "redirect_to": validate_redirect_to(redirect_to),
        "iat": now,
        "exp": now + 900,  # 15 minutes
    }
    if txn:
        payload["txn"] = txn
    if provider:
        payload["provider"] = provider
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")

    secret = get_auth_secret_key().encode("utf-8")
    sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    return f"{payload_b64}.{sig_b64}"


def verify_oauth_state(state: str) -> Optional[Dict[str, Any]]:
    """
    Verifies that the state parameter is authentic and unexpired.
    """
    if not state or "." not in state:
        return None

    try:
        payload_b64, sig_b64 = state.split(".", 1)
        secret = get_auth_secret_key().encode("utf-8")

        expected_sig = hmac.new(secret, payload_b64.encode("utf-8"), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        padding = "=" * (-len(payload_b64) % 4)
        payload_json = base64.urlsafe_b64decode(payload_b64 + padding).decode("utf-8")
        payload = json.loads(payload_json)

        if payload.get("exp", 0) < int(time.time()):
            return None

        return payload
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Browser-bound OAuth Transactions & PKCE (F3a / F3b)
# ---------------------------------------------------------------------------

OAUTH_TXN_COOKIE_NAME = "agcg_oauth_txn"
OAUTH_TXN_TTL_SECONDS = 900  # 15 minutes


class OAuthTransactionUnavailable(RuntimeError):
    """Raised when the Redis store backing OAuth transactions is unavailable or errors."""


async def _oauth_txn_redis():
    """Returns the Redis client for single-use OAuth transactions."""
    return await _profile_store_redis()


def generate_pkce_pair() -> Tuple[str, str]:
    """Generates an RFC 7636 PKCE code_verifier and S256 code_challenge."""
    code_verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return code_verifier, code_challenge


def create_oauth_transaction(
    provider: str,
    redirect_to: str = "/",
) -> Tuple[str, str, str, str, str, str]:
    """Creates a new browser-bound OAuth transaction with PKCE.

    Returns:
        (txn_id, state_token, cookie_value, code_verifier, code_challenge, nonce)
    """
    txn_id = secrets.token_urlsafe(32)
    nonce = secrets.token_hex(16)
    code_verifier, code_challenge = generate_pkce_pair()

    state = create_oauth_state(
        redirect_to=validate_redirect_to(redirect_to),
        provider=provider,
        txn=txn_id,
        nonce=nonce,
    )

    now = int(time.time())
    cookie_payload = {
        "txn": txn_id,
        "provider": provider,
        "nonce": nonce,
        "code_verifier": code_verifier,
        "exp": now + OAUTH_TXN_TTL_SECONDS,
    }
    payload_bytes = json.dumps(cookie_payload, separators=(",", ":")).encode("utf-8")
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
    sig_raw = f"{txn_id}.{payload_b64}".encode("utf-8")
    secret = get_auth_secret_key().encode("utf-8")
    sig = hmac.new(secret, sig_raw, hashlib.sha256).digest()
    sig_b64 = base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")
    cookie_value = f"{txn_id}.{payload_b64}.{sig_b64}"

    return txn_id, state, cookie_value, code_verifier, code_challenge, nonce


def verify_oauth_txn_cookie(
    cookie_value: Optional[str],
    *,
    expected_txn: str,
    expected_provider: str,
) -> Optional[Dict[str, Any]]:
    """Verifies the signed transaction cookie (signature, expiry, txn, provider)."""
    if not cookie_value or not isinstance(cookie_value, str):
        return None
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return None

    cookie_txn, payload_b64, sig_b64 = parts
    if not cookie_txn or not payload_b64 or not sig_b64:
        return None
    if not expected_txn or not hmac.compare_digest(cookie_txn, expected_txn):
        return None

    secret = get_auth_secret_key().encode("utf-8")
    sig_raw = f"{cookie_txn}.{payload_b64}".encode("utf-8")
    expected_sig = hmac.new(secret, sig_raw, hashlib.sha256).digest()
    expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).decode("utf-8").rstrip("=")
    if not hmac.compare_digest(sig_b64, expected_sig_b64):
        logger.warning("OAuth transaction cookie HMAC verification failed")
        return None

    try:
        padding_chars = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding_chars).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None

    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp < now:
        logger.warning("OAuth transaction cookie expired")
        return None

    if payload.get("txn") != expected_txn:
        logger.warning("OAuth transaction cookie txn mismatch")
        return None

    if payload.get("provider") != expected_provider:
        logger.warning("OAuth transaction provider mismatch (%s != %s)", payload.get("provider"), expected_provider)
        return None

    if not isinstance(payload.get("nonce"), str) or not payload.get("nonce"):
        return None
    if not isinstance(payload.get("code_verifier"), str) or not payload.get("code_verifier"):
        return None

    return payload


def set_oauth_txn_cookie(response: Response, cookie_value: str, request: Optional[Request] = None) -> None:
    response.set_cookie(
        key=OAUTH_TXN_COOKIE_NAME,
        value=cookie_value,
        max_age=OAUTH_TXN_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=is_cookie_secure(request),
        path="/",
    )


async def save_oauth_transaction(
    txn_id: str,
    provider: str,
    code_verifier: str,
    nonce: str,
) -> None:
    """Stores transaction data in Redis; fails closed when Redis is unavailable or errors."""
    try:
        redis_client = await _oauth_txn_redis()
    except Exception as exc:
        logger.error("Redis unavailable while saving OAuth transaction: %s", type(exc).__name__)
        raise OAuthTransactionUnavailable("OAuth transaction store is unavailable.") from exc
    if redis_client is None:
        logger.error("Refusing OAuth transaction save: Redis is not configured or unavailable.")
        raise OAuthTransactionUnavailable("OAuth transaction store is unavailable.")
    try:
        data = json.dumps({
            "provider": provider,
            "code_verifier": code_verifier,
            "nonce": nonce,
        })
        await redis_client.set(
            f"agcg:oauth_txn:{txn_id}",
            data,
            ex=OAUTH_TXN_TTL_SECONDS,
        )
    except Exception as exc:
        logger.error("Failed to store OAuth transaction in Redis: %s", type(exc).__name__)
        raise OAuthTransactionUnavailable("Failed to store OAuth transaction in Redis.") from exc


async def consume_oauth_transaction(
    txn_id: str,
    cookie_value: Optional[str],
    expected_provider: str,
) -> Optional[Dict[str, Any]]:
    """Verifies the signed transaction cookie first, then atomically consumes the
    transaction in Redis via GETDEL. Fails closed if Redis is unavailable or errors."""
    cookie_payload = verify_oauth_txn_cookie(
        cookie_value,
        expected_txn=txn_id,
        expected_provider=expected_provider,
    )
    if cookie_payload is None:
        return None

    try:
        redis_client = await _oauth_txn_redis()
    except Exception as exc:
        logger.error("Redis unavailable during consume_oauth_transaction: %s", type(exc).__name__)
        raise OAuthTransactionUnavailable("OAuth transaction store is unavailable.") from exc
    if redis_client is None:
        logger.error("Refusing OAuth transaction consume: Redis is not configured or unavailable.")
        raise OAuthTransactionUnavailable("OAuth transaction store is unavailable.")

    try:
        raw = await redis_client.getdel(f"agcg:oauth_txn:{txn_id}")
    except Exception as exc:
        logger.error("Redis error during consume_oauth_transaction: %s", type(exc).__name__)
        raise OAuthTransactionUnavailable("OAuth transaction store error.") from exc

    if not raw:
        return None

    try:
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if data.get("provider") != expected_provider:
        return None
    if data.get("nonce") != cookie_payload.get("nonce"):
        return None
    if data.get("code_verifier") != cookie_payload.get("code_verifier"):
        return None
    return data


def _b64url_decode_bytes(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def verify_google_id_token_nonce(id_token: Any, *, expected_nonce: Optional[str]) -> Dict[str, Any]:
    """Validates Google ID token JWT structure and exact nonce match."""
    if not expected_nonce or not isinstance(expected_nonce, str):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing expected OAuth transaction nonce.",
        )
    if not isinstance(id_token, str) or not id_token.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing Google ID token.",
        )
    parts = id_token.split(".")
    if len(parts) != 3 or not all(parts):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Google ID token.",
        )
    try:
        header = json.loads(_b64url_decode_bytes(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode_bytes(parts[1]).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Google ID token payload.",
        ) from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed Google ID token structure.",
        )
    alg = header.get("alg")
    if not isinstance(alg, str) or not alg.strip() or alg.strip().lower() == "none":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Google ID token algorithm.",
        )
    if "exp" in payload:
        exp = payload["exp"]
        if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp < int(time.time()) - 60:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Expired or invalid Google ID token.",
            )
    id_nonce = payload.get("nonce")
    if not isinstance(id_nonce, str) or not id_nonce:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing nonce claim in Google ID token.",
        )
    if not hmac.compare_digest(id_nonce, expected_nonce):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google ID token nonce mismatch",
        )
    return payload


# ---------------------------------------------------------------------------
# Google OAuth 2.0 / OIDC Helpers
# ---------------------------------------------------------------------------

def get_google_auth_url(
    redirect_uri: str,
    state: str,
    code_challenge: Optional[str] = None,
    nonce: Optional[str] = None,
) -> str:
    params = {
        "client_id": settings.google_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if nonce:
        params["nonce"] = nonce
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}"


async def exchange_google_code(
    code: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> Dict[str, Any]:
    if not settings.google_client_secret or not settings.google_client_secret.strip():
        logger.error("Google OAuth token exchange failed: GOOGLE_CLIENT_SECRET is missing or empty in environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Google OAuth misconfigured: GOOGLE_CLIENT_SECRET is not set in the server environment. "
                "Ensure Kubernetes Secret 'acgc' has GOOGLE_CLIENT_SECRET and the pod has been restarted."
            ),
        )

    data = {
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data=data)
        if resp.status_code != 200:
            logger.error("Failed Google token exchange (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Google authentication failed during token exchange: {resp.text}",
            )
        return resp.json()


async def get_google_user_info(access_token: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(GOOGLE_USERINFO_ENDPOINT, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to fetch Google user info (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to retrieve user profile from Google.",
            )
        return resp.json()


# ---------------------------------------------------------------------------
# Enterprise SAML 2.0 & Claims Helpers
# ---------------------------------------------------------------------------

def build_saml_authn_request(acs_url: str, relay_state: str = "/") -> Tuple[str, str]:
    """
    Builds a standard SAML 2.0 AuthnRequest, deflates and base64 encodes it.
    Returns (request_id, redirect_url).
    """
    request_id = f"id_{uuid.uuid4().hex}"
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    issuer = settings.saml_sp_entity_id.strip() or acs_url
    destination = settings.saml_idp_sso_url.strip()

    xml = (
        f'<samlp:AuthnRequest xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" '
        f'xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
        f'ID="{request_id}" Version="2.0" IssueInstant="{now_iso}" '
        f'Destination="{html.escape(destination)}" '
        f'ProtocolBinding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" '
        f'AssertionConsumerServiceURL="{html.escape(acs_url)}">'
        f'<saml:Issuer>{html.escape(issuer)}</saml:Issuer>'
        f'<samlp:NameIDPolicy Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress" AllowCreate="true"/>'
        f'</samlp:AuthnRequest>'
    )

    deflated = zlib.compress(xml.encode("utf-8"))[2:-4]
    b64_request = base64.b64encode(deflated).decode("utf-8")
    params = {"SAMLRequest": b64_request, "RelayState": validate_redirect_to(relay_state)}
    redirect_url = f"{destination}?{urlencode(params)}"
    return request_id, redirect_url


def parse_saml_response(saml_response_b64: str) -> SAMLAssertionData:
    """
    Decodes and parses a SAML 2.0 Response XML, extracting NameID, attributes, and groups.

    SECURITY NOTE (Finding F1 in acgc-auth-audit):
    This parser extracts assertion elements without verifying XML digital signatures
    (ds:Signature) or Assertion conditions/recipients against an IdP certificate.
    Starting ACGC with SAML_ENABLED=true is refused at application startup (lifespan)
    via saml_startup_error() until a full, library-backed signed assertion verification
    is implemented.
    """
    try:
        xml_bytes = base64.b64decode(saml_response_b64)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Base64 in SAMLResponse") from exc

    try:
        root = ET.fromstring(xml_bytes)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Malformed XML in SAMLResponse") from exc

    # Validate status
    status_elem = root.find(".//samlp:StatusCode", SAML_NS)
    if status_elem is not None:
        status_val = status_elem.attrib.get("Value", "")
        if "Success" not in status_val:
            raise HTTPException(status_code=401, detail=f"SAML Authentication failed at IdP: {status_val}")

    # Extract issuer
    issuer_elem = root.find(".//saml:Issuer", SAML_NS)
    issuer = issuer_elem.text.strip() if issuer_elem is not None and issuer_elem.text else ""

    # Extract NameID / Subject
    name_id_elem = root.find(".//saml:Subject/saml:NameID", SAML_NS)
    name_id = name_id_elem.text.strip() if name_id_elem is not None and name_id_elem.text else ""

    # Extract all AttributeStatement attributes
    attributes: Dict[str, List[str]] = {}
    attr_elems = root.findall(".//saml:AttributeStatement/saml:Attribute", SAML_NS)
    for attr in attr_elems:
        attr_name = attr.attrib.get("Name", "")
        values = []
        for val in attr.findall("saml:AttributeValue", SAML_NS):
            if val.text:
                values.append(val.text.strip())
        if attr_name and values:
            attributes[attr_name] = values

    # Resolve email from claims or NameID
    email = ""
    for email_key in [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/emailaddress",
        "http://schemas.microsoft.com/identity/claims/emailaddress",
        "email",
        "mail",
        "User.Email",
    ]:
        if email_key in attributes:
            email = attributes[email_key][0]
            break
    if not email:
        email = name_id

    # Resolve display name
    display_name = ""
    for name_key in [
        "http://schemas.xmlsoap.org/ws/2005/05/identity/claims/name",
        "http://schemas.microsoft.com/identity/claims/displayname",
        "displayName",
        "name",
        "cn",
    ]:
        if name_key in attributes:
            display_name = attributes[name_key][0]
            break
    if not display_name:
        display_name = email

    # Resolve groups / roles
    groups = []
    for group_key in [
        "http://schemas.microsoft.com/ws/2008/06/identity/claims/groups",
        "http://schemas.xmlsoap.org/claims/Group",
        "groups",
        "group",
        "roles",
        "role",
        "memberOf",
    ]:
        if group_key in attributes:
            groups.extend(attributes[group_key])

    authn_statement = root.find(".//saml:AuthnStatement", SAML_NS)
    session_index = authn_statement.attrib.get("SessionIndex") if authn_statement is not None else None

    return SAMLAssertionData(
        name_id=email,
        attributes=attributes,
        groups=list(dict.fromkeys(groups)),
        display_name=display_name,
        issuer=issuer,
        session_index=session_index,
    )


def generate_sp_metadata_xml(acs_url: str, sp_entity_id: Optional[str] = None) -> str:
    """
    Generates standard SAML 2.0 SP Metadata XML for MSK IT and IdP configuration.
    """
    entity_id = sp_entity_id or settings.saml_sp_entity_id.strip() or acs_url
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata" entityID="{html.escape(entity_id)}">
    <md:SPSSODescriptor AuthnRequestsSigned="false" WantAssertionsSigned="true" protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">
        <md:NameIDFormat>urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress</md:NameIDFormat>
        <md:NameIDFormat>urn:oasis:names:tc:SAML:2.0:nameid-format:persistent</md:NameIDFormat>
        <md:AssertionConsumerService Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST" Location="{html.escape(acs_url)}" index="1" isDefault="true"/>
    </md:SPSSODescriptor>
</md:EntityDescriptor>"""


# ---------------------------------------------------------------------------
# Cookie & Dependency Helpers
# ---------------------------------------------------------------------------

def is_cookie_secure(request: Optional[Request] = None) -> bool:
    """Whether session and auth cookies must have the Secure flag.

    In deployed mode (PUBLIC_APP_BASE_URL starts with https://), Secure is
    always required, regardless of the incoming request.url.scheme (which may be
    http:// behind an internal TLS-terminating reverse proxy / ingress).
    Otherwise, falls back to request.url.scheme == "https" if request is provided.
    For local test runners (testserver over http), avoids breaking in-memory TestClient.
    """
    base = settings.public_app_base_url.strip().lower()
    if base.startswith("https://"):
        if request is not None and getattr(request, "url", None) is not None:
            if request.url.hostname in ("testserver", "localhost", "127.0.0.1") and request.url.scheme == "http":
                if not getattr(settings, "_public_app_base_url_configured", False) or request.url.hostname == "testserver":
                    return False
        return True
    if request is not None and getattr(request, "url", None) is not None:
        return request.url.scheme == "https"
    return False


def set_session_cookie(
    response: Response,
    token: str,
    is_secure: Optional[bool] = None,
    request: Optional[Request] = None,
) -> None:
    secure = is_cookie_secure(request) if is_secure is None else is_secure
    response.set_cookie(
        key=settings.auth_cookie_name,
        value=token,
        max_age=settings.auth_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=settings.auth_cookie_name, path="/")


def _uses_api_key(request: Request) -> bool:
    """True when the request authenticates with an ACGC API key rather than a session.

    A session cookie takes precedence, matching get_current_user's lookup order.
    """
    if request.cookies.get(settings.auth_cookie_name):
        return False
    return looks_like_api_key(bearer_token(request.headers.get("Authorization")))


# request.state attribute caching the API-key resolution for the current request,
# so the observability middleware and require_auth share a single DB lookup.
_API_KEY_STATE_ATTR = "acgc_api_key_auth"
_API_KEY_RATE_CHECKED_ATTR = "acgc_api_key_rate_checked"
# Same idea for session tokens: (user, failure reason) after the live profile
# check, so each request reads the user profile at most once.
_SESSION_STATE_ATTR = "acgc_session_auth"


def _session_token(request: Request) -> Optional[str]:
    return request.cookies.get(settings.auth_cookie_name) or bearer_token(request.headers.get("Authorization"))


def get_current_user(request: Request) -> Optional[AuthenticatedUser]:
    """The caller's identity as already verified for this request.

    API keys and session tokens both need an async lookup (the key record and
    the live user profile respectively), so this sync helper only returns what
    resolve_api_key_user() / resolve_session_user() verified, and None
    otherwise. user_context_middleware resolves both on every request.
    """
    attr = _API_KEY_STATE_ATTR if _uses_api_key(request) else _SESSION_STATE_ATTR
    cached = getattr(request.state, attr, None)
    return cached[0] if cached else None


async def _verify_session(token: str) -> Tuple[Optional[AuthenticatedUser], Optional[str]]:
    claims = decode_session_token(token)
    if claims is None:
        return None, "invalid"

    # The token's role/status are a snapshot from login. Authorize against the
    # current profile so demotions take effect on already-issued sessions.
    try:
        profile = await load_user_profile_strict(claims.email)
    except UserStoreUnavailable as exc:
        # No traceback or email: a cause's message can quote stored data.
        logger.error("User profile lookup failed for a session: %s", describe_store_error(exc))
        return None, "unavailable"

    if profile is None:
        logger.warning("Session rejected for %s: user profile no longer exists", claims.email)
        return None, "invalid"

    if profile.status != "active":
        logger.warning("Session rejected for %s: account status is %s", claims.email, profile.status)
        return None, "inactive"

    if claims.session_generation != profile.session_generation:
        logger.warning(
            "Session rejected for %s: stale session generation (%s != %s)",
            claims.email, claims.session_generation, profile.session_generation,
        )
        return None, "invalid"

    return claims.model_copy(update={
        "role": profile.role,
        "status": profile.status,
        "session_generation": profile.session_generation,
    }), None


async def bump_session_generation(
    email: str,
    *,
    expected_generation: Optional[int] = None,
) -> Optional[int]:
    """Bumps session generation for user across active backend, revoking existing sessions.

    When expected_generation is provided, atomically increments only if the stored
    generation matches expected_generation, returning the new generation or None if
    already revoked / user absent.
    """
    clean_email = email.strip().lower()
    if mysql_user_store_enabled():
        try:
            return await _mysql_store().bump_session_generation(
                clean_email,
                expected_generation=expected_generation,
            )
        except UserStoreUnavailable:
            raise
        except Exception as exc:
            raise UserStoreUnavailable("User store bump_session_generation failed") from exc
    redis_client = await _profile_store_redis()
    if redis_client is not None:
        key = f"agcg:user:{clean_email}"
        try:
            from redis.exceptions import WatchError

            async with redis_client.pipeline(transaction=True) as pipe:
                while True:
                    try:
                        await pipe.watch(key)
                        payload = await pipe.get(key)
                        if not payload:
                            await pipe.unwatch()
                            return None if expected_generation is not None else 1
                        profile = UserProfile.model_validate_json(payload)
                        if (
                            expected_generation is not None
                            and profile.session_generation != expected_generation
                        ):
                            await pipe.unwatch()
                            return None
                        new_gen = profile.session_generation + 1
                        updated = profile.model_copy(update={"session_generation": new_gen})
                        pipe.multi()
                        pipe.set(key, updated.model_dump_json())
                        await pipe.execute()
                        break
                    except WatchError:
                        continue
        except Exception as exc:
            raise UserStoreUnavailable("Failed to bump session generation in Redis") from exc
        async with _user_store_lock:
            _user_store[clean_email] = updated
        return new_gen
    async with _user_store_lock:
        profile = _user_store.get(clean_email)
        if profile is None:
            return None if expected_generation is not None else 1
        if expected_generation is not None and profile.session_generation != expected_generation:
            return None
        new_gen = profile.session_generation + 1
        _user_store[clean_email] = profile.model_copy(update={"session_generation": new_gen})
        return new_gen


async def resolve_session_user(request: Request) -> Optional[AuthenticatedUser]:
    """Verify the session cookie / session bearer token (once per request, cached).

    Returns None for API-key requests, a missing/invalid/expired token, a
    deleted or non-active profile, or an unreadable profile store; the cached
    failure reason ("invalid", "inactive", "unavailable") tells them apart.
    """
    if _uses_api_key(request):
        return None
    cached = getattr(request.state, _SESSION_STATE_ATTR, None)
    if cached is None:
        token = _session_token(request)
        if not token:
            cached = (None, None)
        elif not settings.auth_enabled:
            # Auth disabled: nothing to authorize against, so keep the token's
            # claims (and its pre-existing rejection of inactive snapshots).
            claims = decode_session_token(token)
            cached = (claims if claims and claims.status == "active" else None, None)
        else:
            cached = await _verify_session(token)
        setattr(request.state, _SESSION_STATE_ATTR, cached)
    return cached[0]


def session_store_unavailable(request: Request) -> bool:
    """True when this request's session couldn't be checked against the user store."""
    cached = getattr(request.state, _SESSION_STATE_ATTR, None)
    return bool(cached) and cached[1] == "unavailable"


async def _verify_api_key(request: Request, plaintext: str) -> Tuple[Optional[AuthenticatedUser], Optional[str]]:
    store = getattr(request.app.state, "run_store", None)
    if store is None:
        return None, "unavailable"
    try:
        # Every well-formed key takes the same path: one indexed lookup by its
        # full SHA-256, so the lookup reveals nothing about partial matches.
        record = await store.get_api_key_by_hash(hash_api_key(plaintext))
    except Exception:
        logger.exception("API key lookup failed")
        return None, "unavailable"

    if record is None or not api_key_matches(plaintext, record):
        logger.warning("API key rejected: unknown key")
        return None, "invalid"

    inactive = api_key_inactive_reason(record)
    if inactive:
        logger.warning("API key %s rejected: %s", record.id, inactive)
        return None, "invalid"

    # Re-check the owner against the live allowlist so offboarded users and
    # domains lose access even though their key was never revoked.
    owner_email = record.owner_email.strip().lower()
    allowed, reason = is_email_allowed(owner_email)
    if not allowed:
        logger.warning("API key %s rejected for %s: %s", record.id, owner_email, reason)
        return None, "invalid"

    now = datetime.now(timezone.utc)
    if should_touch_last_used(record.last_used_at, settings.api_key_last_used_update_seconds, now):
        try:
            await store.touch_api_key(record.id, now)
        except Exception as exc:
            logger.warning("Failed to update last_used_at for API key %s: %s", record.id, exc)

    # Same authoritative read as sessions: with MySQL or Redis as the store,
    # an owner missing there was deleted. Without either the in-process store
    # is lost on restart, so a missing owner keeps the default-role behaviour.
    try:
        profile = await load_user_profile_strict(owner_email)
    except UserStoreUnavailable as exc:
        logger.error(
            "User profile lookup failed for API key %s owner: %s", record.id, describe_store_error(exc)
        )
        return None, "unavailable"
    if profile is None and (mysql_user_store_enabled() or settings.redis_url.strip()):
        logger.warning("API key %s rejected: owner %s has no user profile", record.id, owner_email)
        return None, "invalid"

    return (
        AuthenticatedUser(
            email=owner_email,
            name=(profile.name if profile else None) or owner_email,
            picture=profile.picture if profile else None,
            domain=owner_email.split("@")[-1],
            role=profile.role if profile else settings.jit_default_role,
            status=profile.status if profile else "active",
            provider="api_key",
            groups=list(profile.groups) if profile else [],
            auth_method="api_key",
            api_key_id=record.id,
        ),
        None,
    )


async def resolve_api_key_user(request: Request) -> Optional[AuthenticatedUser]:
    """Verify an `Authorization: Bearer acgc_...` key (once per request, cached).

    Returns the key owner's identity, or None when auth is disabled, the request
    doesn't carry an API key, or the key is unknown/revoked/expired/disallowed.
    """
    if not settings.auth_enabled or not _uses_api_key(request):
        return None
    cached = getattr(request.state, _API_KEY_STATE_ATTR, None)
    if cached is None:
        plaintext = parse_api_key(bearer_token(request.headers.get("Authorization")))
        if plaintext is None:
            cached = (None, "invalid")
        else:
            cached = await _verify_api_key(request, plaintext)
        setattr(request.state, _API_KEY_STATE_ATTR, cached)
    return cached[0]


async def get_request_user(request: Request) -> Optional[AuthenticatedUser]:
    """Async counterpart of get_current_user that resolves API keys and sessions."""
    await resolve_api_key_user(request)
    await resolve_session_user(request)
    return get_current_user(request)


async def _enforce_api_key_rate_limit(request: Request, user: AuthenticatedUser) -> None:
    if not user.api_key_id or getattr(request.state, _API_KEY_RATE_CHECKED_ATTR, False):
        return
    setattr(request.state, _API_KEY_RATE_CHECKED_ATTR, True)
    allowed, retry_after = await check_api_key_rate_limit(
        user.api_key_id,
        settings.api_key_rate_limit_per_minute,
        redis_client=await _get_auth_redis_client(),
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="API key rate limit exceeded. Please slow down and retry later.",
            headers={"Retry-After": str(retry_after)},
        )


_INACTIVE_ACCOUNT_DETAIL = "Your account is pending administrator approval. Please contact your AGCG administrator."


async def require_auth(request: Request) -> AuthenticatedUser:
    if not settings.auth_enabled:
        return AuthenticatedUser(
            email="local@internal",
            name="Local Developer",
            domain="internal",
            role="admin",
            provider="local",
            groups=["Admins"],
        )

    uses_api_key = _uses_api_key(request)
    if uses_api_key:
        user = await resolve_api_key_user(request)
        if not user and getattr(request.state, _API_KEY_STATE_ATTR, (None, None))[1] == "unavailable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API key verification is temporarily unavailable.",
            )
        if not user:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid, revoked, or expired API key.",
                headers={"WWW-Authenticate": "Bearer"},
            )
    else:
        user = await resolve_session_user(request)
        reason = getattr(request.state, _SESSION_STATE_ATTR, (None, None))[1]
        if reason == "unavailable":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Session verification is temporarily unavailable. Please retry shortly.",
            )
        if reason == "inactive":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_INACTIVE_ACCOUNT_DETAIL)
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required. Please sign in with your MSK (@mskcc.org) or OpenEvidence (@openevidence.com) account.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if user.status != "active":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=_INACTIVE_ACCOUNT_DETAIL,
        )

    if uses_api_key:
        await _enforce_api_key_rate_limit(request, user)
        logger.debug("Request authenticated with API key %s for %s", user.api_key_id, user.email)

    return user


async def require_admin(request: Request) -> AuthenticatedUser:
    user = await require_auth(request)
    if user.role != "admin" and not settings.agcg_dev_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required for this action.",
        )
    return user


async def require_curator(request: Request) -> AuthenticatedUser:
    """Allow curators and admins to edit curated overrides.

    require_auth re-reads the user's current role and status from the user
    store on every request (sessions and API keys alike), so a promotion or
    demotion on /admin/users applies to the very next request.
    """
    user = await require_auth(request)
    if not can_curate(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Curator privileges required for this action.",
        )
    return user


def render_access_denied_html(email: str, reason: str, login_url: str = "/auth/login") -> str:
    escaped_email = html.escape(email)
    escaped_reason = html.escape(reason)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Access Restricted — Agentic Cancer Gene Classification</title>
  <style>
    :root {{
      --bg: #f5f7f8;
      --card-bg: #ffffff;
      --line: #cdd6dc;
      --text: #152026;
      --muted: #60717c;
      --accent: #0f766e;
      --danger: #b42318;
      --danger-bg: #fef3f2;
    }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      color: var(--text);
      padding: 24px;
      box-sizing: border-box;
    }}
    .auth-card {{
      background: var(--card-bg);
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 36px 32px;
      max-width: 480px;
      width: 100%;
      box-shadow: 0 18px 40px rgba(22, 34, 42, 0.08);
      text-align: center;
    }}
    .badge {{
      display: inline-block;
      background: var(--danger-bg);
      color: var(--danger);
      font-weight: 600;
      font-size: 12px;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      padding: 4px 10px;
      border-radius: 999px;
      margin-bottom: 16px;
    }}
    h1 {{
      font-size: 22px;
      font-weight: 600;
      margin: 0 0 12px;
    }}
    .email-box {{
      background: #f8fafc;
      border: 1px dashed var(--line);
      padding: 10px;
      border-radius: 6px;
      font-family: monospace;
      font-size: 14px;
      margin: 16px 0;
      word-break: break-all;
    }}
    p {{
      color: var(--muted);
      font-size: 14px;
      line-height: 1.5;
      margin: 0 0 18px;
    }}
    .allowed-tags {{
      display: flex;
      gap: 8px;
      justify-content: center;
      margin: 12px 0 24px;
    }}
    .domain-tag {{
      background: #eef2f4;
      color: var(--text);
      font-size: 13px;
      font-weight: 500;
      padding: 4px 8px;
      border-radius: 4px;
    }}
    .btn {{
      display: inline-block;
      background: var(--accent);
      color: white;
      text-decoration: none;
      font-size: 14px;
      font-weight: 500;
      padding: 10px 20px;
      border-radius: 6px;
      transition: background 0.15s ease;
    }}
    .btn:hover {{
      background: #0b5f59;
    }}
    .support-note {{
      font-size: 12px;
      color: var(--muted);
      margin-top: 24px;
    }}
  </style>
</head>
<body>
  <div class="auth-card">
    <span class="badge">Access Restricted</span>
    <h1>Domain Not Authorized</h1>
    <p>You authenticated with:</p>
    <div class="email-box">{escaped_email}</div>
    <p>{escaped_reason}</p>
    <div class="allowed-tags">
      <span class="domain-tag">@mskcc.org</span>
      <span class="domain-tag">@openevidence.com</span>
    </div>
    <a href="{login_url}" class="btn">Sign In with an Authorized Account</a>
    <div class="support-note">
      If you are a member of MSK or OpenEvidence and believe this is an error, please contact your systems administrator.
    </div>
  </div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Keycloak OIDC & PingID SSO Helpers (keycloak.oncokb.org)
# ---------------------------------------------------------------------------


def get_keycloak_realm_base_url() -> str:
    base = settings.keycloak_url.rstrip("/")
    realm = settings.keycloak_realm.strip()
    return f"{base}/realms/{realm}"


def get_keycloak_auth_url(
    redirect_uri: str,
    state: str,
    idp_hint: Optional[str] = None,
    code_challenge: Optional[str] = None,
    nonce: Optional[str] = None,
) -> str:
    """Constructs Keycloak OpenID Connect authorization URL.

    If idp_hint is provided (or defaults to keycloak_ping_idp_alias, e.g. 'msk-ping'),
    appends kc_idp_hint to redirect directly to MSK PingFederate without presenting
    an intermediate Keycloak login screen.
    """
    realm_url = get_keycloak_realm_base_url()
    auth_endpoint = f"{realm_url}/protocol/openid-connect/auth"
    params = {
        "client_id": settings.keycloak_client_id,
        "response_type": "code",
        "scope": "openid email profile",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    if code_challenge:
        params["code_challenge"] = code_challenge
        params["code_challenge_method"] = "S256"
    if nonce:
        params["nonce"] = nonce
    hint = idp_hint if idp_hint is not None else settings.keycloak_ping_idp_alias
    if hint and hint.strip() and hint.strip().lower() != "none":
        params["kc_idp_hint"] = hint.strip()

    return f"{auth_endpoint}?{urlencode(params)}"


async def exchange_keycloak_code(
    code: str,
    redirect_uri: str,
    code_verifier: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchanges authorization code for OIDC tokens with Keycloak token endpoint."""
    if not settings.keycloak_client_secret or not settings.keycloak_client_secret.strip():
        logger.error("Keycloak token exchange failed: KEYCLOAK_CLIENT_SECRET is missing or empty in environment.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=(
                "Keycloak OAuth misconfigured: KEYCLOAK_CLIENT_SECRET is not set in the server environment. "
                "Ensure Kubernetes Secret has KEYCLOAK_CLIENT_SECRET and the pod has been restarted."
            ),
        )

    realm_url = get_keycloak_realm_base_url()
    token_endpoint = f"{realm_url}/protocol/openid-connect/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": settings.keycloak_client_id,
        "client_secret": settings.keycloak_client_secret,
    }
    if code_verifier:
        data["code_verifier"] = code_verifier
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(token_endpoint, data=data)
        if resp.status_code != 200:
            logger.error("Failed Keycloak token exchange (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Keycloak authentication failed during token exchange: {resp.text}",
            )
        return resp.json()


async def get_keycloak_user_info(access_token: str) -> Dict[str, Any]:
    """Fetches user claims from Keycloak userinfo endpoint."""
    realm_url = get_keycloak_realm_base_url()
    userinfo_endpoint = f"{realm_url}/protocol/openid-connect/userinfo"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(userinfo_endpoint, headers=headers)
        if resp.status_code != 200:
            logger.error("Failed to fetch Keycloak user info (%s): %s", resp.status_code, resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to retrieve user profile from Keycloak.",
            )
        return resp.json()


KEYCLOAK_JWKS_CACHE_TTL_SECONDS = 300
KEYCLOAK_JWKS_REFRESH_MIN_INTERVAL_SECONDS = 60
KEYCLOAK_JWKS_FAILURE_BACKOFF_SECONDS = 10
KEYCLOAK_JWT_LEEWAY_SECONDS = 60


class JwksState:
    """App-lifespan-scoped Keycloak JWKS cache, refresh throttle, backoff, and single-flight locks."""

    def __init__(self) -> None:
        # Maps jwks_url -> (expires_at, keys, version)
        self.cache: Dict[str, Tuple[float, List[Dict[str, Any]], int]] = {}
        # Maps jwks_url -> monotonic timestamp of last successful failure-triggered refresh (60s bound).
        self.last_refresh: Dict[str, float] = {}
        # Maps jwks_url -> (failed_at_mono, error_detail) for failed fetches/refreshes (10s back-off).
        self.last_failure: Dict[str, Tuple[float, str]] = {}
        self.version: Dict[str, int] = {}
        # Lazily created per jwks_url in this app's loop; cleared and dropped on app shutdown.
        self.locks: Dict[str, asyncio.Lock] = {}

    def get_lock(self, jwks_url: str) -> asyncio.Lock:
        lock = self.locks.get(jwks_url)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[jwks_url] = lock
        return lock

    def clear_keycloak_jwks_cache(self) -> None:
        """Clears cached Keycloak realm JWKS keys, refresh throttle, failure back-off, and locks."""
        self.cache.clear()
        self.last_refresh.clear()
        self.last_failure.clear()
        self.version.clear()
        self.locks.clear()

    clear = clear_keycloak_jwks_cache


_RSA_PKCS1_ALGS = {
    "RS256": hashes.SHA256,
    "RS384": hashes.SHA384,
    "RS512": hashes.SHA512,
}
_RSA_PSS_ALGS = {
    "PS256": hashes.SHA256,
    "PS384": hashes.SHA384,
    "PS512": hashes.SHA512,
}
_EC_ALGS = {
    "ES256": (ec.SECP256R1, hashes.SHA256, 32),
    "ES384": (ec.SECP384R1, hashes.SHA384, 48),
    "ES512": (ec.SECP521R1, hashes.SHA512, 66),
}


def clear_keycloak_jwks_cache(jwks_state: JwksState) -> None:
    """Clears cached Keycloak realm JWKS keys and throttle state on a JwksState."""
    jwks_state.clear_keycloak_jwks_cache()


def _jwks_monotonic() -> float:
    return time.monotonic()


def _jwks_refresh_min_interval_seconds() -> int:
    return max(
        0,
        int(
            getattr(
                settings,
                "keycloak_jwks_refresh_min_interval_seconds",
                KEYCLOAK_JWKS_REFRESH_MIN_INTERVAL_SECONDS,
            )
        ),
    )


def _jwks_failure_backoff_seconds() -> int:
    return max(
        0,
        int(
            getattr(
                settings,
                "keycloak_jwks_failure_backoff_seconds",
                KEYCLOAK_JWKS_FAILURE_BACKOFF_SECONDS,
            )
        ),
    )


def _format_jwks_unavailable_detail(exc: BaseException) -> str:
    if isinstance(exc, HTTPException) and exc.detail:
        msg = str(exc.detail)
        if "identity provider unavailable" in msg.lower():
            return msg
        return f"Identity provider unavailable: {msg}"
    return "Identity provider unavailable: unable to retrieve Keycloak signing keys."


async def fetch_keycloak_jwks(jwks_url: str) -> List[Dict[str, Any]]:
    """Fetches the realm JWKS document from Keycloak."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(jwks_url)
    except Exception as exc:
        logger.error("Failed to fetch Keycloak JWKS (%s): %s", jwks_url, type(exc).__name__)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve Keycloak signing keys.",
        ) from exc
    if resp.status_code != 200:
        logger.error("Keycloak JWKS endpoint returned HTTP %s", resp.status_code)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to retrieve Keycloak signing keys.",
        )
    try:
        body = resp.json()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Invalid Keycloak JWKS response.",
        ) from exc
    keys = body.get("keys") if isinstance(body, dict) else None
    if not isinstance(keys, list) or not all(isinstance(k, dict) for k in keys):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malformed Keycloak JWKS response.",
        )
    return keys


async def _fetch_and_store_jwks(
    jwks_url: str,
    *,
    jwks_state: JwksState,
) -> Tuple[List[Dict[str, Any]], int]:
    """Fetches JWKS, updating the cache on success or recording failure backoff on error.

    Caller must hold ``jwks_state.get_lock(jwks_url)``. Keeps any existing cache entry intact on failure.
    """
    try:
        keys = await fetch_keycloak_jwks(jwks_url)
    except Exception as exc:
        detail = _format_jwks_unavailable_detail(exc)
        jwks_state.last_failure[jwks_url] = (_jwks_monotonic(), detail)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=detail,
        ) from exc

    jwks_state.last_failure.pop(jwks_url, None)
    new_version = jwks_state.version.get(jwks_url, 0) + 1
    jwks_state.version[jwks_url] = new_version
    jwks_state.cache[jwks_url] = (
        _jwks_monotonic() + KEYCLOAK_JWKS_CACHE_TTL_SECONDS,
        keys,
        new_version,
    )
    return keys, new_version


def _maybe_raise_recent_jwks_failure(
    jwks_url: str,
    *,
    jwks_state: JwksState,
    now_mono: float,
    wait_started_mono: float,
) -> None:
    last_failure = jwks_state.last_failure.get(jwks_url)
    if last_failure is None:
        return
    failed_at, error_detail = last_failure
    if failed_at >= wait_started_mono or (now_mono - failed_at) < _jwks_failure_backoff_seconds():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=error_detail,
        )


async def _get_keycloak_jwks(
    jwks_url: str,
    *,
    jwks_state: JwksState,
    force_refresh: bool = False,
) -> Tuple[List[Dict[str, Any]], int, bool]:
    now_mono = _jwks_monotonic()
    if not force_refresh:
        cached = jwks_state.cache.get(jwks_url)
        if cached is not None and now_mono < cached[0]:
            return cached[1], cached[2], True

    observed_version = jwks_state.version.get(jwks_url, 0)
    wait_started_mono = now_mono
    lock = jwks_state.get_lock(jwks_url)
    async with lock:
        now_mono = _jwks_monotonic()
        cached = jwks_state.cache.get(jwks_url)
        if not force_refresh and cached is not None and now_mono < cached[0]:
            # If another waiter fetched new keys while we waited on the lock, treat them as
            # fresh for this caller so concurrent failures don't immediately re-fetch.
            from_cache = cached[2] == observed_version
            return cached[1], cached[2], from_cache
        # If no valid unexpired cache exists, reuse any in-flight or short-backoff failure
        # instead of letting queued waiters stampede the failing JWKS endpoint.
        _maybe_raise_recent_jwks_failure(
            jwks_url,
            jwks_state=jwks_state,
            now_mono=now_mono,
            wait_started_mono=wait_started_mono,
        )
        keys, new_version = await _fetch_and_store_jwks(jwks_url, jwks_state=jwks_state)
        return keys, new_version, False


async def _refresh_keycloak_jwks_on_failure(
    jwks_url: str,
    *,
    jwks_state: JwksState,
    failed_version: int,
) -> Optional[List[Dict[str, Any]]]:
    """Coalesces concurrent JWKS refreshes and bounds them per realm.

    Uses two distinct intervals:
    - Successful refreshes update ``jwks_state.last_refresh`` and are bounded by the
      full minimum interval (default 60s), avoiding repeated fetches on attacker bad-kid floods.
    - Failed refreshes update ``jwks_state.last_failure`` (leaving ``jwks_state.last_refresh``
      untouched and keeping any valid cached JWKS usable) and are bounded only by the short failure
      back-off (default 10s), so a recovered Keycloak is retried promptly for newly rotated keys.
    """
    wait_started_mono = _jwks_monotonic()
    lock = jwks_state.get_lock(jwks_url)
    async with lock:
        cached = jwks_state.cache.get(jwks_url)
        if cached is not None and cached[2] != failed_version:
            # Another concurrent request already refreshed this realm's JWKS while we waited.
            return cached[1]
        now_mono = _jwks_monotonic()
        last_refresh = jwks_state.last_refresh.get(jwks_url)
        if last_refresh is not None and (now_mono - last_refresh) < _jwks_refresh_min_interval_seconds():
            return None
        _maybe_raise_recent_jwks_failure(
            jwks_url,
            jwks_state=jwks_state,
            now_mono=now_mono,
            wait_started_mono=wait_started_mono,
        )
        keys, _new_version = await _fetch_and_store_jwks(jwks_url, jwks_state=jwks_state)
        jwks_state.last_refresh[jwks_url] = _jwks_monotonic()
        return keys


def _verify_jws_signature_with_jwk(
    signing_input: bytes,
    sig_bytes: bytes,
    alg: str,
    jwk: Dict[str, Any],
) -> bool:
    use = jwk.get("use")
    if use is not None and use != "sig":
        return False
    jwk_alg = jwk.get("alg")
    if jwk_alg is not None and jwk_alg != alg:
        return False

    kty = jwk.get("kty")
    try:
        if alg in _RSA_PKCS1_ALGS or alg in _RSA_PSS_ALGS:
            if kty != "RSA":
                return False
            n = int.from_bytes(_b64url_decode_bytes(jwk["n"]), "big")
            e = int.from_bytes(_b64url_decode_bytes(jwk["e"]), "big")
            pub_key = rsa.RSAPublicNumbers(e, n).public_key()
            if alg in _RSA_PKCS1_ALGS:
                hash_cls = _RSA_PKCS1_ALGS[alg]
                pub_key.verify(sig_bytes, signing_input, padding.PKCS1v15(), hash_cls())
            else:
                hash_cls = _RSA_PSS_ALGS[alg]
                hash_obj = hash_cls()
                pub_key.verify(
                    sig_bytes,
                    signing_input,
                    padding.PSS(mgf=padding.MGF1(hash_obj), salt_length=hash_obj.digest_size),
                    hash_obj,
                )
            return True

        if alg in _EC_ALGS:
            if kty != "EC":
                return False
            curve_cls, hash_cls, coord_size = _EC_ALGS[alg]
            expected_crv = {
                "ES256": "P-256",
                "ES384": "P-384",
                "ES512": "P-521",
            }[alg]
            if jwk.get("crv") != expected_crv:
                return False
            if len(sig_bytes) != 2 * coord_size:
                return False
            x = int.from_bytes(_b64url_decode_bytes(jwk["x"]), "big")
            y = int.from_bytes(_b64url_decode_bytes(jwk["y"]), "big")
            pub_key = ec.EllipticCurvePublicNumbers(x, y, curve_cls()).public_key()
            r = int.from_bytes(sig_bytes[:coord_size], "big")
            s = int.from_bytes(sig_bytes[coord_size:], "big")
            der_sig = encode_dss_signature(r, s)
            pub_key.verify(der_sig, signing_input, ec.ECDSA(hash_cls()))
            return True
    except (InvalidSignature, KeyError, TypeError, ValueError):
        return False
    return False


async def _verify_keycloak_jwt_signature(
    token: str,
    *,
    jwks_state: JwksState,
) -> Dict[str, Any]:
    if not isinstance(token, str) or not token.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing Keycloak JWT.",
        )
    parts = token.split(".")
    if len(parts) != 3 or not all(parts):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Malformed Keycloak JWT.",
        )
    try:
        header = json.loads(_b64url_decode_bytes(parts[0]).decode("utf-8"))
        payload = json.loads(_b64url_decode_bytes(parts[1]).decode("utf-8"))
        sig_bytes = _b64url_decode_bytes(parts[2])
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Malformed Keycloak JWT encoding.",
        ) from exc

    if not isinstance(header, dict) or not isinstance(payload, dict) or not sig_bytes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Malformed Keycloak JWT structure.",
        )

    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in (_RSA_PKCS1_ALGS.keys() | _RSA_PSS_ALGS.keys() | _EC_ALGS.keys()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Unsupported or unsigned Keycloak JWT algorithm.",
        )

    kid = header.get("kid")
    if kid is not None and not isinstance(kid, str):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Malformed Keycloak JWT kid header.",
        )

    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    jwks_url = f"{get_keycloak_realm_base_url().rstrip('/')}/protocol/openid-connect/certs"

    def _try_keys(keys: List[Dict[str, Any]]) -> bool:
        candidates = [k for k in keys if k.get("kid") == kid] if kid is not None else keys
        return any(_verify_jws_signature_with_jwk(signing_input, sig_bytes, alg, k) for k in candidates)

    keys, jwks_version, from_cache = await _get_keycloak_jwks(
        jwks_url,
        jwks_state=jwks_state,
        force_refresh=False,
    )
    verified = _try_keys(keys)
    if not verified:
        if not from_cache:
            # Freshly fetched (or coalesced onto a fresh fetch) and verification still failed;
            # record the failure refresh timestamp so subsequent failures are throttled.
            jwks_state.last_refresh[jwks_url] = _jwks_monotonic()
        else:
            refreshed_keys = await _refresh_keycloak_jwks_on_failure(
                jwks_url,
                jwks_state=jwks_state,
                failed_version=jwks_version,
            )
            if refreshed_keys is not None:
                verified = _try_keys(refreshed_keys)

    if not verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak ID token signature verification failed.",
        )
    return payload


def _validate_keycloak_timestamps(claims: Dict[str, Any]) -> None:
    now = int(time.time())
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak token missing valid exp claim.",
        )
    if exp < now - KEYCLOAK_JWT_LEEWAY_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak token has expired.",
        )
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)) or isinstance(iat, bool):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak token missing valid iat claim.",
        )
    if iat > now + KEYCLOAK_JWT_LEEWAY_SECONDS:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak token iat claim is in the future.",
        )
    if "nbf" in claims:
        nbf = claims["nbf"]
        if not isinstance(nbf, (int, float)) or isinstance(nbf, bool) or nbf > now + KEYCLOAK_JWT_LEEWAY_SECONDS:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak token is not yet valid (nbf).",
            )


async def verify_keycloak_id_token(
    id_token: Any,
    *,
    jwks_state: JwksState,
    expected_nonce: Optional[str] = None,
    require_nonce: bool = True,
) -> Dict[str, Any]:
    """Verifies Keycloak ID token signature (via realm JWKS) and validates required OIDC claims."""
    if not isinstance(id_token, str) or not id_token.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Missing Keycloak ID token.",
        )

    id_claims = await _verify_keycloak_jwt_signature(id_token, jwks_state=jwks_state)

    expected_iss = get_keycloak_realm_base_url().rstrip("/")
    token_iss = id_claims.get("iss")
    if not isinstance(token_iss, str) or not token_iss.strip() or token_iss.rstrip("/") != expected_iss:
        logger.warning("Keycloak ID token issuer mismatch: %r != %r", token_iss, expected_iss)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Keycloak ID token issuer mismatch: expected {expected_iss}",
        )

    expected_aud = settings.keycloak_client_id.strip()
    if not expected_aud:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak client ID is not configured.",
        )
    token_aud = id_claims.get("aud")
    aud_match = False
    if isinstance(token_aud, str):
        aud_match = token_aud == expected_aud
    elif isinstance(token_aud, list) and all(isinstance(a, str) for a in token_aud):
        aud_match = expected_aud in token_aud
    if not aud_match:
        logger.warning(
            "Keycloak ID token audience mismatch: expected %s, got aud=%r, azp=%r",
            expected_aud, token_aud, id_claims.get("azp"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Keycloak ID token audience mismatch: expected {expected_aud}",
        )

    if "azp" in id_claims:
        if not isinstance(id_claims["azp"], str) or id_claims["azp"] != expected_aud:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak ID token authorized party (azp) mismatch.",
            )
    elif isinstance(token_aud, list) and len(token_aud) > 1:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak ID token missing azp claim for multi-audience token.",
        )

    _validate_keycloak_timestamps(id_claims)

    if require_nonce or expected_nonce is not None:
        if not expected_nonce or not isinstance(expected_nonce, str):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Missing expected OAuth transaction nonce.",
            )
        token_nonce = id_claims.get("nonce")
        if not isinstance(token_nonce, str) or not token_nonce:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak ID token missing nonce claim.",
            )
        if not hmac.compare_digest(token_nonce, expected_nonce):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak ID token nonce mismatch",
            )

    sub = id_claims.get("sub")
    if not isinstance(sub, str) or not sub.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak ID token missing sub claim.",
        )

    if id_claims.get("email_verified") is not True:
        logger.warning(
            "Rejected Keycloak user with unverified email in ID token (email_verified=%r): %s",
            id_claims.get("email_verified"), id_claims.get("email"),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak reports that this email address is unverified.",
        )

    raw_email = id_claims.get("email")
    if not isinstance(raw_email, str):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak ID token does not contain a valid email address.",
        )
    clean_email = raw_email.strip().lower()
    if not clean_email or "@" not in clean_email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Keycloak ID token does not contain a valid email address.",
        )

    return id_claims


def _extract_keycloak_roles_from_claims(payload: Dict[str, Any]) -> List[str]:
    extracted: List[str] = []
    realm_access = payload.get("realm_access")
    if isinstance(realm_access, dict):
        roles = realm_access.get("roles")
        if isinstance(roles, list):
            extracted.extend(str(r) for r in roles if isinstance(r, str))
    resource_access = payload.get("resource_access")
    if isinstance(resource_access, dict):
        client_access = resource_access.get(settings.keycloak_client_id)
        if isinstance(client_access, dict):
            roles = client_access.get("roles")
            if isinstance(roles, list):
                extracted.extend(str(r) for r in roles if isinstance(r, str))
    groups = payload.get("groups")
    if isinstance(groups, list):
        extracted.extend(str(g) for g in groups if isinstance(g, str))
    return extracted


async def parse_keycloak_user(
    token_data: Dict[str, Any],
    userinfo: Optional[Dict[str, Any]] = None,
    *,
    jwks_state: JwksState,
    expected_nonce: Optional[str] = None,
    require_nonce: bool = False,
    verified_id_claims: Optional[Dict[str, Any]] = None,
) -> AuthenticatedUser:
    """Verifies Keycloak ID token + optional userinfo binding and constructs AuthenticatedUser."""
    id_claims = (
        verified_id_claims
        if verified_id_claims is not None
        else await verify_keycloak_id_token(
            token_data.get("id_token"),
            jwks_state=jwks_state,
            expected_nonce=expected_nonce,
            require_nonce=require_nonce,
        )
    )

    id_sub = str(id_claims["sub"])
    clean_email = str(id_claims["email"]).strip().lower()

    if userinfo is not None:
        if not isinstance(userinfo, dict):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Malformed Keycloak userinfo response.",
            )
        ui_sub = userinfo.get("sub")
        if not isinstance(ui_sub, str) or not ui_sub or not hmac.compare_digest(ui_sub, id_sub):
            logger.warning("Keycloak userinfo sub mismatch: %r != %r", ui_sub, id_sub)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak userinfo subject mismatch.",
            )
        if "email_verified" in userinfo and userinfo["email_verified"] is not True:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak reports that this email address is unverified.",
            )
        if "email" in userinfo and userinfo["email"] is not None:
            if not isinstance(userinfo["email"], str) or userinfo["email"].strip().lower() != clean_email:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Keycloak userinfo email mismatch.",
                )

    profile_source = userinfo if userinfo is not None else id_claims
    name = profile_source.get("name")
    if not isinstance(name, str) or not name.strip():
        given = str(profile_source.get("given_name") or "").strip()
        family = str(profile_source.get("family_name") or "").strip()
        if given or family:
            name = f"{given} {family}".strip()
        else:
            pref = profile_source.get("preferred_username")
            name = str(pref).strip() if isinstance(pref, str) and pref.strip() else clean_email.split("@")[0]

    picture = profile_source.get("picture")
    if picture is not None and not isinstance(picture, str):
        picture = None

    domain = clean_email.split("@")[-1]

    # Roles and groups extraction from verified sources only
    groups: List[str] = []
    groups.extend(_extract_keycloak_roles_from_claims(id_claims))

    if userinfo is not None:
        if isinstance(userinfo.get("groups"), list):
            groups.extend(str(g) for g in userinfo["groups"] if isinstance(g, str))
        if isinstance(userinfo.get("roles"), list):
            groups.extend(str(r) for r in userinfo["roles"] if isinstance(r, str))

    access_token = token_data.get("access_token")
    if isinstance(access_token, str) and access_token.count(".") == 2:
        at_claims = await _verify_keycloak_jwt_signature(access_token, jwks_state=jwks_state)
        expected_iss = get_keycloak_realm_base_url().rstrip("/")
        at_iss = at_claims.get("iss")
        if not isinstance(at_iss, str) or at_iss.rstrip("/") != expected_iss:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak access token issuer mismatch.",
            )
        _validate_keycloak_timestamps(at_claims)
        at_sub = at_claims.get("sub")
        if at_sub is not None and (not isinstance(at_sub, str) or not hmac.compare_digest(at_sub, id_sub)):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Keycloak access token subject mismatch.",
            )
        groups.extend(_extract_keycloak_roles_from_claims(at_claims))

    unique_groups = sorted(set(groups))

    # Determine user role
    role = settings.jit_default_role
    admin_roles = set(settings.keycloak_admin_roles_list)
    if admin_roles and any(r in admin_roles for r in unique_groups):
        role = "admin"
    elif settings.saml_admin_groups_list and any(g in set(settings.saml_admin_groups_list) for g in unique_groups):
        role = "admin"

    return AuthenticatedUser(
        email=clean_email,
        name=name,
        picture=picture,
        domain=domain,
        role=role,
        status="active",
        provider="keycloak",
        groups=unique_groups,
    )

