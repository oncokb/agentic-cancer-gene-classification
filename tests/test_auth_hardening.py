"""Tests verifying auth hardening fixes:
1. Google Workspace hd claim enforcement (F2) & strict boolean email_verified.
2. Keycloak email_verified enforcement, issuer & audience/azp validation (F4).
3. Browser-bound, one-use OAuth state + PKCE (F3a/F3b) & ID token nonce check.
4. Secure cookie enforcement in deployed mode (F5).
5. Revocable sessions via session_generation on logout & admin disable (F6).
6. SAML startup guard against insecure parser (F1).
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src import main as main_module
from src.auth import (
    OAUTH_TXN_COOKIE_NAME,
    AuthenticatedUser,
    create_oauth_transaction,
    is_cookie_secure,
    parse_keycloak_user,
    set_session_cookie,
)
from src.config import saml_startup_error, settings
from src.main import app, lifespan
from tests.auth_helpers import save_profile_for, session_token_for

# Every test runs once per USER_STORE_BACKEND
pytestmark = pytest.mark.usefixtures("user_store_backend")

EMAIL = "testuser@mskcc.org"


def _make_jwt(payload: dict) -> str:
    header_b64 = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    )
    return f"{header_b64}.{payload_b64}.fake_sig"


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

    # 1. Login sets cookie and state
    login_resp = client.get("/auth/login", follow_redirects=False)
    assert login_resp.status_code in (302, 303, 307)
    state = parse_qs(urlsplit(login_resp.headers["location"]).query)["state"][0]

    # Mock google code exchange and userinfo with matching hd and email_verified=True
    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok"}),
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
    login_resp = client.get("/auth/login", follow_redirects=False)
    assert login_resp.status_code in (302, 303, 307)
    state = parse_qs(urlsplit(login_resp.headers["location"]).query)["state"][0]

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok"}),
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
    login_resp2 = client.get("/auth/login", follow_redirects=False)
    assert login_resp2.status_code in (302, 303, 307)
    state2 = parse_qs(urlsplit(login_resp2.headers["location"]).query)["state"][0]
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

    login_resp = client.get("/auth/login", follow_redirects=False)
    assert login_resp.status_code in (302, 303, 307)
    state = parse_qs(urlsplit(login_resp.headers["location"]).query)["state"][0]

    userinfo = {
        "email": "curator@mskcc.org",
        "name": "MSK Curator",
    }
    if bad_verified is not None:
        userinfo["email_verified"] = bad_verified

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "valid_tok"}),
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
# 2. Keycloak email_verified, issuer, and audience/azp checks (F4)
# ===========================================================================


@pytest.mark.parametrize("bad_verified", [False, "true", None, 0, 1])
def test_keycloak_parse_user_unverified_email_rejected(auth_settings, bad_verified):
    token_data = {"access_token": "tok"}
    userinfo = {"email": "kcuser@mskcc.org", "preferred_username": "kcuser"}
    if bad_verified is not None:
        userinfo["email_verified"] = bad_verified

    with pytest.raises(HTTPException) as exc_info:
        parse_keycloak_user(token_data, userinfo)
    assert exc_info.value.status_code == 403
    assert "unverified" in exc_info.value.detail.lower()


def test_keycloak_id_token_issuer_validation(auth_settings):
    userinfo = {"email": "kcuser@mskcc.org", "email_verified": True}

    # Wrong issuer
    bad_iss_jwt = _make_jwt({
        "iss": "https://malicious-idp.com/realms/agentic-cancer-gene-classification",
        "aud": "acgc-client",
        "email": "kcuser@mskcc.org",
        "email_verified": True,
    })
    with pytest.raises(HTTPException) as exc_info:
        parse_keycloak_user({"access_token": "tok", "id_token": bad_iss_jwt}, userinfo)
    assert exc_info.value.status_code == 403
    assert "issuer mismatch" in exc_info.value.detail


def test_keycloak_id_token_audience_validation(auth_settings):
    expected_iss = f"{settings.keycloak_url.rstrip('/')}/realms/{settings.keycloak_realm}"
    userinfo = {"email": "kcuser@mskcc.org", "email_verified": True}

    # Wrong audience
    bad_aud_jwt = _make_jwt({
        "iss": expected_iss,
        "aud": "different-client",
        "azp": "different-client",
        "email": "kcuser@mskcc.org",
        "email_verified": True,
    })
    with pytest.raises(HTTPException) as exc_info:
        parse_keycloak_user({"access_token": "tok", "id_token": bad_aud_jwt}, userinfo)
    assert exc_info.value.status_code == 403
    assert "audience mismatch" in exc_info.value.detail

    # Correct issuer and audience
    valid_jwt = _make_jwt({
        "iss": expected_iss,
        "aud": "acgc-client",
        "email": "kcuser@mskcc.org",
        "email_verified": True,
    })
    user = parse_keycloak_user({"access_token": "tok", "id_token": valid_jwt}, userinfo)
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
    txn_id, state, _, verifier, _, nonce = create_oauth_transaction("google", "/")

    # Request callback without agcg_oauth_txn cookie
    resp = client.get("/auth/callback/google", params={"code": "c", "state": state})
    assert resp.status_code == 400
    assert "Missing OAuth transaction cookie" in resp.text


def test_oauth_callback_detects_mismatched_cookie(auth_settings):
    client = TestClient(app)
    # Client has cookie for txn 1
    client.get("/auth/login", follow_redirects=False)

    # Attacker supplies state for txn 2
    txn_id2, state2, _, _, _, _ = create_oauth_transaction("google", "/")
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
    login_resp = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(login_resp.headers["location"]).query)["state"][0]

    monkeypatch.setattr(
        main_module,
        "exchange_google_code",
        AsyncMock(return_value={"access_token": "tok"}),
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
    login_resp = client.get("/auth/login", follow_redirects=False)
    state = parse_qs(urlsplit(login_resp.headers["location"]).query)["state"][0]

    bad_id_token = _make_jwt({
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
