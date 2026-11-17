"""Helpers for tests that sign in with a minted session token.

Profiles go to whichever user store src.auth reads (USER_STORE_BACKEND):
the in-process Redis-less store, or the FakeUserStore the
`user_store_backend` fixture installs for "mysql".
"""

import base64
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from src import auth
from src.auth import AuthenticatedUser, UserProfile, create_session_token
from src.config import settings


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


_RSA_KEY_CACHE: Dict[str, rsa.RSAPrivateKey] = {}


def get_test_rsa_key(name: str = "primary") -> rsa.RSAPrivateKey:
    key = _RSA_KEY_CACHE.get(name)
    if key is None:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        _RSA_KEY_CACHE[name] = key
    return key


def _b64u_int(value: int) -> str:
    length = max(1, (value.bit_length() + 7) // 8)
    return base64.urlsafe_b64encode(value.to_bytes(length, "big")).decode("ascii").rstrip("=")


def rsa_jwk_from_key(private_key: rsa.RSAPrivateKey, kid: str = "test-key-1", alg: str = "RS256") -> Dict[str, Any]:
    nums = private_key.public_key().public_numbers()
    return {
        "kty": "RSA",
        "use": "sig",
        "alg": alg,
        "kid": kid,
        "n": _b64u_int(nums.n),
        "e": _b64u_int(nums.e),
    }


def sign_rs256_jwt(
    payload: Dict[str, Any],
    *,
    kid: Optional[str] = "test-key-1",
    private_key: Optional[rsa.RSAPrivateKey] = None,
    header_overrides: Optional[Dict[str, Any]] = None,
) -> str:
    key = private_key or get_test_rsa_key("primary")
    header: Dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if kid is not None:
        header["kid"] = kid
    if header_overrides:
        header.update(header_overrides)
    h_b64 = base64.urlsafe_b64encode(json.dumps(header, separators=(",", ":")).encode()).decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signing_input = f"{h_b64}.{p_b64}".encode("ascii")
    sig = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    s_b64 = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{h_b64}.{p_b64}.{s_b64}"


def install_test_keycloak_jwks(
    monkeypatch,
    keys: Optional[List[Dict[str, Any]]] = None,
) -> List[str]:
    """Patches auth.fetch_keycloak_jwks to return test JWKS keys and records fetched URLs."""
    auth.clear_keycloak_jwks_cache()
    jwk_list = keys if keys is not None else [rsa_jwk_from_key(get_test_rsa_key("primary"), kid="test-key-1")]
    fetches: List[str] = []

    async def _fake_fetch(jwks_url: str) -> List[Dict[str, Any]]:
        fetches.append(jwks_url)
        return list(jwk_list)

    monkeypatch.setattr(auth, "fetch_keycloak_jwks", _fake_fetch)
    return fetches


def make_keycloak_id_claims(
    *,
    email: str = "curator@mskcc.org",
    email_verified: Any = True,
    sub: str = "kc-sub-123",
    nonce: str = "test-nonce",
    iss: Optional[str] = None,
    aud: Any = None,
    azp: Optional[str] = None,
    iat: Optional[int] = None,
    exp: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = int(time.time())
    realm_url = f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm.strip()}"
    claims: Dict[str, Any] = {
        "iss": iss if iss is not None else realm_url,
        "aud": aud if aud is not None else settings.keycloak_client_id.strip(),
        "sub": sub,
        "nonce": nonce,
        "email": email,
        "email_verified": email_verified,
        "iat": now if iat is None else iat,
        "exp": (now + 300) if exp is None else exp,
    }
    if azp is not None:
        claims["azp"] = azp
    if extra:
        claims.update(extra)
    return claims


def begin_oauth_login(client, provider: str = "google", redirect_to: str = "/") -> Tuple[str, str]:
    """Starts an OAuth login via the browser login endpoint and returns (state, nonce)."""
    url = "/auth/login" if provider == "google" else f"/auth/{provider}/login"
    resp = client.get(url, params={"redirect_to": redirect_to}, follow_redirects=False)
    assert resp.status_code in (302, 303, 307), resp.text
    qs = parse_qs(urlsplit(resp.headers["location"]).query)
    return qs["state"][0], qs["nonce"][0]
