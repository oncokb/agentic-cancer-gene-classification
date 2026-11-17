"""Tests verifying auth hardening fixes:
1. Google Workspace hd claim enforcement (F2) & strict boolean email_verified.
2. Keycloak email_verified enforcement, issuer & audience/azp validation (F4).
3. Browser-bound, one-use OAuth state + PKCE (F3a/F3b) & ID token nonce check.
4. Secure cookie enforcement in deployed mode (F5).
5. Revocable sessions via session_generation on logout & admin disable (F6).
6. SAML startup guard against insecure parser (F1).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src import auth as auth_module
from src import main as main_module
from src.auth import (
    OAUTH_TXN_COOKIE_NAME,
    AuthenticatedUser,
    clear_keycloak_jwks_cache,
    create_oauth_state,
    create_oauth_transaction,
    is_cookie_secure,
    parse_keycloak_user,
    set_session_cookie,
)
from src.config import saml_startup_error, settings
from src.main import app, lifespan
from tests.auth_helpers import (
    begin_oauth_login,
    get_test_rsa_key,
    install_test_keycloak_jwks,
    make_keycloak_id_claims,
    rsa_jwk_from_key,
    save_profile_for,
    session_token_for,
    sign_rs256_jwt,
    stored_profile,
)

# Every test runs once per USER_STORE_BACKEND
pytestmark = pytest.mark.usefixtures("user_store_backend")

EMAIL = "testuser@mskcc.org"


@pytest.fixture
def auth_settings(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "auth-hardening-test-secret-key-32b")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "google_client_id", "test-google-client-id")
    monkeypatch.setattr(settings, "keycloak_url", "https://keycloak.oncokb.org")
    monkeypatch.setattr(settings, "keycloak_realm", "agentic-cancer-gene-classification")
    monkeypatch.setattr(settings, "keycloak_client_id", "acgc-client")
    monkeypatch.setattr(settings, "keycloak_allowed_roles", "")
    monkeypatch.setattr(settings, "jit_provisioning_enabled", True)
    monkeypatch.setattr(settings, "jit_require_admin_approval", False)


# ===========================================================================
# 1. Google Workspace hd claim & strict email_verified (F2)
# ===========================================================================


def test_google_callback_allowed_hd_enforced(auth_settings, monkeypatch):
    monkeypatch.setattr(settings, "google_allowed_hd", "mskcc.org")
    client = TestClient(app)

    state, nonce = begin_oauth_login(client, "google", "/")

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(
            return_value={
                "email": "curator@mskcc.org",
                "hd": "mskcc.org",
                "email_verified": True,
                "name": "MSK Curator",
            }
        ),
    )

    cb_resp = client.get(
        "/auth/callback/google",
        params={"code": "auth_code", "state": state},
        follow_redirects=False,
    )
    assert cb_resp.status_code == 303
    assert settings.auth_cookie_name in client.cookies


def test_google_callback_missing_or_mismatched_hd_rejected(auth_settings, monkeypatch):
    monkeypatch.setattr(settings, "google_allowed_hd", "mskcc.org")
    client = TestClient(app)

    # Missing hd
    state, nonce = begin_oauth_login(client, "google", "/")
    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(
            return_value={
                "email": "curator@mskcc.org",
                "email_verified": True,
                "name": "Personal Account",
            }
        ),
    )

    cb_resp = client.get(
        "/auth/callback/google",
        params={"code": "auth_code", "state": state},
        follow_redirects=False,
    )
    assert cb_resp.status_code == 403
    assert "allowed hosted domain" in cb_resp.text

    # Mismatched hd
    state2, nonce2 = begin_oauth_login(client, "google", "/")
    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok", "id_token": sign_rs256_jwt({"nonce": nonce2})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(
            return_value={
                "email": "curator@mskcc.org",
                "hd": "attacker.com",
                "email_verified": True,
                "name": "Wrong HD",
            }
        ),
    )
    cb_resp2 = client.get(
        "/auth/callback/google",
        params={"code": "auth_code", "state": state2},
        follow_redirects=False,
    )
    assert cb_resp2.status_code == 403


@pytest.mark.parametrize("bad_verified", [False, "true", "True", None, 1, 0, ""])
def test_google_callback_unverified_email_rejected(auth_settings, monkeypatch, bad_verified):
    monkeypatch.setattr(settings, "google_allowed_hd", "")
    client = TestClient(app)

    state, nonce = begin_oauth_login(client, "google", "/")

    userinfo = {
        "email": "curator@mskcc.org",
        "name": "MSK Curator",
    }
    if bad_verified is not None:
        userinfo["email_verified"] = bad_verified

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value=userinfo),
    )

    cb_resp = client.get(
        "/auth/callback/google",
        params={"code": "auth_code", "state": state},
        follow_redirects=False,
    )
    assert cb_resp.status_code == 403
    assert "unverified" in cb_resp.text.lower()


# ===========================================================================
# 2. Keycloak ID token JWKS verification, claim validation, & userinfo binding (F4)
# ===========================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_verified", [False, "true", None, 0, 1])
async def test_keycloak_parse_user_unverified_email_rejected(auth_settings, monkeypatch, bad_verified):
    install_test_keycloak_jwks(monkeypatch)
    claims = make_keycloak_id_claims(email="kcuser@mskcc.org", sub="sub-1", email_verified=bad_verified)
    if bad_verified is None:
        claims.pop("email_verified", None)
    id_token = sign_rs256_jwt(claims)
    userinfo = {"sub": "sub-1", "email": "kcuser@mskcc.org", "email_verified": True}

    with pytest.raises(HTTPException) as exc_info:
        await parse_keycloak_user({"access_token": "tok", "id_token": id_token}, userinfo)
    assert exc_info.value.status_code == 403
    assert "unverified" in exc_info.value.detail.lower()


@pytest.mark.asyncio
async def test_keycloak_id_token_issuer_validation(auth_settings, monkeypatch):
    install_test_keycloak_jwks(monkeypatch)
    userinfo = {"sub": "sub-1"}

    # Wrong issuer (even with valid JWKS signature)
    bad_iss_jwt = sign_rs256_jwt(
        make_keycloak_id_claims(
            email="kcuser@mskcc.org",
            sub="sub-1",
            iss="https://malicious-idp.com/realms/agentic-cancer-gene-classification",
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await parse_keycloak_user({"access_token": "tok", "id_token": bad_iss_jwt}, userinfo)
    assert exc_info.value.status_code == 403
    assert "issuer mismatch" in exc_info.value.detail


@pytest.mark.asyncio
async def test_keycloak_id_token_audience_validation(auth_settings, monkeypatch):
    install_test_keycloak_jwks(monkeypatch)
    userinfo = {"sub": "sub-1"}

    # Wrong audience even with matching azp -> 403
    bad_aud_jwt = sign_rs256_jwt(
        make_keycloak_id_claims(
            email="kcuser@mskcc.org",
            sub="sub-1",
            aud="different-client",
            azp="acgc-client",
        )
    )
    with pytest.raises(HTTPException) as exc_info:
        await parse_keycloak_user({"access_token": "tok", "id_token": bad_aud_jwt}, userinfo)
    assert exc_info.value.status_code == 403
    assert "audience mismatch" in exc_info.value.detail

    # Valid issuer, audience, signature, sub, exp, iat, and email_verified=True in ID token
    valid_jwt = sign_rs256_jwt(
        make_keycloak_id_claims(
            email="kcuser@mskcc.org",
            sub="sub-1",
            aud="acgc-client",
        )
    )
    user = await parse_keycloak_user({"access_token": "tok", "id_token": valid_jwt}, userinfo)
    assert user.email == "kcuser@mskcc.org"


# ===========================================================================
# 3. Browser-bound, one-use OAuth state & PKCE (F3a/F3b)
# ===========================================================================


def test_oauth_login_sets_cookie_and_pkce_params(auth_settings):
    client = TestClient(app)

    # Google login
    resp_g = client.get("/auth/login", follow_redirects=False)
    assert resp_g.status_code in (302, 303, 307)
    assert OAUTH_TXN_COOKIE_NAME in resp_g.cookies
    loc_g = resp_g.headers["location"]
    query_g = parse_qs(urlsplit(loc_g).query)
    assert "code_challenge" in query_g
    assert query_g["code_challenge_method"] == ["S256"]
    assert "nonce" in query_g
    assert "state" in query_g

    # Keycloak login
    resp_k = client.get("/auth/keycloak/login", follow_redirects=False)
    assert resp_k.status_code == 303
    assert OAUTH_TXN_COOKIE_NAME in resp_k.cookies
    loc_k = resp_k.headers["location"]
    query_k = parse_qs(urlsplit(loc_k).query)
    assert "code_challenge" in query_k
    assert query_k["code_challenge_method"] == ["S256"]
    assert "nonce" in query_k


def test_oauth_callback_requires_browser_cookie(auth_settings):
    client = TestClient(app)
    # Generate transaction without setting cookie in browser client
    _, state, _, _, _, _ = create_oauth_transaction("google", "/")

    # Request callback without agcg_oauth_txn cookie
    resp = client.get("/auth/callback/google", params={"code": "c", "state": state})
    assert resp.status_code == 400
    assert "Missing OAuth transaction cookie" in resp.text


def test_oauth_callback_detects_mismatched_cookie(auth_settings):
    client = TestClient(app)
    # Client has cookie for txn 1
    client.get("/auth/login", follow_redirects=False)

    # Attacker supplies state for txn 2
    _, state2, _, _, _, _ = create_oauth_transaction("google", "/")
    resp = client.get("/auth/callback/google", params={"code": "c", "state": state2})
    assert resp.status_code == 400
    assert "transaction cookie mismatch" in resp.text


def test_oauth_callback_detects_cross_provider_state(auth_settings):
    client = TestClient(app)
    # Login via Google
    resp = client.get("/auth/login", follow_redirects=False)
    state_google = parse_qs(urlsplit(resp.headers["location"]).query)["state"][0]

    # Try using Google state against Keycloak callback
    resp_bad = client.get("/auth/callback/keycloak", params={"code": "c", "state": state_google})
    assert resp_bad.status_code == 400
    assert "provider mismatch" in resp_bad.text


@pytest.mark.asyncio
async def test_oauth_callback_one_use_replay_prevention(auth_settings, monkeypatch):
    client = TestClient(app)
    state, nonce = begin_oauth_login(client, "google", "/")

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    # First callback succeeds
    resp1 = client.get("/auth/callback/google", params={"code": "c1", "state": state}, follow_redirects=False)
    assert resp1.status_code == 303

    # Replay same state -> 400
    resp2 = client.get("/auth/callback/google", params={"code": "c2", "state": state}, follow_redirects=False)
    assert resp2.status_code == 400


def test_oauth_callback_validates_id_token_nonce(auth_settings, monkeypatch):
    client = TestClient(app)
    state, _ = begin_oauth_login(client, "google", "/")

    bad_id_token = sign_rs256_jwt({
        "iss": "https://accounts.google.com",
        "aud": "client_id",
        "email": "curator@mskcc.org",
        "nonce": "wrong-nonce",
    })
    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "tok", "id_token": bad_id_token}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    resp = client.get("/auth/callback/google", params={"code": "c", "state": state})
    assert resp.status_code == 400
    assert "nonce mismatch" in resp.text


# ===========================================================================
# 4. Secure cookie in deployed mode (F5)
# ===========================================================================


def test_secure_cookie_deployed_mode_with_http_scheme(monkeypatch):
    # In deployed mode, PUBLIC_APP_BASE_URL starts with https://
    monkeypatch.setattr(settings, "public_app_base_url", "https://acgc.oncokb.org")

    class DeployedProxyRequest:
        from starlette.datastructures import URL
        url = URL("http://acgc.oncokb.org/auth/login")

    # In deployed mode, is_cookie_secure must be True regardless of request scheme
    req = DeployedProxyRequest()
    assert is_cookie_secure(req) is True

    from starlette.responses import Response
    resp = Response()
    set_session_cookie(resp, "test-token", request=req)
    assert "Secure" in resp.headers["set-cookie"]


# ===========================================================================
# 5. Revocable sessions (F6)
# ===========================================================================


def test_session_revocation_on_logout(auth_settings, monkeypatch):
    client = TestClient(app)
    user = AuthenticatedUser(
        email="revoketest@mskcc.org",
        name="Revoke Test",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        session_generation=1,
    )
    save_profile_for(user)

    token = session_token_for(user)
    client.cookies.set(settings.auth_cookie_name, token)

    # Token is valid before logout
    resp_before = client.get("/?run=123", follow_redirects=False)
    assert resp_before.status_code == 200

    # User calls logout
    resp_logout = client.post("/auth/logout", follow_redirects=False)
    assert resp_logout.status_code == 303

    # Copied session token reused after logout -> rejected with 401
    client2 = TestClient(app)
    client2.cookies.set(settings.auth_cookie_name, token)
    resp_after = client2.get("/?run=123", follow_redirects=False)
    assert resp_after.status_code == 303
    assert resp_after.headers["location"].startswith("/login")


def test_session_revocation_on_admin_disable(auth_settings):
    if settings.user_store_backend != "mysql":
        pytest.skip("/admin/users changes status on the MySQL user store only")
    client = TestClient(app)
    user = AuthenticatedUser(
        email="disabletest@mskcc.org",
        name="Disable Test",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        session_generation=1,
    )
    save_profile_for(user)

    token = session_token_for(user)
    client.cookies.set(settings.auth_cookie_name, token)

    # Valid initially
    assert client.get("/?run=123", follow_redirects=False).status_code == 200

    # Admin disables user
    admin_user = AuthenticatedUser(
        email="admin@mskcc.org",
        name="Admin",
        domain="mskcc.org",
        role="admin",
        status="active",
        provider="google",
    )
    save_profile_for(admin_user)
    admin_client = TestClient(app)
    admin_client.cookies.set(settings.auth_cookie_name, session_token_for(admin_user))
    disable_resp = admin_client.post(
        "/v1/admin/users/disabletest@mskcc.org/changes",
        json={"status": "disabled", "reason": "offboarded"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert disable_resp.status_code == 200

    # Immediately blocked (403 inactive for API endpoints, 303 redirect for browser page)
    assert client.get("/auth/users").status_code == 403

    # Admin re-enables user
    enable_resp = admin_client.post(
        "/v1/admin/users/disabletest@mskcc.org/changes",
        json={"status": "active", "reason": "re-hired"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert enable_resp.status_code == 200

    # Old token must NOT work even after re-enabling (generation bumped on disable -> 401)
    resp_stale = client.get("/auth/users")
    assert resp_stale.status_code == 401


# ===========================================================================
# 6. SAML startup guard (F1)
# ===========================================================================


def test_saml_startup_guard_prevents_startup(monkeypatch):
    monkeypatch.setattr(settings, "saml_enabled", True)
    err = saml_startup_error(settings)
    assert err is not None
    assert "SAML_ENABLED is true" in err
    assert "auth audit F1" in err

    # Lifespan raises RuntimeError when SAML is enabled
    with pytest.raises(RuntimeError) as exc_info:
        import asyncio
        asyncio.run(lifespan(app).__aenter__())
    assert "SAML_ENABLED is true" in str(exc_info.value)


def test_saml_startup_guard_allows_saml_disabled(monkeypatch):
    monkeypatch.setattr(settings, "saml_enabled", False)
    assert saml_startup_error(settings) is None


# ===========================================================================
# 7. Security review blockers 1–6
# ===========================================================================


@pytest.mark.parametrize("provider", ["google", "keycloak"])
def test_oauth_callback_authenticates_cookie_before_redis_lookup(
    auth_settings, fake_oauth_redis, monkeypatch, provider
):
    """Blocker 1: Redis holds the txn; cookie with right txn prefix but bad/absent
    signature, expired payload, or wrong provider is rejected 400 BEFORE Redis lookup."""
    client = TestClient(app)
    state, nonce = begin_oauth_login(client, provider, "/")
    valid_cookie = client.cookies.get(OAUTH_TXN_COOKIE_NAME)
    assert valid_cookie is not None
    txn_id, payload_b64, _ = valid_cookie.split(".", 2)
    redis_key = f"agcg:oauth_txn:{txn_id}"
    assert redis_key in fake_oauth_redis._store

    # Absent or forged signature -> 400 BEFORE Redis lookup
    attacker = TestClient(app)
    for bad_cookie in (
        txn_id,
        f"{txn_id}.{payload_b64}",
        f"{txn_id}.{payload_b64}.",
        f"{txn_id}.{payload_b64}.tampered_sig",
    ):
        attacker.cookies.clear()
        attacker.cookies.set(OAUTH_TXN_COOKIE_NAME, bad_cookie)
        resp = attacker.get(f"/auth/callback/{provider}", params={"code": "c", "state": state})
        assert resp.status_code == 400
        # Cookie must be authenticated BEFORE Redis GETDEL so attacker cannot burn the victim's txn
        assert redis_key in fake_oauth_redis._store

    # Cookie with valid HMAC signature for the other provider -> 400, Redis untouched
    other_provider = "keycloak" if provider == "google" else "google"
    other_payload = base64.urlsafe_b64encode(
        json.dumps({
            "txn": txn_id,
            "provider": other_provider,
            "nonce": nonce,
            "code_verifier": "v",
            "exp": int(time.time()) + 300,
        }).encode()
    ).decode().rstrip("=")
    sig = hmac.new(
        settings.auth_secret_key.encode(), f"{txn_id}.{other_payload}".encode(), hashlib.sha256
    ).digest()
    wrong_prov_cookie = f"{txn_id}.{other_payload}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"
    attacker.cookies.clear()
    attacker.cookies.set(OAUTH_TXN_COOKIE_NAME, wrong_prov_cookie)
    resp_prov = attacker.get(f"/auth/callback/{provider}", params={"code": "c", "state": state})
    assert resp_prov.status_code == 400
    assert redis_key in fake_oauth_redis._store

    # Expired cookie with valid HMAC signature -> 400, Redis untouched
    exp_payload = base64.urlsafe_b64encode(
        json.dumps({
            "txn": txn_id,
            "provider": provider,
            "nonce": nonce,
            "code_verifier": "v",
            "exp": int(time.time()) - 10,
        }).encode()
    ).decode().rstrip("=")
    exp_sig = hmac.new(
        settings.auth_secret_key.encode(), f"{txn_id}.{exp_payload}".encode(), hashlib.sha256
    ).digest()
    expired_cookie = f"{txn_id}.{exp_payload}.{base64.urlsafe_b64encode(exp_sig).decode().rstrip('=')}"
    attacker.cookies.clear()
    attacker.cookies.set(OAUTH_TXN_COOKIE_NAME, expired_cookie)
    resp_exp = attacker.get(f"/auth/callback/{provider}", params={"code": "c", "state": state})
    assert resp_exp.status_code == 400
    assert redis_key in fake_oauth_redis._store


@pytest.mark.asyncio
async def test_oauth_callback_concurrent_requests_exactly_one_succeeds(auth_settings, monkeypatch):
    """Blocker 2: Two concurrent callbacks with the same state/cookie -> exactly one succeeds."""
    client = TestClient(app)
    state, nonce = begin_oauth_login(client, "google", "/")
    cookie_val = client.cookies.get(OAUTH_TXN_COOKIE_NAME)

    async def slow_exchange(code, redirect_uri, code_verifier=None):
        await asyncio.sleep(0.02)
        return {"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": nonce})}

    monkeypatch.setattr(main_module, "exchange_google_code", slow_exchange)
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={OAUTH_TXN_COOKIE_NAME: cookie_val}
    ) as c1, httpx.AsyncClient(
        transport=transport, base_url="http://testserver", cookies={OAUTH_TXN_COOKIE_NAME: cookie_val}
    ) as c2:
        r1, r2 = await asyncio.gather(
            c1.get(
                "/auth/callback/google",
                params={"code": "c1", "state": state},
                follow_redirects=False,
            ),
            c2.get(
                "/auth/callback/google",
                params={"code": "c2", "state": state},
                follow_redirects=False,
            ),
        )

    statuses = sorted([r1.status_code, r2.status_code])
    assert statuses == [303, 400]
    succeeded = r1 if r1.status_code == 303 else r2
    rejected = r2 if r1.status_code == 303 else r1
    assert settings.auth_cookie_name in succeeded.cookies
    assert settings.auth_cookie_name not in rejected.cookies


def test_oauth_callback_refuses_when_redis_unavailable(auth_settings, fake_oauth_redis, monkeypatch):
    """Blocker 2: Redis down or unconfigured refuses sign-in (fail closed, 503, no login)."""
    client = TestClient(app)
    state, nonce = begin_oauth_login(client, "google", "/")

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    # 1. Redis connection error during callback
    fake_oauth_redis.fail = True
    resp_cb = client.get("/auth/callback/google", params={"code": "c", "state": state}, follow_redirects=False)
    assert resp_cb.status_code == 503
    assert settings.auth_cookie_name not in resp_cb.cookies

    # 2. Redis connection error during login initiation
    resp_login = client.get("/auth/login", follow_redirects=False)
    assert resp_login.status_code == 503

    # 3. Redis unconfigured (returns None) fails closed too
    fake_oauth_redis.fail = False

    async def no_redis():
        return None

    monkeypatch.setattr(auth_module, "_oauth_txn_redis", no_redis)
    resp_none_login = client.get("/auth/login", follow_redirects=False)
    assert resp_none_login.status_code == 503
    resp_none_cb = client.get("/auth/callback/google", params={"code": "c", "state": state}, follow_redirects=False)
    assert resp_none_cb.status_code == 503
    assert settings.auth_cookie_name not in resp_none_cb.cookies


def test_oauth_callback_refuses_on_redis_error_after_getdel(auth_settings, fake_oauth_redis, monkeypatch):
    """Blocker 2: Redis error right after GETDEL fails closed (503, no login) and does not replay."""
    client = TestClient(app)
    state, nonce = begin_oauth_login(client, "google", "/")

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    fake_oauth_redis.fail_after_getdel = True
    resp1 = client.get("/auth/callback/google", params={"code": "c1", "state": state}, follow_redirects=False)
    assert resp1.status_code == 503
    assert settings.auth_cookie_name not in resp1.cookies

    # Even once Redis recovers, GETDEL deleted the txn so replay is also rejected
    fake_oauth_redis.fail_after_getdel = False
    resp2 = client.get("/auth/callback/google", params={"code": "c2", "state": state}, follow_redirects=False)
    assert resp2.status_code == 400
    assert settings.auth_cookie_name not in resp2.cookies


def test_oauth_callback_replay_from_second_client_with_copied_cookie_refused(auth_settings, monkeypatch):
    """Blocker 2: A second client replaying the copied state + cookie is refused."""
    client1 = TestClient(app)
    state, nonce = begin_oauth_login(client1, "google", "/")
    copied_cookie = client1.cookies.get(OAUTH_TXN_COOKIE_NAME)

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": nonce})}),
    )
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    resp1 = client1.get("/auth/callback/google", params={"code": "c1", "state": state}, follow_redirects=False)
    assert resp1.status_code == 303
    assert settings.auth_cookie_name in resp1.cookies

    client2 = TestClient(app)
    client2.cookies.set(OAUTH_TXN_COOKIE_NAME, copied_cookie)
    resp2 = client2.get("/auth/callback/google", params={"code": "c2", "state": state}, follow_redirects=False)
    assert resp2.status_code == 400
    assert settings.auth_cookie_name not in resp2.cookies


@pytest.mark.asyncio
async def test_keycloak_jwks_caching_and_key_rotation(auth_settings, monkeypatch):
    """Blocker 3: Keycloak JWKS keys are cached and rotated keys trigger refresh."""
    clear_keycloak_jwks_cache()
    k1 = get_test_rsa_key("primary")
    k2 = get_test_rsa_key("rotated")
    active_jwks = {"keys": [rsa_jwk_from_key(k1, kid="key-1")]}
    http_calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls.append(str(request.url))
        return httpx.Response(200, json=active_jwks)

    real_async_client = httpx.AsyncClient
    monkeypatch.setattr(
        auth_module.httpx,
        "AsyncClient",
        lambda **kwargs: real_async_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    tok1 = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="key-1", private_key=k1)
    user1 = await parse_keycloak_user({"access_token": "tok", "id_token": tok1}, {"sub": "s1"})
    assert user1.email == "curator@mskcc.org"
    assert len(http_calls) == 1

    # Second verification uses cached JWKS without another HTTP request
    tok1b = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="key-1", private_key=k1)
    user1b = await parse_keycloak_user({"access_token": "tok", "id_token": tok1b}, {"sub": "s1"})
    assert user1b.email == "curator@mskcc.org"
    assert len(http_calls) == 1

    # Rotate keys at Keycloak JWKS endpoint to key-2
    active_jwks["keys"] = [rsa_jwk_from_key(k2, kid="key-2")]
    tok2 = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="key-2", private_key=k2)
    user2 = await parse_keycloak_user({"access_token": "tok", "id_token": tok2}, {"sub": "s1"})
    assert user2.email == "curator@mskcc.org"
    assert len(http_calls) == 2


@pytest.mark.asyncio
async def test_keycloak_rejects_unsigned_and_wrongly_signed_tokens(auth_settings, monkeypatch):
    """Blocker 3: Unsigned (alg=none), malformed, HS256, or wrongly signed ID tokens -> 403."""
    install_test_keycloak_jwks(monkeypatch)
    claims = make_keycloak_id_claims(email="curator@mskcc.org", sub="s1")

    # 1. alg: none token
    h_none = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    p_b64 = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    alg_none_tok = f"{h_none}.{p_b64}."

    # 2. Signed with untrusted private key (attacker) even matching kid
    wrong_key_tok = sign_rs256_jwt(claims, kid="test-key-1", private_key=get_test_rsa_key("attacker"))

    # 3. Unknown kid
    unknown_kid_tok = sign_rs256_jwt(claims, kid="unknown-kid")

    # 4. HS256 token
    h_hs = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    hs_sig = base64.urlsafe_b64encode(b"hs256sig").decode().rstrip("=")
    hs256_tok = f"{h_hs}.{p_b64}.{hs_sig}"

    for bad_tok in (alg_none_tok, wrong_key_tok, unknown_kid_tok, hs256_tok, "not-a-jwt", "a.b"):
        with pytest.raises(HTTPException) as exc_info:
            await parse_keycloak_user({"access_token": "tok", "id_token": bad_tok}, {"sub": "s1"})
        assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_keycloak_rejects_missing_or_invalid_claims(auth_settings, monkeypatch):
    """Blocker 3: Missing iss/aud, wrong aud with matching azp, wrong azp, expired/future exp/iat -> 403."""
    install_test_keycloak_jwks(monkeypatch)
    now = int(time.time())

    bad_claim_sets = [
        # Missing iss
        {k: v for k, v in make_keycloak_id_claims().items() if k != "iss"},
        # Missing aud
        {k: v for k, v in make_keycloak_id_claims().items() if k != "aud"},
        # Wrong aud even with matching azp -> azp must NOT override wrong aud
        make_keycloak_id_claims(aud="other-client", azp="acgc-client"),
        # Correct aud list with wrong azp
        make_keycloak_id_claims(aud=["acgc-client", "other"], azp="other"),
        # Expired exp
        make_keycloak_id_claims(exp=now - 300),
        # Missing exp
        {k: v for k, v in make_keycloak_id_claims().items() if k != "exp"},
        # Future iat
        make_keycloak_id_claims(iat=now + 600),
        # Missing iat
        {k: v for k, v in make_keycloak_id_claims().items() if k != "iat"},
        # Missing sub
        {k: v for k, v in make_keycloak_id_claims().items() if k != "sub"},
        # Missing email
        {k: v for k, v in make_keycloak_id_claims().items() if k != "email"},
    ]

    for bad_claims in bad_claim_sets:
        tok = sign_rs256_jwt(bad_claims)
        with pytest.raises(HTTPException) as exc_info:
            await parse_keycloak_user({"access_token": "tok", "id_token": tok}, {"sub": "kc-sub-123"})
        assert exc_info.value.status_code == 403, f"Expected 403 for claims {bad_claims}"


@pytest.mark.asyncio
async def test_keycloak_userinfo_sub_mismatch_and_no_claim_mixing(auth_settings, monkeypatch):
    """Blocker 3: userinfo.sub != id_token.sub, missing userinfo.sub, and email_verified
    only in the other source -> all 403; email & email_verified come only from ID token."""
    install_test_keycloak_jwks(monkeypatch)

    valid_id_tok = sign_rs256_jwt(
        make_keycloak_id_claims(email="curator@mskcc.org", sub="sub-id-1", email_verified=True)
    )

    # 1. userinfo sub mismatch -> 403
    with pytest.raises(HTTPException) as exc1:
        await parse_keycloak_user({"id_token": valid_id_tok}, {"sub": "sub-other", "email_verified": True})
    assert exc1.value.status_code == 403

    # 2. userinfo missing sub -> 403
    with pytest.raises(HTTPException) as exc2:
        await parse_keycloak_user({"id_token": valid_id_tok}, {"email": "curator@mskcc.org", "email_verified": True})
    assert exc2.value.status_code == 403

    # 3. email_verified=True ONLY in userinfo (missing in id_token) -> 403
    no_verified_claims = make_keycloak_id_claims(email="curator@mskcc.org", sub="sub-id-1")
    no_verified_claims.pop("email_verified")
    tok_no_verified = sign_rs256_jwt(no_verified_claims)
    with pytest.raises(HTTPException) as exc3:
        await parse_keycloak_user({"id_token": tok_no_verified}, {"sub": "sub-id-1", "email_verified": True})
    assert exc3.value.status_code == 403

    # 4. email_verified=True in id_token, but False in userinfo -> 403
    with pytest.raises(HTTPException) as exc4:
        await parse_keycloak_user({"id_token": valid_id_tok}, {"sub": "sub-id-1", "email_verified": False})
    assert exc4.value.status_code == 403

    # 5. email ONLY in userinfo (missing in id_token) -> 403 (never mix email from userinfo)
    no_email_claims = make_keycloak_id_claims(sub="sub-id-1", email_verified=True)
    no_email_claims.pop("email")
    tok_no_email = sign_rs256_jwt(no_email_claims)
    with pytest.raises(HTTPException) as exc5:
        await parse_keycloak_user(
            {"id_token": tok_no_email},
            {"sub": "sub-id-1", "email": "curator@mskcc.org", "email_verified": True},
        )
    assert exc5.value.status_code == 403


def test_google_callback_requires_valid_id_token_and_exact_nonce(auth_settings, monkeypatch):
    """Blocker 4: Google callback rejects missing id_token, malformed id_token, and missing/mismatched nonce."""
    client = TestClient(app)
    monkeypatch.setattr(
        main_module,
        "get_google_user_info",
        AsyncMock(return_value={"email": "curator@mskcc.org", "email_verified": True}),
    )

    alg_none_tok = (
        base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(b'{"nonce":"x"}').decode().rstrip("=")
        + ".sig"
    )
    bad_tokens = [
        {"access_token": "tok"},  # missing id_token
        {"access_token": "tok", "id_token": ""},  # empty id_token
        {"access_token": "tok", "id_token": "not.a.valid_b64!"},  # malformed
        {"access_token": "tok", "id_token": "a.b"},  # missing 3rd part
        {"access_token": "tok", "id_token": alg_none_tok},  # alg=none
        {"access_token": "tok", "id_token": sign_rs256_jwt({"email": "curator@mskcc.org"})},  # missing nonce
        {"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": "wrong-nonce"})},  # mismatched nonce
    ]

    for tok_payload in bad_tokens:
        state, _ = begin_oauth_login(client, "google", "/")
        monkeypatch.setattr(main_module, "exchange_google_code", AsyncMock(return_value=tok_payload))
        resp = client.get("/auth/callback/google", params={"code": "c", "state": state}, follow_redirects=False)
        assert resp.status_code == 400
        assert settings.auth_cookie_name not in resp.cookies


def test_keycloak_callback_requires_valid_id_token_and_exact_nonce(auth_settings, monkeypatch):
    """Blocker 4: Keycloak callback rejects missing/malformed id_token and missing/mismatched nonce."""
    install_test_keycloak_jwks(monkeypatch)
    client = TestClient(app)
    monkeypatch.setattr(
        main_module,
        "get_keycloak_user_info",
        AsyncMock(return_value={"sub": "kc-sub-123"}),
    )

    no_nonce_claims = make_keycloak_id_claims(email="curator@mskcc.org", sub="kc-sub-123")
    no_nonce_claims.pop("nonce")
    bad_tokens = [
        {"access_token": "tok"},  # missing id_token
        {"access_token": "tok", "id_token": "malformed"},  # malformed id_token
        {"access_token": "tok", "id_token": sign_rs256_jwt(no_nonce_claims)},  # missing nonce
        {
            "access_token": "tok",
            "id_token": sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", nonce="wrong-nonce")),
        },
    ]

    for tok_payload in bad_tokens:
        state, _ = begin_oauth_login(client, "keycloak", "/")
        monkeypatch.setattr(main_module, "exchange_keycloak_code", AsyncMock(return_value=tok_payload))
        resp = client.get("/auth/callback/keycloak", params={"code": "c", "state": state}, follow_redirects=False)
        assert resp.status_code in (400, 403)
        assert settings.auth_cookie_name not in resp.cookies


@pytest.mark.parametrize("provider", ["google", "keycloak"])
def test_oauth_callbacks_reject_legacy_state_without_txn_and_provider(auth_settings, provider):
    """Blocker 5: Any signed state lacking txn and provider is rejected with 400."""
    client = TestClient(app)
    legacy_state = create_oauth_state(redirect_to="/")
    no_provider_state = create_oauth_state(redirect_to="/", txn="some-txn")
    no_txn_state = create_oauth_state(redirect_to="/", provider=provider)

    for bad_state in (legacy_state, no_provider_state, no_txn_state):
        resp = client.get(f"/auth/callback/{provider}", params={"code": "c", "state": bad_state}, follow_redirects=False)
        assert resp.status_code == 400


def test_logout_generation_bump_failure_returns_error_and_clears_cookie(auth_settings, monkeypatch):
    """Blocker 6: If session generation bump fails on logout, return 503 error, clear cookie,
    and tell the user revocation failed (do not claim 303 success)."""
    client = TestClient(app)
    user = AuthenticatedUser(
        email="logoutfail@mskcc.org",
        name="Logout Fail",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        session_generation=1,
    )
    token = session_token_for(user)
    client.cookies.set(settings.auth_cookie_name, token)

    async def broken_bump(email: str, *, expected_generation=None) -> int:
        raise auth_module.UserStoreUnavailable("store unavailable")

    monkeypatch.setattr(main_module, "bump_session_generation", broken_bump)

    resp = client.post("/auth/logout", follow_redirects=False)
    assert resp.status_code == 503
    assert "revocation failed" in resp.text.lower()
    assert any(
        settings.auth_cookie_name in c and ("max-age=0" in c.lower() or '""' in c)
        for c in resp.headers.get_list("set-cookie")
    )


@pytest.mark.redis_user_store_only
def test_logout_redis_bump_failure_fails_loudly_and_clears_cookie(auth_settings, monkeypatch):
    """Blocker 6: Redis error during logout revocation must not swallow the failure."""
    client = TestClient(app)
    user = AuthenticatedUser(
        email="logoutredis@mskcc.org",
        name="Logout Redis",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        session_generation=1,
    )
    token = session_token_for(user)

    class _FailingPipeline:
        def __init__(self, redis):
            self.redis = redis
            self.ops = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def watch(self, *keys):
            return None

        async def unwatch(self):
            return None

        async def get(self, key):
            return await self.redis.get(key)

        def multi(self):
            return self

        def set(self, key, val, ex=None):
            self.ops.append((key, val))
            return self

        async def execute(self):
            for key, val in self.ops:
                await self.redis.set(key, val)

    class FailingRedis:
        async def get(self, key):
            return stored_profile(user.email).model_dump_json()

        async def set(self, key, val):
            raise ConnectionError("redis write down")

        def pipeline(self, transaction=True):
            return _FailingPipeline(self)

    async def get_failing_redis():
        return FailingRedis()

    monkeypatch.setattr(auth_module, "_profile_store_redis", get_failing_redis)
    client.cookies.set(settings.auth_cookie_name, token)
    resp = client.post("/auth/logout", headers={"Accept": "application/json"}, follow_redirects=False)
    assert resp.status_code == 503
    assert "revocation failed" in resp.json()["detail"].lower()
    assert any(
        settings.auth_cookie_name in c and ("max-age=0" in c.lower() or '""' in c)
        for c in resp.headers.get_list("set-cookie")
    )


def test_logout_repeat_with_already_revoked_token_is_noop(auth_settings):
    """Blocker 6: Comparing token gen against current profile prevents repeat logout bumps."""
    client = TestClient(app)
    user = AuthenticatedUser(
        email="repeatlogout@mskcc.org",
        name="Repeat Logout",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        session_generation=1,
    )
    token = session_token_for(user)

    # First logout bumps generation 1 -> 2
    client.cookies.set(settings.auth_cookie_name, token)
    resp1 = client.post("/auth/logout", follow_redirects=False)
    assert resp1.status_code == 303
    assert stored_profile(user.email).session_generation == 2

    # Repeat logout with the already-revoked gen=1 token -> does nothing, stays gen=2
    client2 = TestClient(app)
    client2.cookies.set(settings.auth_cookie_name, token)
    resp2 = client2.post("/auth/logout", follow_redirects=False)
    assert resp2.status_code == 303
    assert stored_profile(user.email).session_generation == 2
    assert any(
        settings.auth_cookie_name in c and ("max-age=0" in c.lower() or '""' in c)
        for c in resp2.headers.get_list("set-cookie")
    )


# ===========================================================================
# 7. Bounded & coalesced JWKS refresh, atomic logout race, & OAuth Redis check
# ===========================================================================


@pytest.mark.asyncio
async def test_keycloak_jwks_coalesces_concurrent_unknown_kid_requests(auth_settings, monkeypatch):
    """Blocker 1: N concurrent unknown-kid tokens trigger exactly 1 JWKS fetch (cold & warm)."""
    clear_keycloak_jwks_cache()
    k1 = get_test_rsa_key("primary")
    jwks_doc = {"keys": [rsa_jwk_from_key(k1, kid="key-1")]}
    fetch_count = 0

    async def fake_fetch(jwks_url: str):
        nonlocal fetch_count
        fetch_count += 1
        await asyncio.sleep(0.02)
        return jwks_doc["keys"]

    monkeypatch.setattr(auth_module, "fetch_keycloak_jwks", fake_fetch)
    monkeypatch.setattr(settings, "keycloak_jwks_refresh_min_interval_seconds", 60)

    # Part A: Cold cache + 8 concurrent unknown-kid tokens -> exactly 1 fetch, all rejected 403
    bad_tokens = [
        sign_rs256_jwt(
            make_keycloak_id_claims(email="attacker@mskcc.org", sub=f"s-{i}"),
            kid=f"unknown-{i}",
            private_key=k1,
        )
        for i in range(8)
    ]

    async def _attempt(tok: str):
        with pytest.raises(HTTPException) as exc:
            await parse_keycloak_user({"access_token": "tok", "id_token": tok}, {"sub": "s-0"})
        return exc.value.status_code

    statuses = await asyncio.gather(*(_attempt(tok) for tok in bad_tokens))
    assert all(code == 403 for code in statuses)
    assert fetch_count == 1

    # Part B: Warm cache (valid login, no prior failure refresh) + 8 concurrent unknown-kid -> 1 refresh
    clear_keycloak_jwks_cache()
    fetch_count = 0
    valid_tok = sign_rs256_jwt(
        make_keycloak_id_claims(email="curator@mskcc.org", sub="s-valid"),
        kid="key-1",
        private_key=k1,
    )
    valid_user = await parse_keycloak_user({"access_token": "tok", "id_token": valid_tok}, {"sub": "s-valid"})
    assert valid_user.email == "curator@mskcc.org"
    assert fetch_count == 1

    statuses_warm = await asyncio.gather(*(_attempt(tok) for tok in bad_tokens))
    assert all(code == 403 for code in statuses_warm)
    assert fetch_count == 2  # +1 coalesced refresh for all 8 concurrent unknown-kid attempts


@pytest.mark.asyncio
async def test_keycloak_jwks_rate_limits_failures_and_allows_after_interval(auth_settings, monkeypatch):
    """Blocker 1: Repeated failures within min interval trigger 0 extra fetches; after interval, 1 more."""
    clear_keycloak_jwks_cache()
    clock = [1000.0]
    monkeypatch.setattr(auth_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(settings, "keycloak_jwks_refresh_min_interval_seconds", 60)

    k1 = get_test_rsa_key("primary")
    k2 = get_test_rsa_key("rotated")
    active_keys = [rsa_jwk_from_key(k1, kid="key-1")]
    fetches = 0

    async def fake_fetch(jwks_url: str):
        nonlocal fetches
        fetches += 1
        return list(active_keys)

    monkeypatch.setattr(auth_module, "fetch_keycloak_jwks", fake_fetch)

    # Warm cache with valid key-1
    tok1 = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="key-1", private_key=k1)
    await parse_keycloak_user({"access_token": "tok", "id_token": tok1}, {"sub": "s1"})
    assert fetches == 1

    # Unknown kid triggers 1 forced refresh at t=1001
    clock[0] = 1001.0
    bad_kid = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="missing-kid", private_key=k1)
    with pytest.raises(HTTPException) as exc1:
        await parse_keycloak_user({"access_token": "tok", "id_token": bad_kid}, {"sub": "s1"})
    assert exc1.value.status_code == 403
    assert fetches == 2

    # Repeated failures (unknown kid and bad signature) within 60s -> 403 WITHOUT extra fetch
    bad_sig_parts = tok1.split(".")
    bad_sig_tok = f"{bad_sig_parts[0]}.{bad_sig_parts[1]}.AAAA_invalid_signature"
    for dt in (10.0, 30.0, 59.0):
        clock[0] = 1001.0 + dt
        for tok in (bad_kid, bad_sig_tok):
            with pytest.raises(HTTPException) as exc_rep:
                await parse_keycloak_user({"access_token": "tok", "id_token": tok}, {"sub": "s1"})
            assert exc_rep.value.status_code == 403
    assert fetches == 2

    # Rotate keys after interval expires -> allows 1 more refresh and accepts rotated token
    active_keys[:] = [rsa_jwk_from_key(k2, kid="key-2")]
    clock[0] = 1061.0
    tok2 = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="key-2", private_key=k2)
    user2 = await parse_keycloak_user({"access_token": "tok", "id_token": tok2}, {"sub": "s1"})
    assert user2.email == "curator@mskcc.org"
    assert fetches == 3


@pytest.mark.asyncio
async def test_keycloak_jwks_coalesces_concurrent_key_rotation(auth_settings, monkeypatch):
    """Blocker 1: N concurrent tokens for a rotated key coalesce onto 1 refresh and all succeed."""
    clear_keycloak_jwks_cache()
    k1 = get_test_rsa_key("primary")
    k2 = get_test_rsa_key("rotated")
    active_keys = [rsa_jwk_from_key(k1, kid="key-1")]
    fetches = 0

    async def fake_fetch(jwks_url: str):
        nonlocal fetches
        fetches += 1
        await asyncio.sleep(0.02)
        return list(active_keys)

    monkeypatch.setattr(auth_module, "fetch_keycloak_jwks", fake_fetch)

    tok1 = sign_rs256_jwt(make_keycloak_id_claims(email="curator@mskcc.org", sub="s1"), kid="key-1", private_key=k1)
    await parse_keycloak_user({"access_token": "tok", "id_token": tok1}, {"sub": "s1"})
    assert fetches == 1

    active_keys[:] = [rsa_jwk_from_key(k2, kid="key-2")]
    rotated_tokens = [
        sign_rs256_jwt(make_keycloak_id_claims(email=f"u{i}@mskcc.org", sub=f"s-{i}"), kid="key-2", private_key=k2)
        for i in range(6)
    ]
    users = await asyncio.gather(
        *(parse_keycloak_user({"access_token": "tok", "id_token": tok}, {"sub": f"s-{i}"}) for i, tok in enumerate(rotated_tokens))
    )
    assert len(users) == 6
    assert fetches == 2


@pytest.mark.asyncio
async def test_logout_concurrent_requests_with_interleaved_login_keep_new_session_valid(
    auth_settings, monkeypatch
):
    """Blocker 2: Two concurrent logouts with the same gen=1 token interleaved with a new login
    bump session_generation exactly once; the new gen=2 session remains valid."""
    email = "racelogout@mskcc.org"
    user_v1 = AuthenticatedUser(
        email=email,
        name="Race Logout",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        session_generation=1,
    )
    save_profile_for(user_v1)
    token_v1 = session_token_for(user_v1)

    real_bump = main_module.bump_session_generation
    first_entered = asyncio.Event()
    release_second = asyncio.Event()
    call_index = 0

    async def coordinated_bump(target_email: str, *, expected_generation=None):
        nonlocal call_index
        call_index += 1
        my_idx = call_index
        if my_idx == 1:
            res = await real_bump(target_email, expected_generation=expected_generation)
            first_entered.set()
            return res
        await first_entered.wait()
        await release_second.wait()
        return await real_bump(target_email, expected_generation=expected_generation)

    monkeypatch.setattr(main_module, "bump_session_generation", coordinated_bump)

    async with (
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={settings.auth_cookie_name: token_v1},
        ) as ac1,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
            cookies={settings.auth_cookie_name: token_v1},
        ) as ac2,
    ):
        t1 = asyncio.create_task(ac1.post("/auth/logout"))
        t2 = asyncio.create_task(ac2.post("/auth/logout"))
        await first_entered.wait()
        assert stored_profile(email).session_generation == 2

        # User logs in again between Logout 1 and Logout 2 -> issued new session with gen=2
        user_v2 = await auth_module.provision_or_update_user(
            email=email,
            name="Race Logout",
            domain="mskcc.org",
            provider="google",
        )
        assert user_v2.session_generation == 2
        token_v2 = auth_module.create_session_token(
            AuthenticatedUser(
                email=user_v2.email,
                name=user_v2.name,
                domain=user_v2.domain,
                role=user_v2.role,
                status=user_v2.status,
                provider=user_v2.provider,
                session_generation=user_v2.session_generation,
            )
        )

        # Let Logout 2 finish its conditional bump attempt with expected_generation=1
        release_second.set()
        r1, r2 = await asyncio.gather(t1, t2)
        assert r1.status_code == 303
        assert r2.status_code == 303
        assert call_index == 2

        # Generation was bumped strictly once (remains 2), and new login session stays valid
        assert stored_profile(email).session_generation == 2
        ac1.cookies.set(settings.auth_cookie_name, token_v2)
        status_new = await ac1.get("/auth/me")
        assert status_new.status_code == 200
        assert status_new.json()["authenticated"] is True

        ac1.cookies.set(settings.auth_cookie_name, token_v1)
        status_old = await ac1.get("/auth/me")
        assert status_old.status_code == 200
        assert status_old.json()["authenticated"] is False


@pytest.mark.redis_user_store_only
@pytest.mark.asyncio
async def test_logout_redis_watch_multi_race_aborts_on_concurrent_login(
    auth_settings, _auth_profiles_in_memory, monkeypatch
):
    """Blocker 2 (Redis): WATCH/MULTI aborts cleanly when generation advances concurrently."""
    from tests.test_session_live_status import FakeAuthRedis

    fake_redis = FakeAuthRedis()
    email = "redisrace@mskcc.org"
    initial = auth_module.UserProfile(
        email=email,
        name="Redis Race",
        domain="mskcc.org",
        role="curator",
        status="active",
        provider="google",
        first_login_at="2026-01-01T00:00:00Z",
        last_login_at="2026-01-01T00:00:00Z",
        session_generation=1,
    )
    await fake_redis.set(f"agcg:user:{email}", initial.model_dump_json())

    class InterleavingRedis(FakeAuthRedis):
        def __init__(self, backing: FakeAuthRedis):
            self._backing = backing
            self._injected = False

        async def get(self, key):
            return await self._backing.get(key)

        async def set(self, key, value, ex=None):
            return await self._backing.set(key, value, ex=ex)

        def pipeline(self, transaction=True):
            real_pipe = self._backing.pipeline(transaction=transaction)
            outer = self

            class _RacePipe:
                async def __aenter__(self):
                    await real_pipe.__aenter__()
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return await real_pipe.__aexit__(exc_type, exc, tb)

                async def watch(self, *keys):
                    return await real_pipe.watch(*keys)

                async def unwatch(self):
                    return await real_pipe.unwatch()

                async def get(self, key):
                    return await real_pipe.get(key)

                def multi(self):
                    if not outer._injected:
                        outer._injected = True
                        # Concurrent login/first logout advances gen 1 -> 2 before EXEC
                        updated = initial.model_copy(update={"session_generation": 2})
                        outer._backing.data[f"agcg:user:{email}"] = updated.model_dump_json()
                        outer._backing._versions[f"agcg:user:{email}"] += 1
                    return real_pipe.multi()

                def set(self, key, value, ex=None):
                    return real_pipe.set(key, value, ex=ex)

                async def execute(self):
                    return await real_pipe.execute()

            return _RacePipe()

    race_redis = InterleavingRedis(fake_redis)

    async def get_race_redis():
        return race_redis

    monkeypatch.setattr(auth_module, "_profile_store_redis", get_race_redis)
    monkeypatch.setattr(settings, "user_store_backend", "redis")

    result = await auth_module.bump_session_generation(email, expected_generation=1)
    assert result is None
    stored_after = auth_module.UserProfile.model_validate_json(fake_redis.data[f"agcg:user:{email}"])
    assert stored_after.session_generation == 2


def test_oauth_txn_store_uses_real_accessor_with_mysql_and_redis_url(
    auth_settings, _auth_profiles_in_memory, monkeypatch
):
    """Operational check 4: Using the REAL _oauth_txn_redis accessor (no override), when
    USER_STORE_BACKEND=mysql and REDIS_URL is set, OAuth transactions get a working Redis client
    and sign-in succeeds end-to-end."""
    import redis.asyncio as redis_asyncio
    from src.pipeline.user_store_fake import FakeUserStore
    from tests.test_session_live_status import FakeAuthRedis

    fake_redis = FakeAuthRedis()
    from_url_calls = []

    def fake_from_url(url: str, **kwargs):
        from_url_calls.append((url, kwargs))
        return fake_redis

    # Restore REAL auth Redis accessors (no override of _oauth_txn_redis)
    monkeypatch.setattr(auth_module, "_get_auth_redis_client", _auth_profiles_in_memory["_get_auth_redis_client"])
    monkeypatch.setattr(auth_module, "_profile_store_redis", _auth_profiles_in_memory["_profile_store_redis"])
    monkeypatch.setattr(auth_module, "_oauth_txn_redis", _auth_profiles_in_memory["_oauth_txn_redis"])
    monkeypatch.setattr(auth_module, "_redis_client", None)

    monkeypatch.setattr(redis_asyncio, "from_url", fake_from_url)
    monkeypatch.setattr(settings, "user_store_backend", "mysql")
    monkeypatch.setattr(settings, "redis_url", "redis://oauth-check-redis:6379/2")

    mysql_fake = FakeUserStore()
    monkeypatch.setattr(auth_module, "_mysql_user_store", mysql_fake)
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = mysql_fake
    try:
        client = TestClient(app)
        login_resp = client.get("/auth/login", follow_redirects=False)
        assert login_resp.status_code in (302, 303, 307)
        assert any(k.startswith("agcg:oauth_txn:") for k in fake_redis.data)
        assert from_url_calls == [("redis://oauth-check-redis:6379/2", {"decode_responses": True})]

        login_query = parse_qs(urlsplit(login_resp.headers["location"]).query)
        state = login_query["state"][0]
        nonce = login_query["nonce"][0]

        async def fake_exchange(code, redirect_uri, code_verifier=None):
            return {"access_token": "tok", "id_token": sign_rs256_jwt({"nonce": nonce})}

        async def fake_userinfo(access_token):
            return {
                "email": "oauth.real@mskcc.org",
                "name": "Real OAuth User",
                "email_verified": True,
            }

        monkeypatch.setattr(main_module, "exchange_google_code", fake_exchange)
        monkeypatch.setattr(main_module, "get_google_user_info", fake_userinfo)

        cb_resp = client.get("/auth/callback/google", params={"code": "c1", "state": state}, follow_redirects=False)
        assert cb_resp.status_code == 303
        assert not any(k.startswith("agcg:oauth_txn:") for k in fake_redis.data)

        status_resp = client.get("/auth/me")
        assert status_resp.status_code == 200
        assert status_resp.json()["authenticated"] is True
        assert status_resp.json()["user"]["email"] == "oauth.real@mskcc.org"
    finally:
        if previous is None:
            delattr(app.state, "run_store")
        else:
            app.state.run_store = previous
