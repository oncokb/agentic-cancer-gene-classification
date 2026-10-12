"""Curator role derivation (Keycloak roles / SAML groups) and require_curator."""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src import auth as auth_module
from src.auth import (
    AuthenticatedUser,
    UserProfile,
    create_session_token,
    decode_session_token,
    derive_role,
    get_auth_secret_key,
    parse_keycloak_user,
    provision_or_update_user,
)
from src.config import settings
from src.main import app
from tests.test_api_keys import FakeStore, _bearer, _create_key, _session_client

CURATION_URL = "/v1/curation/gene/ALK"


@pytest.fixture
def role_settings(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_admin_roles", "acgc-admin")
    monkeypatch.setattr(settings, "keycloak_curator_roles", "acgc-curator")
    monkeypatch.setattr(settings, "saml_admin_groups", "MSK-Admins")
    monkeypatch.setattr(settings, "saml_curator_groups", "MSK-Curators")
    monkeypatch.setattr(settings, "jit_default_role", "viewer")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth_module, "_redis_client", None)


@pytest.fixture
def auth_on(monkeypatch, role_settings):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "curation-role-secret-123456")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org,openevidence.com")
    monkeypatch.setattr(settings, "allowed_emails", "")


def test_default_jit_role_is_viewer():
    assert type(settings).model_fields["jit_default_role"].default == "viewer"


@pytest.mark.parametrize(
    ("groups", "expected"),
    [
        (["acgc-admin", "acgc-curator"], "admin"),
        (["MSK-Admins"], "admin"),
        (["acgc-curator"], "curator"),
        (["MSK-Curators", "other"], "curator"),
        (["other"], "viewer"),
        ([], "viewer"),
    ],
)
def test_derive_role(role_settings, groups, expected):
    assert derive_role(groups) == expected


def test_settings_lists_parse_comma_separated(monkeypatch):
    monkeypatch.setattr(settings, "keycloak_curator_roles", " a , b ,")
    monkeypatch.setattr(settings, "saml_curator_groups", "G1,,G2 ")
    assert settings.keycloak_curator_roles_list == ["a", "b"]
    assert settings.saml_curator_groups_list == ["G1", "G2"]


def test_parse_keycloak_user_maps_curator_role(role_settings, monkeypatch):
    monkeypatch.setattr(settings, "keycloak_client_id", "acgc")
    claims = {"realm_access": {"roles": ["acgc-curator"]}}
    jwt_payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    user = parse_keycloak_user(
        {"access_token": f"header.{jwt_payload}.sig"}, {"email": "kc.curator@mskcc.org", "name": "KC"}
    )
    assert user.role == "curator"
    assert parse_keycloak_user({}, {"email": "kc.viewer@mskcc.org", "name": "V"}).role == "viewer"


async def test_role_is_rederived_on_every_login(role_settings):
    email = "rederive.user@mskcc.org"
    now = datetime.now(timezone.utc).isoformat()
    # A profile stored before roles were derived: everyone was "curator".
    await auth_module.save_user_profile(
        UserProfile(email=email, name="Legacy", domain="mskcc.org", role="curator",
                    first_login_at=now, last_login_at=now)
    )

    profile = await provision_or_update_user(email=email, name="Legacy", provider="keycloak", groups=["other"])
    assert profile.role == "viewer"
    assert profile.role_version == auth_module.ROLE_DERIVATION_VERSION

    profile = await provision_or_update_user(email=email, name="Legacy", provider="keycloak", groups=["acgc-curator"])
    assert profile.role == "curator"

    profile = await provision_or_update_user(email=email, name="Legacy", provider="keycloak", groups=["acgc-admin"])
    assert profile.role == "admin"

    # Admin is not sticky either: the IdP is the source of truth.
    profile = await provision_or_update_user(email=email, name="Legacy", provider="keycloak", groups=[])
    assert profile.role == "viewer"


def test_legacy_session_token_curator_role_is_downgraded(auth_on):
    import hashlib
    import hmac

    def legacy_token(role: str) -> str:
        now = int(datetime.now(timezone.utc).timestamp())
        payload = {"email": "old@mskcc.org", "name": "Old", "domain": "mskcc.org", "role": role,
                   "status": "active", "provider": "google", "groups": [], "iat": now, "exp": now + 60}
        body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
        sig = hmac.new(get_auth_secret_key().encode(), body.encode(), hashlib.sha256).digest()
        return f"{body}.{base64.urlsafe_b64encode(sig).decode().rstrip('=')}"

    assert decode_session_token(legacy_token("curator")).role == "viewer"
    assert decode_session_token(legacy_token("admin")).role == "admin"
    fresh = create_session_token(AuthenticatedUser(email="new@mskcc.org", name="N", domain="mskcc.org", role="curator"))
    assert decode_session_token(fresh).role == "curator"


@pytest.mark.parametrize(("role", "can"), [("viewer", False), ("curator", True), ("admin", True)])
def test_auth_me_exposes_can_curate(auth_on, role, can):
    client = _session_client(f"{role}.me@mskcc.org", role=role)
    body = client.get("/auth/me").json()
    assert body["user"]["role"] == role
    assert body["can_curate"] is can


def test_auth_me_can_curate_when_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert TestClient(app).get("/auth/me").json()["can_curate"] is True


def test_require_curator_rejects_viewer_on_writes_but_allows_reads(auth_on):
    viewer = _session_client("viewer.writes@mskcc.org", role="viewer")
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = FakeStore()  # no curation methods: reads 503 past auth
    try:
        put = viewer.put(CURATION_URL, json={"payload": {"citations": ["1"]}})
        assert put.status_code == 403
        assert "Curator privileges required" in put.json()["detail"]
        assert viewer.delete(CURATION_URL).status_code == 403
        assert viewer.get(CURATION_URL).status_code == 503

        curator = _session_client("curator.writes@mskcc.org", role="curator")
        assert curator.put(CURATION_URL, json={"payload": {"citations": ["1"]}}).status_code == 503
    finally:
        app.state.run_store = previous


def test_api_key_curation_follows_owner_live_profile_role(auth_on, monkeypatch):
    from src import api_keys as api_keys_module
    from src.api_keys import InMemoryRateLimiter

    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())
    store = FakeStore()
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = store
    email = "key.owner@mskcc.org"
    now = datetime.now(timezone.utc).isoformat()

    def set_profile(role: str, role_version: int = auth_module.ROLE_DERIVATION_VERSION) -> None:
        monkeypatch.setitem(
            auth_module._user_store,
            email,
            UserProfile(email=email, name="Owner", domain="mskcc.org", role=role,
                        first_login_at=now, last_login_at=now, role_version=role_version),
        )

    try:
        key = _create_key(_session_client(email, role="curator"))["key"]
        client = TestClient(app)
        body = {"payload": {"citations": ["1"]}}

        set_profile("viewer")
        assert client.put(CURATION_URL, json=body, headers=_bearer(key)).status_code == 403
        # Legacy profile stored as "curator" before roles were derived.
        set_profile("curator", role_version=1)
        assert client.put(CURATION_URL, json=body, headers=_bearer(key)).status_code == 403
        set_profile("curator")
        assert client.put(CURATION_URL, json=body, headers=_bearer(key)).status_code == 503  # past auth
    finally:
        app.state.run_store = previous


def test_dev_login_role_param(auth_on, monkeypatch):
    monkeypatch.setattr(settings, "dev_login_enabled", True)
    client = TestClient(app)

    resp = client.get("/auth/dev/login?email=dev.curator@mskcc.org&role=curator", follow_redirects=False)
    assert decode_session_token(resp.cookies.get(settings.auth_cookie_name)).role == "curator"

    resp = client.get("/auth/dev/login?email=dev.viewer@mskcc.org", follow_redirects=False)
    assert decode_session_token(resp.cookies.get(settings.auth_cookie_name)).role == "viewer"

    resp = client.get("/auth/dev/login?email=dev.admin@mskcc.org&role=admin", follow_redirects=False)
    assert decode_session_token(resp.cookies.get(settings.auth_cookie_name)).role == "admin"


@pytest.mark.parametrize("jit_default", ["curator", "admin"])
def test_api_key_without_live_profile_cannot_curate_even_with_privileged_jit_default(auth_on, monkeypatch, jit_default):
    from src import api_keys as api_keys_module
    from src.api_keys import InMemoryRateLimiter

    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = FakeStore()
    email = f"no.profile.{jit_default}@mskcc.org"
    try:
        key = _create_key(_session_client(email, role="curator"))["key"]
        monkeypatch.setattr(settings, "jit_default_role", jit_default)
        monkeypatch.delitem(auth_module._user_store, email, raising=False)
        client = TestClient(app)
        resp = client.put(CURATION_URL, json={"payload": {"citations": ["1"]}}, headers=_bearer(key))
        assert resp.status_code == 403
        assert client.get("/v1/api-keys?all=true", headers=_bearer(key)).status_code == 403  # not admin either
    finally:
        app.state.run_store = previous
