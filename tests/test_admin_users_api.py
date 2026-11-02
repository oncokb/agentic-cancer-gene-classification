"""Admin user management API (/v1/admin) and the /admin/users page route.

The MySQL user store is FakeUserStore here (the in-memory stand-in with the
same semantics; tests/test_user_store_contract.py runs change_user_access
against both it and real MySQL). "redis" is the in-process Redis-less store.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient

from src import auth as auth_module
from src import main as main_module
from src.api_keys import ApiKeyRecord, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser, UserProfile, create_session_token
from src.config import settings
from src.main import app
from src.pipeline.user_store_fake import FakeUserStore
from tests.auth_helpers import put_profile, stored_profile
from tests.test_api_keys import FakeStore

ADMIN = "admin@mskcc.org"
ADMIN2 = "admin2@mskcc.org"
CURATOR = "curator@mskcc.org"
VIEWER = "viewer@mskcc.org"
PENDING = "pending@mskcc.org"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

USERS = "/v1/admin/users"
LOG = "/v1/admin/role-changes"


def changes_url(email: str) -> str:
    return f"{USERS}/{quote(email, safe='')}/changes"


@pytest.fixture
def auth_env(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "admin-users-test-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(main_module, "record_user_action", lambda *args, **kwargs: None)
    keys = FakeStore()
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = keys
    yield keys
    if previous is None:
        del app.state.run_store
    else:
        app.state.run_store = previous


@pytest.fixture
def store(monkeypatch, auth_env, _auth_profiles_in_memory) -> FakeUserStore:
    """USER_STORE_BACKEND=mysql backed by a FakeUserStore."""
    fake = FakeUserStore()
    monkeypatch.setattr(settings, "user_store_backend", "mysql")
    monkeypatch.setattr(auth_module, "_mysql_user_store", fake)
    return fake


@pytest.fixture
def redis_backend(monkeypatch, auth_env, _auth_profiles_in_memory):
    monkeypatch.setattr(settings, "user_store_backend", "redis")


def put(email: str, role: str = "viewer", status: str = "active", name: Optional[str] = None,
        minutes: int = 0) -> UserProfile:
    at = (T0 + timedelta(minutes=minutes)).isoformat()
    profile = UserProfile(
        email=email, name=name or email.split("@")[0].title(), domain=email.split("@")[-1],
        role=role, status=status, first_login_at=at, last_login_at=at,
    )
    put_profile(profile)
    return profile


def session(email: str = ADMIN, role: str = "admin") -> TestClient:
    client = TestClient(app)
    client.cookies.set(
        settings.auth_cookie_name,
        create_session_token(AuthenticatedUser(email=email, name="X", domain="mskcc.org", role=role)),
    )
    return client


def api_key_headers(keys: FakeStore, owner: str) -> dict:
    key = generate_api_key()
    keys.keys["key-1"] = ApiKeyRecord(
        id="key-1", key_hash=hash_api_key(key), owner_email=owner, name="script",
        created_at=datetime.now(timezone.utc),
    )
    return {"Authorization": f"Bearer {key}"}


def seed():
    put(ADMIN, "admin", minutes=5)
    put(CURATOR, "curator", minutes=4)
    put(VIEWER, "viewer", minutes=3)
    put(PENDING, "viewer", "pending", minutes=2)


def all_changes(store: FakeUserStore):
    return list(store._role_changes)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------

ENDPOINTS = [
    ("get", USERS, None),
    ("post", changes_url(VIEWER), {"role": "curator"}),
    ("get", changes_url(VIEWER), None),
    ("get", LOG, None),
]


@pytest.mark.parametrize("method, url, body", ENDPOINTS)
@pytest.mark.parametrize("role", ["viewer", "curator"])
def test_non_admin_sessions_get_403(store, method, url, body, role):
    seed()
    put("someone@mskcc.org", role)
    response = getattr(session("someone@mskcc.org", role), method)(url, **({"json": body} if body else {}))
    assert response.status_code == 403
    assert all_changes(store) == []


@pytest.mark.parametrize("method, url, body", ENDPOINTS)
def test_signed_out_gets_401(store, method, url, body):
    seed()
    response = getattr(TestClient(app), method)(url, **({"json": body} if body else {}))
    assert response.status_code == 401


def test_session_role_is_the_stored_one_not_the_tokens(store):
    seed()
    put("demoted@mskcc.org", "viewer")
    # The token still says admin; the store says viewer.
    assert session("demoted@mskcc.org", "admin").get(USERS).status_code == 403


def test_dev_mode_does_not_open_admin_endpoints(store, monkeypatch):
    monkeypatch.setattr(settings, "agcg_dev_mode", True)
    seed()
    assert session(CURATOR, "curator").get(USERS).status_code == 403


@pytest.mark.parametrize("owner, expected", [(ADMIN, 200), (CURATOR, 403), (VIEWER, 403)])
def test_api_keys_follow_their_owners_role(store, auth_env, owner, expected):
    seed()
    headers = api_key_headers(auth_env, owner)
    client = TestClient(app)
    assert client.get(USERS, headers=headers).status_code == expected
    response = client.post(changes_url(PENDING), json={"status": "active"}, headers=headers)
    assert response.status_code == expected
    if expected == 200:
        assert response.json()["changes"][0]["actor"] == ADMIN
    else:
        assert all_changes(store) == []


def test_api_key_loses_access_when_owner_is_demoted(store, auth_env):
    seed()
    put(ADMIN2, "admin")
    headers = api_key_headers(auth_env, ADMIN2)
    client = TestClient(app)
    assert client.get(USERS, headers=headers).status_code == 200
    assert session().post(changes_url(ADMIN2), json={"role": "viewer"}).status_code == 200
    assert client.get(USERS, headers=headers).status_code == 403


# ---------------------------------------------------------------------------
# GET /v1/admin/users
# ---------------------------------------------------------------------------


def test_list_users_newest_login_first_with_backend_flags(store):
    seed()
    body = session().get(USERS).json()
    assert [u["email"] for u in body["users"]] == [ADMIN, CURATOR, VIEWER, PENDING]
    assert (body["backend"], body["writable"], body["next_cursor"]) == ("mysql", True, None)
    assert set(body["users"][0]) >= {"email", "name", "role", "status", "last_login_at", "annotation_count"}
    assert "claims" not in body["users"][0] and "groups" not in body["users"][0]


def test_list_users_filters_and_searches(store):
    seed()
    put("jose@mskcc.org", "curator", name="José Straße", minutes=1)
    client = session()
    emails = lambda **params: [u["email"] for u in client.get(USERS, params=params).json()["users"]]  # noqa: E731
    assert emails(role="curator") == [CURATOR, "jose@mskcc.org"]
    assert emails(status="pending") == [PENDING]
    assert emails(role="viewer", status="active") == [VIEWER]
    assert emails(search="STRASSE") == ["jose@mskcc.org"]  # normalized search keys
    assert emails(search="josé") == ["jose@mskcc.org"]
    assert emails(search="CURATOR@") == [CURATOR]
    assert emails(search="%") == []


def test_list_users_paginates_with_cursor(store):
    seed()
    client = session()
    first = client.get(USERS, params={"limit": 3}).json()
    assert len(first["users"]) == 3 and first["next_cursor"]
    second = client.get(USERS, params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert [u["email"] for u in second["users"]] == [PENDING]
    assert second["next_cursor"] is None


@pytest.mark.parametrize(
    "params", [{"role": "owner"}, {"status": "frozen"}, {"limit": 0}, {"limit": 501}, {"cursor": "!!bad"}],
)
def test_list_users_invalid_values_are_422(store, params):
    seed()
    assert session().get(USERS, params=params).status_code == 422


def test_list_users_store_unavailable_is_503(store):
    seed()
    client = session()
    original = store.list_users

    async def broken(*args, **kwargs):
        store.unavailable = True
        try:
            return await original(*args, **kwargs)
        finally:
            store.unavailable = False

    store.list_users = broken
    response = client.get(USERS)
    assert response.status_code == 503
    assert ADMIN not in response.text


def test_list_users_on_redis_is_read_only(redis_backend):
    seed()
    client = session()
    body = client.get(USERS).json()
    assert (body["backend"], body["writable"]) == ("redis", False)
    assert [u["email"] for u in body["users"]] == [ADMIN, CURATOR, VIEWER, PENDING]
    assert [u["email"] for u in client.get(USERS, params={"status": "pending"}).json()["users"]] == [PENDING]
    assert [u["email"] for u in client.get(USERS, params={"search": "curat"}).json()["users"]] == [CURATOR]
    first = client.get(USERS, params={"limit": 2}).json()
    second = client.get(USERS, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert [u["email"] for u in first["users"] + second["users"]] == [ADMIN, CURATOR, VIEWER, PENDING]


def test_list_users_on_redis_fails_closed(redis_backend, monkeypatch):
    seed()
    client = session()

    class BrokenRedis:
        async def smembers(self, key):
            raise ConnectionError("redis down")

    async def broken_client():
        return BrokenRedis()

    # Session checks keep using the in-process store; only the listing breaks.
    monkeypatch.setattr("src.admin_users_routes.auth._profile_store_redis", broken_client)
    assert client.get(USERS).status_code == 503


# ---------------------------------------------------------------------------
# POST /v1/admin/users/{email}/changes
# ---------------------------------------------------------------------------


def test_approve_pending_user_records_actor_and_reason(store):
    seed()
    response = session().post(changes_url(PENDING), json={"status": "active", "reason": "  known PI  "})
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["status"] == "active"
    [change] = body["changes"]
    assert (change["field"], change["old_value"], change["new_value"]) == ("status", "pending", "active")
    assert (change["actor"], change["reason"], change["email"]) == (ADMIN, "known PI", PENDING)
    assert stored_profile(PENDING).status == "active"
    assert len(all_changes(store)) == 1


def test_role_and_status_in_one_request(store):
    seed()
    response = session().post(changes_url(PENDING), json={"role": "curator", "status": "active"})
    assert [(c["field"], c["new_value"]) for c in response.json()["changes"]] == [
        ("role", "curator"), ("status", "active"),
    ]
    profile = stored_profile(PENDING)
    assert (profile.role, profile.status) == ("curator", "active")


def test_disable_and_re_enable(store):
    seed()
    client = session()
    assert client.post(changes_url(CURATOR), json={"status": "disabled"}).json()["user"]["status"] == "disabled"
    # The disabled user is locked out on their next request.
    assert session(CURATOR, "curator").get("/v1/runs").status_code == 403
    assert client.post(changes_url(CURATOR), json={"status": "active"}).json()["user"]["status"] == "active"
    assert [c.new_value for c in all_changes(store)] == ["disabled", "active"]


def test_promotion_takes_effect_on_next_request(store):
    seed()
    viewer = session(VIEWER, "viewer")
    assert viewer.get(USERS).status_code == 403
    session().post(changes_url(VIEWER), json={"role": "admin"})
    assert viewer.get(USERS).status_code == 200


def test_email_path_is_normalized(store):
    seed()
    response = session().post(changes_url(" Viewer@MSKCC.org "), json={"role": "curator"})
    assert response.status_code == 200 and response.json()["user"]["email"] == VIEWER


def test_no_op_writes_nothing(store):
    seed()
    before = store._users[VIEWER]
    response = session().post(changes_url(VIEWER), json={"role": "viewer", "status": "active", "reason": "x"})
    assert response.status_code == 200
    assert response.json()["changes"] == []
    assert store._users[VIEWER] == before
    assert all_changes(store) == []


def test_unknown_or_malformed_user_is_404(store):
    seed()
    client = session()
    for address in ("ghost@mskcc.org", "not-an-email"):
        response = client.post(changes_url(address), json={"role": "curator"})
        assert (response.status_code, response.json()["detail"]) == (404, "User not found.")
    assert all_changes(store) == []


@pytest.mark.parametrize(
    "body",
    [{}, {"reason": "only"}, {"role": "owner"}, {"status": "pending"}, {"status": "frozen"},
     {"role": None, "status": None}, {"role": "curator", "reason": "x" * 1001}],
)
def test_invalid_values_are_422(store, body):
    seed()
    assert session().post(changes_url(VIEWER), json=body).status_code == 422
    assert all_changes(store) == []


def test_store_unavailable_is_503_and_writes_nothing(store):
    seed()
    client = session()
    original = store.change_user_access

    async def broken(*args, **kwargs):
        store.unavailable = True
        try:
            return await original(*args, **kwargs)
        finally:
            store.unavailable = False

    store.change_user_access = broken
    assert client.post(changes_url(VIEWER), json={"role": "curator"}).status_code == 503
    assert stored_profile(VIEWER).role == "viewer"
    assert all_changes(store) == []


def test_store_not_configured_is_503(store, monkeypatch):
    seed()
    client = session()
    # Sessions still verify; the admin router then finds no store.
    monkeypatch.setattr("src.admin_users_routes.auth._mysql_store", lambda: (_ for _ in ()).throw(
        auth_module.UserStoreUnavailable("no store")))
    assert client.post(changes_url(VIEWER), json={"role": "curator"}).status_code == 503
    assert client.get(LOG).status_code == 503


def test_admin_cannot_disable_themselves(store):
    seed()
    put(ADMIN2, "admin")
    response = session().post(changes_url(ADMIN.upper()), json={"status": "disabled"})
    assert response.status_code == 409
    assert "own account" in response.json()["detail"]
    assert stored_profile(ADMIN).status == "active"
    assert all_changes(store) == []


def test_admin_may_demote_themselves_while_another_admin_remains(store):
    seed()
    put(ADMIN2, "admin")
    response = session().post(changes_url(ADMIN), json={"role": "curator"})
    assert response.status_code == 200
    assert session().get(USERS).status_code == 403


@pytest.mark.parametrize("body", [{"role": "curator"}, {"role": "viewer"}, {"role": "curator", "status": "active"}])
def test_last_active_admin_cannot_be_demoted(store, body):
    seed()
    response = session().post(changes_url(ADMIN), json=body)
    assert response.status_code == 409
    assert "no active admin" in response.json()["detail"]
    assert stored_profile(ADMIN).role == "admin"
    assert all_changes(store) == []


def test_last_active_admin_cannot_be_disabled_by_another_admin_whose_own_is_disabled(store):
    seed()
    put(ADMIN2, "admin", "disabled")
    # ADMIN2 is disabled, so ADMIN is the last active admin; ADMIN2 also can't act.
    assert session(ADMIN2).post(changes_url(ADMIN), json={"status": "disabled"}).status_code == 403
    assert session().post(changes_url(ADMIN), json={"role": "viewer"}).status_code == 409
    # Re-enabling ADMIN2 makes ADMIN's demotion possible.
    assert session().post(changes_url(ADMIN2), json={"status": "active"}).status_code == 200
    assert session().post(changes_url(ADMIN), json={"role": "viewer"}).status_code == 200


class GatedStore(FakeUserStore):
    """Holds change_user_access until `parties` callers have arrived, so
    concurrent requests both pass their admin check before either changes."""

    def __init__(self, parties: int) -> None:
        super().__init__()
        self.parties = parties
        self.arrived = 0
        self.gate = asyncio.Event()

    async def change_user_access(self, *args, **kwargs):
        self.arrived += 1
        if self.arrived >= self.parties:
            self.gate.set()
        await asyncio.wait_for(self.gate.wait(), timeout=5)
        return await super().change_user_access(*args, **kwargs)


async def test_two_admins_removing_each_other_concurrently_leave_one(monkeypatch, auth_env, _auth_profiles_in_memory):
    gated = GatedStore(parties=2)
    monkeypatch.setattr(settings, "user_store_backend", "mysql")
    monkeypatch.setattr(auth_module, "_mysql_user_store", gated)
    put(ADMIN, "admin")
    put(ADMIN2, "admin")

    def client(email):
        token = create_session_token(AuthenticatedUser(email=email, name="X", domain="mskcc.org", role="admin"))
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
            cookies={settings.auth_cookie_name: token},
        )

    async with client(ADMIN) as a, client(ADMIN2) as b:
        responses = await asyncio.gather(
            a.post(changes_url(ADMIN2), json={"role": "viewer"}),
            b.post(changes_url(ADMIN), json={"status": "disabled"}),
        )

    assert sorted(r.status_code for r in responses) == [200, 409]
    assert await gated.count_admins() == 1
    assert len(gated._role_changes) == 1


# ---------------------------------------------------------------------------
# Change log
# ---------------------------------------------------------------------------


def test_change_log_overall_and_per_user_paginated(store):
    seed()
    client = session()
    client.post(changes_url(PENDING), json={"status": "active"})
    client.post(changes_url(VIEWER), json={"role": "curator", "reason": "promoted"})
    client.post(changes_url(VIEWER), json={"role": "admin"})

    overall = client.get(LOG).json()
    assert [(c["email"], c["new_value"]) for c in overall["changes"]] == [
        (VIEWER, "admin"), (VIEWER, "curator"), (PENDING, "active"),
    ]
    assert overall["changes"][1]["reason"] == "promoted"
    assert {c["actor"] for c in overall["changes"]} == {ADMIN}

    first = client.get(changes_url(VIEWER), params={"limit": 1}).json()
    second = client.get(changes_url(VIEWER), params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [c["new_value"] for c in first["changes"] + second["changes"]] == ["admin", "curator"]
    assert second["next_cursor"] is None


def test_change_log_errors(store):
    seed()
    client = session()
    assert client.get(changes_url("ghost@mskcc.org")).status_code == 404
    assert client.get(LOG, params={"cursor": "nope!"}).status_code == 422
    assert client.get(LOG, params={"limit": 0}).status_code == 422
    original = store.list_role_changes

    async def broken(*args, **kwargs):
        store.unavailable = True
        try:
            return await original(*args, **kwargs)
        finally:
            store.unavailable = False

    store.list_role_changes = broken
    assert client.get(LOG).status_code == 503


# ---------------------------------------------------------------------------
# Redis backend: writes and the change log answer 409
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method, url, body", ENDPOINTS[1:])
def test_redis_backend_changes_and_log_are_409(redis_backend, method, url, body):
    seed()
    response = getattr(session(), method)(url, **({"json": body} if body else {}))
    assert response.status_code == 409
    assert "USER_STORE_BACKEND=mysql" in response.json()["detail"]
    assert stored_profile(VIEWER).role == "viewer"


def test_redis_backend_still_requires_admin(redis_backend):
    seed()
    assert session(VIEWER, "viewer").post(changes_url(VIEWER), json={"role": "admin"}).status_code == 403


# ---------------------------------------------------------------------------
# /admin/users page
# ---------------------------------------------------------------------------


def test_page_signed_out_redirects_through_login(store):
    response = TestClient(app).get("/admin/users", follow_redirects=False)
    assert response.status_code == 303
    assert "redirect_to=%2Fadmin%2Fusers" in response.headers["location"]


def test_page_serves_admins_only(store):
    seed()
    page = session().get("/admin/users")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-cache"
    assert '<script src="/static/admin-users.js"></script>' in page.text
    assert "tracking.js" not in page.text
    for email, role in ((CURATOR, "curator"), (VIEWER, "viewer")):
        response = session(email, role).get("/admin/users", follow_redirects=False)
        assert response.status_code == 403
        assert "admin-users.js" not in response.text


def test_page_auth_disabled_serves_page(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert TestClient(app).get("/admin/users", follow_redirects=False).status_code == 200
