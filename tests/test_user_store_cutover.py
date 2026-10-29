"""USER_STORE_BACKEND: sign-in and per-request permission checks against Redis
(today's behaviour) and against the MySQL user store.

Every guarantee from the Redis-only session checks holds for both backends:
one user lookup per request and no cache, a missing user is 401, an
unreadable store is 503 (sign-in sets no cookie), and an API key whose owner
was deleted is rejected. MySQL-only rules: sign-in never changes an existing
user's role or status (IdP groups are stored, not mapped to a role), new
users are active viewers, and BOOTSTRAP_ADMIN_EMAILS grants admin once.

The "mysql" backend is FakeUserStore, the in-memory stand-in with the MySQL
store's behaviour; "redis" is the real Redis code path against a fake client.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src import auth as auth_module
from src import main as main_module
from src.api_keys import ApiKeyRecord, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser, SAMLAssertionData, UserProfile, create_oauth_state, create_session_token
from src.config import settings
from src.main import app
from src.pipeline.user_store import (
    BOOTSTRAP_ACTOR,
    BOOTSTRAP_ALREADY_ADMIN,
    BOOTSTRAP_GRANTED,
    BOOTSTRAP_PREVIOUSLY_GRANTED,
    LoginIdentity,
)
from src.pipeline.user_store_fake import FakeUserStore
from tests.auth_helpers import put_profile
from tests.test_api_keys import FakeStore
from tests.test_session_live_status import FakeAuthRedis

EMAIL = "curator@mskcc.org"
BOOTSTRAP_EMAIL = "kuangf@mskcc.org"
PROTECTED = "/v1/annotate/does-not-exist"  # require_auth; 404 once authenticated
ADMIN_ONLY = "/auth/users"  # require_admin
FAKE_REDIS_URL = "redis://fake-auth-redis:6379/0"


class CountingFakeUserStore(FakeUserStore):
    """FakeUserStore that counts get_user calls (the per-request lookup)."""

    def __init__(self) -> None:
        super().__init__()
        self.get_user_calls = 0

    async def get_user(self, email):
        self.get_user_calls += 1
        return await super().get_user(email)

    async def close(self) -> None:  # stands in for RunStore in the app lifespan
        pass


class Backend:
    """One user store as the tests see it: seed, delete, break, count reads."""

    def __init__(self, name: str, redis: Optional[FakeAuthRedis] = None,
                 store: Optional[CountingFakeUserStore] = None) -> None:
        self.name = name
        self.redis = redis
        self.store = store

    def put(self, email: str = EMAIL, **overrides) -> UserProfile:
        now_iso = datetime.now(timezone.utc).isoformat()
        data = dict(
            email=email, name="Curator", domain=email.split("@")[-1], role="curator",
            status="active", first_login_at=now_iso, last_login_at=now_iso,
        )
        data.update(overrides)
        profile = UserProfile(**data)
        if self.redis is not None:
            self.redis.data[f"agcg:user:{email}"] = profile.model_dump_json()
            self.redis.sets.setdefault("agcg:users", set()).add(email)
            auth_module._user_store[email] = profile  # stale copy save_user_profile leaves
        else:
            put_profile(profile)
        return profile

    def delete(self, email: str = EMAIL) -> None:
        if self.redis is not None:
            del self.redis.data[f"agcg:user:{email}"]
        else:
            del self.store._users[email]

    def fail(self, failing: bool = True) -> None:
        if self.redis is not None:
            self.redis.fail = failing
        else:
            self.store.unavailable = failing

    def lookups(self) -> int:
        if self.redis is not None:
            return sum(1 for key in self.redis.gets if key.startswith("agcg:user:"))
        return self.store.get_user_calls

    def stored(self, email: str = EMAIL) -> Optional[UserProfile]:
        if self.redis is not None:
            payload = self.redis.data.get(f"agcg:user:{email}")
            return UserProfile.model_validate_json(payload) if payload else None
        record = self.store._users.get(email)
        return auth_module._profile_from_record(record) if record else None


@pytest.fixture(autouse=True)
def auth_env(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "user-store-cutover-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "jit_provisioning_enabled", True)
    monkeypatch.setattr(settings, "jit_require_admin_approval", False)
    monkeypatch.setattr(settings, "jit_default_role", "curator")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "")
    monkeypatch.setattr(main_module, "record_user_action", lambda *args, **kwargs: None)
    keys = FakeStore()
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = keys
    yield keys
    if previous is None:
        del app.state.run_store
    else:
        app.state.run_store = previous


@pytest.fixture(params=["redis", "mysql"])
def backend(request, monkeypatch, _auth_profiles_in_memory):
    if request.param == "redis":
        fake = FakeAuthRedis()

        async def get_client():
            return fake

        monkeypatch.setattr(settings, "user_store_backend", "redis")
        monkeypatch.setattr(settings, "redis_url", FAKE_REDIS_URL)
        monkeypatch.setattr(auth_module, "_redis_client", None)
        monkeypatch.setattr(auth_module, "_get_auth_redis_client", get_client)
        monkeypatch.setattr(auth_module, "_profile_store_redis", _auth_profiles_in_memory["_profile_store_redis"])
        return Backend("redis", redis=fake)
    store = CountingFakeUserStore()
    monkeypatch.setattr(settings, "user_store_backend", "mysql")
    monkeypatch.setattr(auth_module, "_mysql_user_store", store)
    return Backend("mysql", store=store)


@pytest.fixture
def mysql(backend):
    if backend.name != "mysql":
        pytest.skip("MySQL user store only")
    return backend


def _session(email: str = EMAIL, role: str = "curator") -> TestClient:
    client = TestClient(app)
    client.cookies.set(
        settings.auth_cookie_name,
        create_session_token(AuthenticatedUser(email=email, name="Curator", domain="mskcc.org", role=role)),
    )
    return client


def _api_key(keys: FakeStore, owner: str = EMAIL) -> dict:
    key = generate_api_key()
    keys.keys["key-1"] = ApiKeyRecord(
        id="key-1", key_hash=hash_api_key(key), owner_email=owner, name="script",
        created_at=datetime.now(timezone.utc),
    )
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def google_login(monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client")
    monkeypatch.setattr(main_module, "exchange_google_code", AsyncMock(return_value={"access_token": "t"}))

    def login(email: str = EMAIL):
        monkeypatch.setattr(
            main_module, "get_google_user_info",
            AsyncMock(return_value={"email": email, "email_verified": True, "name": "Google Name"}),
        )
        browser = TestClient(app)
        resp = browser.get(
            "/auth/callback/google", params={"code": "c", "state": create_oauth_state("/")},
            follow_redirects=False,
        )
        return browser, resp

    return login


@pytest.fixture
def keycloak_login(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_url", "https://idp.example")
    monkeypatch.setattr(settings, "keycloak_client_id", "test-client")
    monkeypatch.setattr(settings, "keycloak_allowed_roles", "")
    monkeypatch.setattr(settings, "keycloak_admin_roles", "acgc-admin")
    monkeypatch.setattr(main_module, "exchange_keycloak_code", AsyncMock(return_value={"access_token": "t"}))
    monkeypatch.setattr(main_module, "get_keycloak_user_info", AsyncMock(return_value={}))

    def login(groups=()):
        groups = list(groups)
        # parse_keycloak_user maps admin roles to "admin" before provisioning.
        role = "admin" if "acgc-admin" in groups else settings.jit_default_role
        user = AuthenticatedUser(
            email=EMAIL, name="KC User", domain="mskcc.org", role=role, provider="keycloak", groups=groups,
        )
        monkeypatch.setattr(main_module, "parse_keycloak_user", lambda *args: user)
        browser = TestClient(app)
        resp = browser.get(
            "/auth/callback/keycloak", params={"code": "c", "state": create_oauth_state("/")},
            follow_redirects=False,
        )
        return browser, resp

    return login


@pytest.fixture
def saml_login(monkeypatch):
    monkeypatch.setattr(settings, "saml_enabled", True)
    monkeypatch.setattr(settings, "saml_idp_sso_url", "https://idp.example/sso")
    monkeypatch.setattr(settings, "saml_allowed_groups", "")
    monkeypatch.setattr(settings, "saml_admin_groups", "MSK-Admins")

    def login(groups=()):
        assertion = SAMLAssertionData(name_id=EMAIL, display_name="SAML Name", groups=list(groups))
        monkeypatch.setattr(main_module, "parse_saml_response", lambda *args: assertion)
        browser = TestClient(app)
        resp = browser.post(
            "/auth/saml/acs", data={"SAMLResponse": "assertion", "RelayState": "/"}, follow_redirects=False,
        )
        return browser, resp

    return login


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_backend_defaults_to_redis_and_rejects_unknown_values():
    from src.config import Settings

    assert Settings(_env_file=None).user_store_backend == "redis"
    assert Settings(_env_file=None, user_store_backend="mysql").user_store_backend == "mysql"
    with pytest.raises(ValueError):
        Settings(_env_file=None, user_store_backend="postgres")
    assert Settings(
        _env_file=None, bootstrap_admin_emails=" KuangF@mskcc.org , ,b@mskcc.org"
    ).bootstrap_admin_emails_list == ["kuangf@mskcc.org", "b@mskcc.org"]


# ---------------------------------------------------------------------------
# Per-request checks (both backends)
# ---------------------------------------------------------------------------


def test_per_request_lookup_count_is_one(backend):
    backend.put()
    client = _session()
    assert client.get(PROTECTED).status_code == 404
    assert backend.lookups() == 1
    assert client.get("/auth/me").json()["user"]["email"] == EMAIL
    assert backend.lookups() == 2  # one per request, no cache


def test_role_and_status_changes_apply_to_issued_sessions(backend):
    backend.put(role="admin")
    client = _session(role="admin")
    assert client.get(ADMIN_ONLY).status_code == 200
    backend.put(role="curator")
    assert client.get(ADMIN_ONLY).status_code == 403
    backend.put(status="disabled")
    assert client.get(PROTECTED).status_code == 403


def test_static_assets_do_not_hit_the_store(backend):
    backend.put()
    assert _session().get("/static/app.js").status_code == 200
    assert backend.lookups() == 0


def test_deleted_user_is_401(backend):
    backend.put()
    client = _session()
    assert client.get(PROTECTED).status_code == 404
    backend.delete()
    assert client.get(PROTECTED).status_code == 401
    assert client.get(ADMIN_ONLY).status_code == 401
    assert client.get("/auth/me").json()["authenticated"] is False


def test_deleted_user_is_not_revived_from_the_other_store(backend):
    # With MySQL a Redis record (or in-process copy) is never consulted, and
    # vice versa: the session claims say admin, but there is no user.
    profile = backend.put(role="admin")
    auth_module._user_store[EMAIL] = profile
    backend.delete()
    assert _session(role="admin").get(ADMIN_ONLY).status_code == 401


def test_store_error_is_503_and_keeps_the_session(backend):
    backend.put()
    client = _session()
    backend.fail()
    for path in (PROTECTED, ADMIN_ONLY, "/", "/auth/me"):
        resp = client.get(path, follow_redirects=False)
        assert resp.status_code == 503, path
        assert "set-cookie" not in resp.headers, path
    backend.fail(False)
    assert client.get(PROTECTED).status_code == 404


def test_mysql_store_not_configured_is_503(mysql, monkeypatch):
    mysql.put()
    monkeypatch.setattr(auth_module, "_mysql_user_store", None)
    assert _session().get(PROTECTED).status_code == 503


def test_mysql_unexpected_store_exception_is_503(mysql, monkeypatch):
    mysql.put()

    async def broken(email):
        raise RuntimeError("pool closed")

    monkeypatch.setattr(mysql.store, "get_user", broken)
    assert _session().get(PROTECTED).status_code == 503


def test_api_key_with_deleted_owner_is_401(backend, auth_env):
    headers = _api_key(auth_env)
    backend.put(role="admin")
    client = TestClient(app)
    assert client.get(ADMIN_ONLY, headers=headers).status_code == 200
    backend.delete()
    assert client.get(PROTECTED, headers=headers).status_code == 401
    assert client.get(ADMIN_ONLY, headers=headers).status_code == 401


def test_api_key_owner_store_error_is_503(backend, auth_env):
    headers = _api_key(auth_env)
    backend.put()
    backend.fail()
    assert TestClient(app).get(PROTECTED, headers=headers).status_code == 503


def test_api_key_uses_owners_current_role_with_one_lookup(backend, auth_env):
    headers = _api_key(auth_env)
    backend.put(role="curator")
    client = TestClient(app)
    assert client.get(ADMIN_ONLY, headers=headers).status_code == 403
    assert backend.lookups() == 1
    backend.put(role="admin")
    assert client.get(ADMIN_ONLY, headers=headers).status_code == 200


# ---------------------------------------------------------------------------
# Sign-in
# ---------------------------------------------------------------------------


def test_existing_user_role_and_status_unchanged_on_google_sign_in(backend, google_login):
    backend.put(role="viewer" if backend.name == "mysql" else "curator", login_count=3)
    before = backend.stored()
    _, resp = google_login()
    assert resp.status_code == 303
    after = backend.stored()
    assert (after.role, after.status) == (before.role, before.status)
    assert after.login_count == 4
    assert after.name == "Google Name"


@pytest.mark.parametrize("status", ["pending", "disabled"])
def test_sign_in_never_reactivates_an_inactive_user(backend, google_login, status):
    backend.put(status=status)
    browser, resp = google_login()
    assert backend.stored().status == status
    if status == "pending":  # turned away at sign-in, without a session
        assert resp.status_code == 403
        assert settings.auth_cookie_name not in resp.cookies
    assert browser.get(PROTECTED).status_code == (401 if status == "pending" else 403)


def test_keycloak_admin_group_does_not_change_role_with_mysql(mysql, keycloak_login):
    mysql.put(role="curator", groups=["old-group"])
    browser, resp = keycloak_login(groups=["acgc-admin", "idp-group"])
    assert resp.status_code == 303
    stored = mysql.stored()
    assert stored.role == "curator"
    assert stored.groups == ["acgc-admin", "idp-group"]  # stored as the IdP sent them
    assert browser.get(ADMIN_ONLY).status_code == 403


def test_keycloak_admin_group_maps_to_admin_with_redis(backend, keycloak_login):
    # Redis keeps today's behaviour byte for byte, group mapping included.
    if backend.name != "redis":
        pytest.skip("Redis user store only")
    backend.put(role="curator")
    browser, resp = keycloak_login(groups=["acgc-admin"])
    assert resp.status_code == 303
    assert backend.stored().role == "admin"


def test_new_keycloak_user_in_admin_group_is_a_viewer_with_mysql(mysql, keycloak_login):
    browser, resp = keycloak_login(groups=["acgc-admin"])
    assert resp.status_code == 303
    assert (mysql.stored().role, mysql.stored().status) == ("viewer", "active")
    assert browser.get(ADMIN_ONLY).status_code == 403


def test_saml_admin_group_does_not_change_role_with_mysql(mysql, saml_login):
    mysql.put(role="viewer")
    _, resp = saml_login(groups=["MSK-Admins"])
    assert resp.status_code == 303
    assert mysql.stored().role == "viewer"
    assert mysql.stored().groups == ["MSK-Admins"]


def test_admin_keeps_admin_without_group_with_mysql(mysql, keycloak_login):
    mysql.put(role="admin")
    keycloak_login(groups=[])
    assert mysql.stored().role == "admin"


def test_new_user_is_an_active_viewer_with_mysql(mysql, google_login):
    browser, resp = google_login()
    assert resp.status_code == 303
    assert settings.auth_cookie_name in resp.cookies
    stored = mysql.stored()
    assert (stored.role, stored.status, stored.provider, stored.login_count) == ("viewer", "active", "google", 1)
    assert browser.get(PROTECTED).status_code == 404
    assert browser.get(ADMIN_ONLY).status_code == 403


def test_new_user_with_redis_keeps_jit_default_role(backend, google_login):
    if backend.name != "redis":
        pytest.skip("Redis user store only")
    google_login()
    assert (backend.stored().role, backend.stored().status) == ("curator", "active")


def test_new_user_is_pending_when_approval_required(backend, google_login, monkeypatch):
    monkeypatch.setattr(settings, "jit_require_admin_approval", True)
    _, resp = google_login()
    assert resp.status_code == 403
    assert "pending administrator approval" in resp.text
    assert settings.auth_cookie_name not in resp.cookies
    assert backend.stored().status == "pending"


def test_new_keycloak_user_without_jit_is_active(backend, keycloak_login, monkeypatch):
    monkeypatch.setattr(settings, "jit_provisioning_enabled", False)
    monkeypatch.setattr(settings, "jit_require_admin_approval", True)
    _, resp = keycloak_login()
    assert resp.status_code == 303
    assert backend.stored().status == "active"


@pytest.mark.parametrize("provider", ["google", "keycloak", "saml"])
def test_store_error_at_sign_in_is_503_without_cookie(backend, provider, google_login, keycloak_login, saml_login):
    backend.put()
    backend.fail()
    login = {"google": google_login, "keycloak": keycloak_login, "saml": saml_login}[provider]
    _, resp = login()
    assert resp.status_code == 503
    assert "temporarily unavailable" in resp.text
    assert "set-cookie" not in resp.headers


def test_sign_in_with_mysql_never_touches_redis(mysql, google_login, monkeypatch):
    async def no_redis():
        raise AssertionError("USER_STORE_BACKEND=mysql must not read or write Redis user records")

    monkeypatch.setattr(settings, "redis_url", FAKE_REDIS_URL)
    monkeypatch.setattr(auth_module, "_get_auth_redis_client", no_redis)
    monkeypatch.setattr(auth_module, "_profile_store_redis", no_redis)
    browser, resp = google_login()
    assert resp.status_code == 303
    assert browser.get(PROTECTED).status_code == 404
    assert auth_module._user_store == {}


# ---------------------------------------------------------------------------
# Bootstrap admins (MySQL)
# ---------------------------------------------------------------------------


async def _role_changes(store, email):
    return (await store.list_role_changes(email=email)).changes


def test_bootstrap_admin_granted_once_at_first_sign_in(mysql, google_login, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL.upper())
    browser, resp = google_login(BOOTSTRAP_EMAIL)
    assert resp.status_code == 303
    assert mysql.stored(BOOTSTRAP_EMAIL).role == "admin"
    assert browser.get(ADMIN_ONLY).status_code == 200

    google_login(BOOTSTRAP_EMAIL)  # signing in again doesn't log another grant
    changes = asyncio.run(_role_changes(mysql.store, BOOTSTRAP_EMAIL))
    assert [(c.field, c.old_value, c.new_value, c.actor) for c in changes] == [
        ("role", "viewer", "admin", BOOTSTRAP_ACTOR)
    ]
    assert changes[0].reason == auth_module.BOOTSTRAP_ADMIN_REASON


def test_bootstrap_admin_not_regranted_after_demotion(mysql, google_login, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    google_login(BOOTSTRAP_EMAIL)
    asyncio.run(mysql.store.set_role(BOOTSTRAP_EMAIL, "curator", actor="other-admin@mskcc.org"))

    browser, resp = google_login(BOOTSTRAP_EMAIL)
    assert resp.status_code == 303
    assert mysql.stored(BOOTSTRAP_EMAIL).role == "curator"
    assert browser.get(ADMIN_ONLY).status_code == 403
    assert asyncio.run(auth_module.bootstrap_admins_at_startup()) is None
    assert mysql.stored(BOOTSTRAP_EMAIL).role == "curator"


def test_bootstrap_respects_an_earlier_migration_grant(mysql, monkeypatch):
    # The migration script's --bootstrap-admin grant counts: same actor.
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    mysql.put(BOOTSTRAP_EMAIL, role="viewer")
    assert asyncio.run(mysql.store.grant_bootstrap_admin(BOOTSTRAP_EMAIL, reason="migration")) == BOOTSTRAP_GRANTED
    asyncio.run(mysql.store.set_role(BOOTSTRAP_EMAIL, "viewer", actor="other-admin@mskcc.org"))
    assert asyncio.run(auth_module.grant_bootstrap_admin_if_listed(BOOTSTRAP_EMAIL)) == BOOTSTRAP_PREVIOUSLY_GRANTED
    assert mysql.stored(BOOTSTRAP_EMAIL).role == "viewer"


def test_unlisted_user_is_never_granted(mysql, google_login, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    google_login()
    assert mysql.stored().role == "viewer"
    assert mysql.store._role_changes == []


def test_bootstrap_list_is_ignored_with_redis(backend, google_login, monkeypatch):
    if backend.name != "redis":
        pytest.skip("Redis user store only")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    google_login(BOOTSTRAP_EMAIL)
    assert backend.stored(BOOTSTRAP_EMAIL).role == "curator"


def test_startup_wires_the_store_and_grants_existing_bootstrap_admins(monkeypatch):
    store = CountingFakeUserStore()
    asyncio.run(store.upsert_login(
        LoginIdentity(email=BOOTSTRAP_EMAIL, name="K"), default_role="viewer", default_status="active",
    ))
    monkeypatch.setattr(settings, "user_store_backend", "mysql")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", f"{BOOTSTRAP_EMAIL},not-signed-in@mskcc.org")
    monkeypatch.setattr(main_module, "RunStore", SimpleNamespace(create=AsyncMock(return_value=store)))
    monkeypatch.setattr(auth_module, "_mysql_user_store", None)

    with TestClient(app):
        assert auth_module._mysql_user_store is store
        assert store._users[BOOTSTRAP_EMAIL].role == "admin"
        assert "not-signed-in@mskcc.org" not in store._users
    assert auth_module._mysql_user_store is None  # cleared at shutdown

    with TestClient(app):  # a restart doesn't grant again
        pass
    assert len(store._role_changes) == 1


def test_startup_with_redis_does_not_wire_mysql(monkeypatch):
    store = CountingFakeUserStore()
    monkeypatch.setattr(settings, "user_store_backend", "redis")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    monkeypatch.setattr(main_module, "RunStore", SimpleNamespace(create=AsyncMock(return_value=store)))
    monkeypatch.setattr(auth_module, "_mysql_user_store", None)
    with TestClient(app):
        assert auth_module._mysql_user_store is None


async def test_startup_bootstrap_survives_store_errors(mysql, monkeypatch):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", BOOTSTRAP_EMAIL)
    mysql.put(BOOTSTRAP_EMAIL, role="viewer")
    mysql.fail()
    await auth_module.bootstrap_admins_at_startup()  # logged, not raised
    mysql.fail(False)
    assert mysql.stored(BOOTSTRAP_EMAIL).role == "viewer"
    assert await auth_module.grant_bootstrap_admin_if_listed(BOOTSTRAP_EMAIL) == BOOTSTRAP_GRANTED
    assert await auth_module.grant_bootstrap_admin_if_listed(BOOTSTRAP_EMAIL) == BOOTSTRAP_ALREADY_ADMIN


# ---------------------------------------------------------------------------
# Admin user list and usage counts
# ---------------------------------------------------------------------------


async def test_usage_counts_and_admin_list(backend):
    backend.put(EMAIL, role="curator", annotation_count=4)
    await auth_module.record_user_annotation_activity(EMAIL, count=3)
    [listed] = await auth_module.list_user_profiles()
    assert listed.email == EMAIL
    assert listed.annotation_count == 7
    assert listed.last_annotation_at is not None


async def test_failed_usage_write_is_dropped(backend):
    backend.put()
    backend.fail()
    await auth_module.record_user_annotation_activity(EMAIL, count=3)  # never raises
    backend.fail(False)
    [listed] = await auth_module.list_user_profiles()
    assert listed.annotation_count == 0
    assert auth_module._user_activity == {}


def test_admin_user_list_endpoint(backend):
    backend.put("admin@mskcc.org", role="admin", last_login_at="2026-10-02T00:00:00+00:00")
    backend.put(EMAIL, role="curator", last_login_at="2026-10-01T00:00:00+00:00")
    resp = _session("admin@mskcc.org", role="admin").get(ADMIN_ONLY)
    assert resp.status_code == 200
    users = resp.json()
    assert [u["email"] for u in users] == ["admin@mskcc.org", EMAIL]
    assert {u["role"] for u in users} == {"admin", "curator"}


def test_admin_user_list_pages_through_mysql(mysql):
    from src.pipeline.user_store import MAX_PAGE_LIMIT

    mysql.put("admin@mskcc.org", role="admin")
    for n in range(MAX_PAGE_LIMIT + 5):
        mysql.put(f"user{n}@mskcc.org", role="viewer", last_login_at="2026-01-01T00:00:00+00:00")
    resp = _session("admin@mskcc.org", role="admin").get(ADMIN_ONLY)
    assert resp.status_code == 200
    assert len(resp.json()) == MAX_PAGE_LIMIT + 6


def test_admin_user_list_store_error_is_503(mysql, monkeypatch):
    mysql.put("admin@mskcc.org", role="admin")

    async def broken(*args, **kwargs):
        raise auth_module.UserStoreUnavailable("down")

    monkeypatch.setattr(mysql.store, "list_users", broken)
    assert _session("admin@mskcc.org", role="admin").get(ADMIN_ONLY).status_code == 503
