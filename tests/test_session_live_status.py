"""Session tokens are authorized against the *current* user profile.

A session token carries the role/status the user had at login. These tests
check that demoting, disabling, or deleting the profile takes effect on an
already-issued session, that the token's snapshot never decides access, that
an unreadable profile store fails closed, and that every accepted login
leaves a stored profile behind.

Self-contained (no tests.auth_helpers) so each case can be run against an
older checkout to confirm it fails there.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src import auth as auth_module
from src import main as main_module
from src.api_keys import ApiKeyRecord, generate_api_key, hash_api_key
from src.auth import (
    AuthenticatedUser,
    SAMLAssertionData,
    UserProfile,
    create_oauth_state,
    create_session_token,
    decode_session_token,
)
from src.config import settings
from src.main import app
from src.models.schema import AnnotationResult
from tests.test_api_keys import FakeStore
from tests.test_gene_query_api import FakeRunStore, pipeline_calls  # noqa: F401 (fixture)

PROTECTED = "/v1/annotate/does-not-exist"  # require_auth; 404 once authenticated
ADMIN_ONLY = "/auth/users"  # require_admin
PENDING_DETAIL = "Your account is pending administrator approval. Please contact your AGCG administrator."
EMAIL = "curator@mskcc.org"
FAKE_REDIS_URL = "redis://fake-auth-redis:6379/0"

# The real store hooks, captured at import time: conftest swaps them for
# Redis-less stand-ins before each test, and `redis_store` puts them back.
REAL_GET_AUTH_REDIS = auth_module._get_auth_redis_client
REAL_PROFILE_STORE = getattr(auth_module, "_profile_store_redis", None)


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
    monkeypatch.setattr(main_module, "record_user_action", lambda *args, **kwargs: None)
    fake = FakeStore()
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = fake
    yield fake
    if previous is None:
        del app.state.run_store
    else:
        app.state.run_store = previous


class FakeAuthRedis:
    """Just enough of redis.asyncio.Redis for the auth profile store."""

    def __init__(self):
        self.data = {}
        self.sets = {}
        self.hashes = {}
        self.gets = []
        self.fail = False

    def _check(self):
        if self.fail:
            raise ConnectionError("redis down")

    async def get(self, key):
        self._check()
        self.gets.append(key)
        return self.data.get(key)

    async def set(self, key, value):
        self._check()
        self.data[key] = value

    async def sadd(self, key, *members):
        self._check()
        self.sets.setdefault(key, set()).update(members)

    async def smembers(self, key):
        self._check()
        return set(self.sets.get(key, set()))

    async def hgetall(self, key):
        self._check()
        return dict(self.hashes.get(key, {}))

    def pipeline(self):
        return _FakePipeline(self)


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.ops = []

    def hincrby(self, key, field, amount):
        self.ops.append(("hincrby", key, field, amount))

    def hset(self, key, field, value):
        self.ops.append(("hset", key, field, value))

    async def execute(self):
        self.redis._check()
        for op, key, field, value in self.ops:
            fields = self.redis.hashes.setdefault(key, {})
            fields[field] = str(int(fields.get(field, 0)) + value) if op == "hincrby" else value


@pytest.fixture
def redis_store(monkeypatch):
    """Run the real Redis-backed profile store code against a FakeAuthRedis."""
    fake = FakeAuthRedis()

    async def get_client():
        return fake

    monkeypatch.setattr(settings, "redis_url", FAKE_REDIS_URL)
    monkeypatch.setattr(auth_module, "_get_auth_redis_client", get_client)
    if REAL_PROFILE_STORE is not None:
        monkeypatch.setattr(auth_module, "_profile_store_redis", REAL_PROFILE_STORE)
    return fake


def _set_profile(email: str = EMAIL, redis: FakeAuthRedis | None = None, **overrides) -> UserProfile:
    """Store a profile the way save_user_profile does: Redis (if any) and in-process."""
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
    if redis is not None:
        redis.data[f"agcg:user:{email}"] = profile.model_dump_json()
        redis.sets.setdefault("agcg:users", set()).add(email)
    auth_module._user_store[email] = profile
    return profile


def _client_with(token: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set(settings.auth_cookie_name, token)
    return client


def _login(role: str = "curator", email: str = EMAIL, redis: FakeAuthRedis | None = None) -> TestClient:
    """A browser holding the session a login as an active `role` would issue."""
    _set_profile(email, redis=redis, role=role)
    return _client_with(
        create_session_token(AuthenticatedUser(email=email, name="Curator", domain=email.split("@")[-1], role=role))
    )


# ---------------------------------------------------------------------------
# Demotion / deletion of an already-issued session
# ---------------------------------------------------------------------------


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
    client = _client_with(
        create_session_token(AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org", role="admin"))
    )
    assert client.get(ADMIN_ONLY).status_code == 403


@pytest.mark.parametrize("snapshot_status", ["pending", "disabled"])
def test_token_status_snapshot_does_not_override_active_profile(snapshot_status):
    # Status comes solely from the profile: an approved user whose token still
    # carries an old inactive snapshot is let in.
    _set_profile(status="active")
    client = _client_with(
        create_session_token(
            AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org", status=snapshot_status)
        )
    )
    assert client.get(PROTECTED).status_code == 404
    assert client.get("/auth/me").json()["user"]["status"] == "active"


def test_decoder_only_checks_signature_expiry_and_allowlist():
    token = create_session_token(
        AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org", status="disabled")
    )
    assert decode_session_token(token) is not None


def test_deleted_profile_invalidates_session():
    client = _login()
    del auth_module._user_store[EMAIL]

    assert client.get(PROTECTED).status_code == 401
    page = client.get("/runs", follow_redirects=False)
    assert page.status_code == 303
    assert page.headers["location"] == "/login?redirect_to=%2Fruns"
    assert client.get("/auth/me").json()["authenticated"] is False


def test_deleted_redis_profile_is_not_revived_by_in_process_copy(redis_store):
    client = _login(role="admin", redis=redis_store)
    assert client.get(ADMIN_ONLY).status_code == 200

    # Deleted from Redis; the in-process copy save_user_profile left is stale.
    del redis_store.data[f"agcg:user:{EMAIL}"]
    assert EMAIL in auth_module._user_store

    assert client.get(PROTECTED).status_code == 401
    assert client.get(ADMIN_ONLY).status_code == 401


@pytest.mark.parametrize("provider", ["keycloak", "google", "saml", "dev"])
@pytest.mark.parametrize("jit", [True, False])
def test_missing_profile_is_401_for_every_provider(monkeypatch, provider, jit):
    monkeypatch.setattr(settings, "jit_provisioning_enabled", jit)
    client = _client_with(
        create_session_token(
            AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org", role="admin", provider=provider)
        )
    )
    assert client.get(PROTECTED).status_code == 401
    assert client.get(ADMIN_ONLY).status_code == 401


# ---------------------------------------------------------------------------
# Profile store failures fail closed
# ---------------------------------------------------------------------------


def test_redis_get_error_fails_closed_with_503_and_keeps_cookie(redis_store):
    client = _login(redis=redis_store)
    assert client.get(PROTECTED).status_code == 404

    redis_store.fail = True
    for path in (PROTECTED, ADMIN_ONLY, "/", "/auth/me", "/login"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 503, path
        assert "set-cookie" not in resp.headers, path  # not logged out
    assert client.cookies.get(settings.auth_cookie_name)

    # Once the store recovers the same session works again.
    redis_store.fail = False
    assert client.get(PROTECTED).status_code == 404


def test_redis_client_init_failure_fails_closed_with_503(monkeypatch):
    import redis.asyncio as redis_asyncio

    def broken_from_url(*args, **kwargs):
        raise ValueError("bad redis url")

    monkeypatch.setattr(settings, "redis_url", FAKE_REDIS_URL)
    monkeypatch.setattr(auth_module, "_redis_client", None)
    monkeypatch.setattr(auth_module, "_get_auth_redis_client", REAL_GET_AUTH_REDIS)
    if REAL_PROFILE_STORE is not None:
        monkeypatch.setattr(auth_module, "_profile_store_redis", REAL_PROFILE_STORE)
    monkeypatch.setattr(redis_asyncio, "from_url", broken_from_url)
    client = _login()  # an in-process copy exists, but Redis is the configured store

    resp = client.get(PROTECTED)
    assert resp.status_code == 503
    assert "set-cookie" not in resp.headers


def test_redis_miss_without_any_copy_is_401(redis_store):
    client = _client_with(create_session_token(AuthenticatedUser(email=EMAIL, name="C", domain="mskcc.org")))
    assert client.get(PROTECTED).status_code == 401


# ---------------------------------------------------------------------------
# At most one profile read per request
# ---------------------------------------------------------------------------


def test_active_session_reads_profile_once_per_request(redis_store):
    client = _login(redis=redis_store)

    # require_auth + the user-context middleware share one lookup.
    assert client.get(PROTECTED).status_code == 404
    assert len(redis_store.gets) == 1
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/auth/me").json()["user"]["email"] == EMAIL
    assert len(redis_store.gets) == 3


def test_static_assets_skip_identity_resolution(redis_store):
    client = _login(redis=redis_store)
    assert client.get("/static/app.js").status_code == 200
    assert redis_store.gets == []


def test_annotation_completion_does_not_reread_profile(redis_store, monkeypatch):
    async def fake_pipeline(fusions, **kwargs):
        return AnnotationResult(
            run_id="read-count", timestamp="2026-10-27", fusions_processed=len(fusions),
            genes_annotated=2, annotations=[],
        )

    monkeypatch.setattr(main_module, "run_pipeline", fake_pipeline)
    app.state.run_store = FakeRunStore()
    client = _login(redis=redis_store)

    resp = client.post("/v1/annotate", json={"fusions": ["EML4::ALK"]})
    assert resp.status_code == 200, resp.text
    assert len(redis_store.gets) == 1
    # Usage is still recorded, beside the profile rather than in it.
    assert redis_store.hashes[f"agcg:user_activity:{EMAIL}"]["annotation_count"] == "2"


def test_gene_query_completion_does_not_reread_profile(redis_store, pipeline_calls):  # noqa: F811
    app.state.run_store = FakeRunStore()
    client = _login(redis=redis_store)

    resp = client.post("/v1/genes/query", json={"genes": ["ALK"]})
    assert resp.status_code == 200, resp.text
    assert len(redis_store.gets) == 1
    assert redis_store.hashes[f"agcg:user_activity:{EMAIL}"]["annotation_count"] == "1"


async def test_annotation_activity_is_listed_without_rewriting_profile(redis_store):
    profile = _set_profile(redis=redis_store, annotation_count=4)
    stored = redis_store.data[f"agcg:user:{EMAIL}"]

    await auth_module.record_user_annotation_activity(EMAIL, count=3)

    assert redis_store.data[f"agcg:user:{EMAIL}"] == stored  # profile untouched
    [listed] = await auth_module.list_user_profiles()
    assert listed.annotation_count == profile.annotation_count + 3
    assert listed.last_annotation_at is not None


# ---------------------------------------------------------------------------
# Every accepted login stores a profile; token provider = actual login path
# ---------------------------------------------------------------------------


@pytest.fixture
def keycloak_login(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_url", "https://idp.example")
    monkeypatch.setattr(settings, "keycloak_client_id", "test-client")
    monkeypatch.setattr(settings, "keycloak_allowed_roles", "")
    monkeypatch.setattr(settings, "jit_provisioning_enabled", False)
    monkeypatch.setattr(main_module, "exchange_keycloak_code", AsyncMock(return_value={"access_token": "t"}))
    monkeypatch.setattr(main_module, "get_keycloak_user_info", AsyncMock(return_value={}))

    def login(role: str = "curator"):
        user = AuthenticatedUser(email=EMAIL, name="KC User", domain="mskcc.org", role=role, provider="keycloak")
        monkeypatch.setattr(main_module, "parse_keycloak_user", lambda *args: user)
        browser = TestClient(app)
        resp = browser.get(
            "/auth/callback/keycloak",
            params={"code": "c", "state": create_oauth_state("/")},
            follow_redirects=False,
        )
        return browser, resp

    return login


def test_keycloak_login_without_jit_stores_profile(keycloak_login):
    browser, resp = keycloak_login(role="admin")
    assert resp.status_code == 303

    profile = auth_module._user_store[EMAIL]
    assert (profile.status, profile.role, profile.provider) == ("active", "admin", "keycloak")
    assert browser.get(ADMIN_ONLY).status_code == 200

    # Deleting that profile now ends the session, like every other provider.
    del auth_module._user_store[EMAIL]
    assert browser.get(PROTECTED).status_code == 401


def test_keycloak_login_without_jit_keeps_stored_status(keycloak_login):
    _set_profile(status="pending", login_count=3)
    _, resp = keycloak_login()
    assert resp.status_code == 403
    assert "pending administrator approval" in resp.text
    assert auth_module._user_store[EMAIL].status == "pending"


def test_google_login_token_provider_comes_from_login_path(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client")
    monkeypatch.setattr(main_module, "exchange_google_code", AsyncMock(return_value={"access_token": "t"}))
    monkeypatch.setattr(
        main_module, "get_google_user_info", AsyncMock(return_value={"email": EMAIL, "email_verified": True})
    )
    _set_profile(provider="keycloak")  # e.g. first provisioned through Keycloak

    resp = TestClient(app).get(
        "/auth/callback/google", params={"code": "c", "state": create_oauth_state("/")}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert decode_session_token(resp.cookies.get(settings.auth_cookie_name)).provider == "google"


def test_saml_login_token_provider_comes_from_login_path(monkeypatch):
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "saml_idp_sso_url", "https://idp.example/sso")
    monkeypatch.setattr(settings, "saml_allowed_groups", "")
    monkeypatch.setattr(main_module, "parse_saml_response", lambda *args: SAMLAssertionData(name_id=EMAIL))
    _set_profile(provider="keycloak")

    resp = TestClient(app).post(
        "/auth/saml/acs", data={"SAMLResponse": "assertion", "RelayState": "/"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert decode_session_token(resp.cookies.get(settings.auth_cookie_name)).provider == "saml"


# ---------------------------------------------------------------------------
# Unchanged paths
# ---------------------------------------------------------------------------


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
    client = _client_with(create_session_token(AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org")))
    assert client.get(PROTECTED).status_code == 404  # local@internal
    assert client.get(ADMIN_ONLY).status_code == 200
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/auth/me").json()["auth_enabled"] is False
