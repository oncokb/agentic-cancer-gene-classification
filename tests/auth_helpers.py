"""Helpers for tests that sign in with a minted session token.

Profiles go to whichever user store src.auth reads (USER_STORE_BACKEND):
the in-process Redis-less store, or the FakeUserStore the
`user_store_backend` fixture installs for "mysql".
"""

from dataclasses import replace
from datetime import datetime, timezone
from typing import Optional

from src import auth
from src.auth import AuthenticatedUser, UserProfile, create_session_token


def _fake_mysql_store():
    """The FakeUserStore when USER_STORE_BACKEND=mysql, else None."""
    return auth._mysql_user_store if auth.mysql_user_store_enabled() else None


def save_profile_for(user: AuthenticatedUser) -> UserProfile:
    """Store the JIT profile a real login would have provisioned for ``user``."""
    now_iso = datetime.now(timezone.utc).isoformat()
    profile = UserProfile(
        email=user.email.strip().lower(),
        name=user.name,
        picture=user.picture,
        domain=user.domain,
        role=user.role,
        status=user.status,
        provider=user.provider,
        groups=user.groups,
        first_login_at=now_iso,
        last_login_at=now_iso,
    )
    put_profile(profile)
    return profile


def put_profile(profile: UserProfile) -> None:
    """Store ``profile`` as is (role and status included) in the active store."""
    store = _fake_mysql_store()
    if store is None:
        auth._user_store[profile.email] = profile
        return
    from src.pipeline.user_store import UserRecord, prepare_import

    first = datetime.fromisoformat(profile.first_login_at)
    last = datetime.fromisoformat(profile.last_login_at)
    record = prepare_import(
        UserRecord(
            email=profile.email, name=profile.name, picture=profile.picture, domain=profile.domain,
            provider=profile.provider, status=profile.status, role=profile.role,
            role_updated_by=None, role_updated_at=None, groups=list(profile.groups),
            claims=dict(profile.claims), first_login_at=first, last_login_at=last,
            login_count=profile.login_count, annotation_count=profile.annotation_count,
            last_annotation_at=None, created_at=first, updated_at=last,
        )
    )
    # Synchronous tests can't await import_user; no other task holds the lock.
    store._users[record.email] = record


def stored_profile(email: str) -> Optional[UserProfile]:
    """The profile the active store holds for ``email``, or None."""
    clean = email.strip().lower()
    store = _fake_mysql_store()
    if store is None:
        return auth._user_store.get(clean)
    record = store._users.get(clean)
    return auth._profile_from_record(record) if record is not None else None


def update_profile(email: str, **changes) -> None:
    """Change stored fields (e.g. an admin's demotion) in the active store."""
    clean = email.strip().lower()
    store = _fake_mysql_store()
    if store is None:
        auth._user_store[clean] = auth._user_store[clean].model_copy(update=changes)
    else:
        store._users[clean] = replace(store._users[clean], **changes)


def delete_profile(email: str) -> None:
    """Remove the user from the active store, as an admin deletion would."""
    clean = email.strip().lower()
    store = _fake_mysql_store()
    if store is None:
        del auth._user_store[clean]
    else:
        del store._users[clean]


def session_token_for(user: AuthenticatedUser, **kwargs) -> str:
    """A session token for ``user`` whose live profile matches the token's claims.

    Session auth re-checks role/status against the stored profile on every
    request, so a bare create_session_token() is rejected as a deleted user.
    """
    save_profile_for(user)
    return create_session_token(user, **kwargs)
