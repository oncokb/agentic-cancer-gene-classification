"""Helpers for tests that sign in with a minted session token."""

from datetime import datetime, timezone

from src import auth
from src.auth import AuthenticatedUser, UserProfile, create_session_token


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
    auth._user_store[profile.email] = profile
    return profile


def session_token_for(user: AuthenticatedUser, **kwargs) -> str:
    """A session token for ``user`` whose live profile matches the token's claims.

    Session auth re-checks role/status against the stored profile on every
    request, so a bare create_session_token() is rejected as a deleted user.
    """
    save_profile_for(user)
    return create_session_token(user, **kwargs)
