"""Session tokens are authorized against the *current* user profile.

A session token carries the role/status the user had at login. These tests
check that demoting, disabling, or deleting the profile takes effect on an
already-issued session, and that an unreadable profile store fails closed.

Self-contained (no tests.auth_helpers) so each case can be run against a
checkout without the live check to confirm it fails there.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src import auth as auth_module
from src.api_keys import ApiKeyRecord, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser, UserProfile, create_session_token
from src.config import settings
from src.main import app
from tests.test_api_keys import FakeStore

PROTECTED = "/v1/annotate/does-not-exist"  # require_auth; 404 once authenticated
ADMIN_ONLY = "/auth/users"  # require_admin
PENDING_DETAIL = "Your account is pending administrator approval. Please contact your AGCG administrator."
EMAIL = "curator@mskcc.org"


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "session-live-status-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth_module, "_redis_client", None)
    monkeypatch.setattr(auth_module, "_user_store", {})
    fake = FakeStore()
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = fake
    yield fake
    if previous is None:
        del app.state.run_store
    else:
        app.state.run_store = previous


def _set_profile(email: str = EMAIL, **overrides) -> UserProfile:
    now_iso = datetime.now(timezone.utc).isoformat()
    data = dict(
        email=email,
        name="Curator",
        domain=email.split("@")[-1],
        role="curator",
        status="active",
        first_login_at=now_iso,
        last_login_at=now_iso,
    )
    data.update(overrides)
    profile = UserProfile(**data)
    auth_module._user_store[email] = profile
    return profile


def _login(role: str = "curator", email: str = EMAIL) -> TestClient:
    """A browser holding the session a login as an active `role` would issue."""
    _set_profile(email, role=role)
    token = create_session_token(
        AuthenticatedUser(email=email, name="Curator", domain=email.split("@")[-1], role=role)
    )
    client = TestClient(app)
    client.cookies.set(settings.auth_cookie_name, token)
    return client


class _BrokenRedis:
    async def get(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def set(self, *args, **kwargs):
        raise ConnectionError("redis down")

    async def sadd(self, *args, **kwargs):
        raise ConnectionError("redis down")


@pytest.mark.parametrize("new_status", ["pending", "disabled"])
def test_demoted_session_is_rejected_with_403(new_status):
    client = _login()
    assert client.get(PROTECTED).status_code == 404

    _set_profile(status=new_status)

    resp = client.get(PROTECTED)
    assert resp.status_code == 403
    assert resp.json()["detail"] == PENDING_DETAIL
    # Browser pages get the same handling as a pending user today: back to login.
    page = client.get("/", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login"
    assert client.get("/auth/me").json()["authenticated"] is False


def test_demoted_session_bearer_token_is_rejected_with_403():
    _set_profile()
    token = create_session_token(AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org"))
    _set_profile(status="pending")
    resp = TestClient(app).get(PROTECTED, headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert resp.json()["detail"] == PENDING_DETAIL


def test_admin_downgrade_takes_effect_immediately():
    client = _login(role="admin")
    assert client.get(ADMIN_ONLY).status_code == 200

    _set_profile(role="curator")

    assert client.get(ADMIN_ONLY).status_code == 403
    assert client.get(PROTECTED).status_code == 404  # still signed in as a curator
    assert client.get("/auth/me").json()["user"]["role"] == "curator"


def test_token_role_never_exceeds_profile_role():
    # A token minted claiming admin for a curator profile doesn't grant admin.
    _set_profile(role="curator")
    token = create_session_token(
        AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org", role="admin")
    )
    client = TestClient(app)
    client.cookies.set(settings.auth_cookie_name, token)
    assert client.get(ADMIN_ONLY).status_code == 403


def test_deleted_profile_invalidates_session():
    client = _login()
    del auth_module._user_store[EMAIL]

    assert client.get(PROTECTED).status_code == 401
    page = client.get("/runs", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?redirect_to=%2Fruns"
    assert client.get("/auth/me").json()["authenticated"] is False


def test_profile_store_error_fails_closed_with_503_and_keeps_cookie(monkeypatch):
    client = _login()

    async def broken():
        return _BrokenRedis()

    monkeypatch.setattr(auth_module, "_get_auth_redis_client", broken)

    for path in (PROTECTED, ADMIN_ONLY, "/", "/auth/me", "/login"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 503, path
        assert "set-cookie" not in resp.headers, path  # not logged out
    assert client.cookies.get(settings.auth_cookie_name)

    # Once the store recovers the same session works again.
    async def recovered():
        return None

    monkeypatch.setattr(auth_module, "_get_auth_redis_client", recovered)
    assert client.get(PROTECTED).status_code == 404


def test_active_session_reads_profile_once_per_request(monkeypatch):
    client = _login()
    calls = []
    real = auth_module.load_user_profile_strict

    async def counting(email):
        calls.append(email)
        return await real(email)

    monkeypatch.setattr(auth_module, "load_user_profile_strict", counting)

    # require_auth + the user-context middleware share one lookup.
    assert client.get(PROTECTED).status_code == 404
    assert calls == [EMAIL]
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/auth/me").json()["user"]["email"] == EMAIL
    assert calls == [EMAIL, EMAIL, EMAIL]


def test_api_key_path_does_not_use_session_profile_check(auth_env, monkeypatch):
    key = generate_api_key()
    auth_env.keys["key-1"] = ApiKeyRecord(
        id="key-1",
        key_hash=hash_api_key(key),
        owner_email=EMAIL,
        name="script",
        created_at=datetime.now(timezone.utc),
    )

    async def must_not_be_called(token):
        raise AssertionError("API keys keep their own profile lookup")

    monkeypatch.setattr(auth_module, "_verify_session", must_not_be_called)
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {key}"}
    # Unchanged: a key owner without a profile is an active default-role user...
    assert client.get(PROTECTED, headers=headers).status_code == 404
    assert client.get(ADMIN_ONLY, headers=headers).status_code == 403
    # ...and a pending owner is rejected with the existing message.
    _set_profile(status="pending")
    resp = client.get(PROTECTED, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["detail"] == PENDING_DETAIL


def test_auth_disabled_mode_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)

    async def must_not_be_called(token):
        raise AssertionError("auth disabled: no profile check")

    monkeypatch.setattr(auth_module, "_verify_session", must_not_be_called)
    # A leftover cookie from an auth-enabled run is still read without a profile check.
    client = TestClient(app)
    client.cookies.set(
        settings.auth_cookie_name,
        create_session_token(AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org")),
    )
    assert client.get(PROTECTED).status_code == 404  # local@internal
    assert client.get(ADMIN_ONLY).status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/auth/me").json()["auth_enabled"] is False
