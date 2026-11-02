"""Who may edit curated overrides.

Roles live in the user store (USER_STORE_BACKEND redis or mysql) and are
re-read on every request; admins grant `curator` on /admin/users. Sign-in
never derives a role from Keycloak roles or SAML groups: they are stored on
the user record only. require_curator (curator or admin, CURRENT role)
guards every curation write; reads stay open to viewers.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from typing import Dict, List, Optional
from unittest.mock import AsyncMock
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from src import api_keys as api_keys_module
from src import auth as auth_module
from src import main as main_module
from src.api_keys import ApiKeyRecord, InMemoryRateLimiter, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser, UserProfile, create_oauth_state, create_session_token
from src.api import curation_routes
from src.config import Settings, settings
from src.main import app
from src.models.schema import FusionEvidenceResult
from tests.auth_helpers import put_profile, stored_profile
from tests.test_curation import FakeCurationStore, store  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("user_store_backend")

EMAIL = "someone@mskcc.org"
ADMIN = "boss@mskcc.org"
GENE_URL = "/v1/curation/gene/ALK?tumor_type=LUAD"
FUSION_URL = "/v1/curation/fusion/EML4::ALK?tumor_type=LUAD"
GENE_BODY = {"payload": {"citations": ["111"]}}
FUSION_BODY = {"payload": {"interpretation": "Curated."}}

# Every curation write endpoint: (method, url, json body).
WRITES = [
    ("put", GENE_URL, GENE_BODY),
    ("delete", GENE_URL, None),
    ("put", FUSION_URL, FUSION_BODY),
    ("delete", FUSION_URL, None),
]
READS = [GENE_URL, FUSION_URL, "/v1/curation/gene/ALK/history?tumor_type=LUAD"]


class CurationAndKeyStore(FakeCurationStore):
    """FakeCurationStore that also holds API keys (one RunStore in the app)."""

    def __init__(self) -> None:
        super().__init__()
        self.keys: Dict[str, ApiKeyRecord] = {}

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        return next((k for k in self.keys.values() if k.key_hash == key_hash), None)

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        pass


@pytest.fixture
def auth_on(monkeypatch, store):  # noqa: F811
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "curation-role-secret-123456")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "jit_provisioning_enabled", True)
    monkeypatch.setattr(settings, "jit_require_admin_approval", False)
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "")
    monkeypatch.setattr(main_module, "record_user_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())

    async def fake_fusion_evidence(fusion, tumor_type=None, max_results=None, **_kwargs):
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, pmids=["111"])

    monkeypatch.setattr(curation_routes.literature, "retrieve_fusion_evidence", fake_fusion_evidence)
    keyed = CurationAndKeyStore()
    app.state.run_store = keyed  # the `store` fixture restores the previous one
    return keyed


def _put_user(email: str, role: str, groups: Optional[List[str]] = None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    put_profile(UserProfile(
        email=email, name="User", domain="mskcc.org", role=role, status="active",
        groups=groups or [], first_login_at=now, last_login_at=now,
    ))


def _session(email: str = EMAIL, token_role: str = "admin") -> TestClient:
    """A signed-in browser. The token's role is a stale login-time snapshot:
    it defaults to admin to prove authorization uses the stored role."""
    client = TestClient(app, headers={"Sec-Fetch-Site": "same-origin"})
    client.cookies.set(
        settings.auth_cookie_name,
        create_session_token(AuthenticatedUser(email=email, name="User", domain="mskcc.org", role=token_role)),
    )
    return client


def _api_key(keyed: CurationAndKeyStore, owner: str = EMAIL) -> Dict[str, str]:
    key = generate_api_key()
    keyed.keys["key-1"] = ApiKeyRecord(
        id="key-1", key_hash=hash_api_key(key), owner_email=owner, name="script",
        created_at=datetime.now(timezone.utc),
    )
    return {"Authorization": f"Bearer {key}"}


def _write(client: TestClient, method: str, url: str, body, headers=None):
    kwargs = {"headers": headers} if headers else {}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(url, **kwargs)


def _seed_overrides(client: TestClient) -> None:
    """Give each DELETE something to revert (so success is 200, not 404)."""
    for method, url, body in WRITES:
        if method == "put":
            assert _write(client, method, url, body).status_code == 200


# ---------------------------------------------------------------------------
# Defaults and removed configuration
# ---------------------------------------------------------------------------


def test_viewer_is_the_default_role():
    assert Settings.model_fields["jit_default_role"].default == "viewer"
    assert AuthenticatedUser(email="a@mskcc.org", name="A", domain="mskcc.org").role == "viewer"
    assert auth_module.MYSQL_NEW_USER_ROLE == "viewer"


def test_no_group_to_role_configuration_or_role_versioning():
    for name in ("keycloak_curator_roles", "saml_curator_groups"):
        assert name not in Settings.model_fields
    for name in ("derive_role", "ROLE_DERIVATION_VERSION"):
        assert not hasattr(auth_module, name)
    assert "role_version" not in UserProfile.model_fields


# ---------------------------------------------------------------------------
# (a) viewers: 403 on every write, reads allowed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "url", "body"), WRITES)
def test_viewer_gets_403_on_every_curation_write(auth_on, method, url, body):
    _put_user(EMAIL, "viewer")
    resp = _write(_session(), method, url, body)
    assert resp.status_code == 403
    assert "Curator privileges required" in resp.json()["detail"]
    assert auth_on.overrides == {} and auth_on.history == []


@pytest.mark.parametrize(("method", "url", "body"), WRITES)
def test_viewer_api_key_gets_403_on_every_curation_write(auth_on, method, url, body):
    _put_user(EMAIL, "viewer")
    resp = _write(TestClient(app), method, url, body, headers=_api_key(auth_on))
    assert resp.status_code == 403
    assert auth_on.overrides == {}


@pytest.mark.parametrize("url", READS)
def test_viewer_can_read_curation(auth_on, url):
    _put_user(EMAIL, "viewer")
    assert _session().get(url).status_code == 200


def test_signed_out_writes_are_401(auth_on):
    for method, url, body in WRITES:
        assert _write(TestClient(app), method, url, body).status_code == 401


# ---------------------------------------------------------------------------
# (b) curators and admins succeed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", ["curator", "admin"])
def test_curator_and_admin_succeed_on_every_curation_write(auth_on, role):
    _put_user(EMAIL, role)
    client = _session(token_role="viewer")  # stale token role is ignored
    for method, url, body in WRITES:
        resp = _write(client, method, url, body)
        assert resp.status_code == 200, (method, url, resp.text)
    assert [e.action for e in auth_on.history] == ["save", "revert", "save", "revert"]
    assert {e.curator_email for e in auth_on.history} == {EMAIL}


@pytest.mark.parametrize("role", ["curator", "admin"])
def test_curator_and_admin_api_keys_succeed(auth_on, role):
    _put_user(EMAIL, role)
    headers = _api_key(auth_on)
    client = TestClient(app)
    for method, url, body in WRITES:
        assert _write(client, method, url, body, headers=headers).status_code == 200


@pytest.mark.parametrize(("role", "can"), [("viewer", False), ("curator", True), ("admin", True)])
def test_auth_me_reports_can_curate_from_the_stored_role(auth_on, role, can):
    _put_user(EMAIL, role)
    body = _session(token_role="admin" if role != "admin" else "viewer").get("/auth/me").json()
    assert body["user"]["role"] == role
    assert body["can_curate"] is can


def test_auth_disabled_allows_curation(monkeypatch, store):  # noqa: F811
    monkeypatch.setattr(settings, "auth_enabled", False)
    client = TestClient(app)
    assert client.get("/auth/me").json()["can_curate"] is True
    assert client.put(GENE_URL, json=GENE_BODY).status_code == 200


# ---------------------------------------------------------------------------
# (c) role changes take effect on the next request
# ---------------------------------------------------------------------------


def test_admin_demoting_a_curator_takes_effect_on_the_next_request(auth_on, user_store_backend):
    if user_store_backend is None:
        pytest.skip("/admin/users changes roles on the MySQL user store only")
    _put_user(ADMIN, "admin")
    _put_user(EMAIL, "curator")
    curator, curator_key = _session(), _api_key(auth_on)
    assert curator.put(GENE_URL, json=GENE_BODY).status_code == 200

    demoted = _session(ADMIN).post(
        f"/v1/admin/users/{quote(EMAIL, safe='')}/changes", json={"role": "viewer", "reason": "rotation"},
    )
    assert demoted.status_code == 200, demoted.text
    assert [(c.field, c.old_value, c.new_value) for c in user_store_backend._role_changes] == [
        ("role", "curator", "viewer")
    ]

    # Same session and same API key, very next request: no re-login needed.
    second = {"expected_version": 1, "payload": {"citations": ["222"]}}
    assert curator.put(GENE_URL, json=second).status_code == 403
    assert curator.delete(GENE_URL).status_code == 403
    assert TestClient(app).put(GENE_URL, json=second, headers=curator_key).status_code == 403
    assert curator.get(GENE_URL).json()["version"] == 1  # still readable, unchanged

    promoted = _session(ADMIN).post(
        f"/v1/admin/users/{quote(EMAIL, safe='')}/changes", json={"role": "curator"},
    )
    assert promoted.status_code == 200
    assert curator.put(GENE_URL, json=second).status_code == 200


def test_stored_role_change_takes_effect_on_the_next_request(auth_on):
    """Both stores: whatever the store holds now decides, not the session."""
    _put_user(EMAIL, "curator")
    client = _session(token_role="curator")
    assert client.put(GENE_URL, json=GENE_BODY).status_code == 200
    _put_user(EMAIL, "viewer")
    assert client.delete(GENE_URL).status_code == 403
    _put_user(EMAIL, "curator")
    assert client.delete(GENE_URL).status_code == 200


# ---------------------------------------------------------------------------
# (d) sign-in never changes an existing role
# ---------------------------------------------------------------------------


@pytest.fixture
def keycloak_login(monkeypatch, auth_on):
    """Sign in through the real Keycloak callback and parse_keycloak_user,
    with the IdP's realm roles in the access token."""
    monkeypatch.setattr(settings, "keycloak_url", "https://idp.example")
    monkeypatch.setattr(settings, "keycloak_client_id", "acgc")
    monkeypatch.setattr(settings, "keycloak_allowed_roles", "")
    monkeypatch.setattr(settings, "keycloak_admin_roles", "")

    def login(groups: List[str], email: str = EMAIL):
        claims = {"realm_access": {"roles": list(groups)}}
        jwt_body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        monkeypatch.setattr(
            main_module, "exchange_keycloak_code",
            AsyncMock(return_value={"access_token": f"header.{jwt_body}.sig"}),
        )
        monkeypatch.setattr(
            main_module, "get_keycloak_user_info",
            AsyncMock(return_value={"email": email, "email_verified": True, "name": "KC User"}),
        )
        browser = TestClient(app)
        resp = browser.get(
            "/auth/callback/keycloak", params={"code": "c", "state": create_oauth_state("/")},
            follow_redirects=False,
        )
        assert resp.status_code == 303, resp.text
        return browser

    return login


# Groups an IdP might send, including ones #117 used to map to curator/admin.
GROUP_SETS = [[], ["acgc-curator"], ["MSK-Curators", "acgc-admin-like"], ["unrelated"]]


@pytest.mark.parametrize("role", ["viewer", "curator", "admin"])
@pytest.mark.parametrize("groups", GROUP_SETS)
def test_keycloak_sign_in_never_changes_an_existing_role(keycloak_login, role, groups):
    _put_user(EMAIL, role, groups=["old-group"])
    browser = keycloak_login(groups)
    stored = stored_profile(EMAIL)
    assert stored.role == role
    assert set(groups) <= set(stored.groups)  # groups are stored, nothing more
    can_write = role in ("curator", "admin")
    assert browser.put(GENE_URL, json=GENE_BODY).status_code == (200 if can_write else 403)


def test_keycloak_admin_role_never_changes_role_on_mysql(keycloak_login, user_store_backend, monkeypatch):
    if user_store_backend is None:
        pytest.skip("Redis keeps main's KEYCLOAK_ADMIN_ROLES promotion (never a demotion)")
    monkeypatch.setattr(settings, "keycloak_admin_roles", "acgc-admin")
    for role in ("viewer", "curator"):
        _put_user(EMAIL, role)
        keycloak_login(["acgc-admin"])
        assert stored_profile(EMAIL).role == role


def test_keycloak_sign_in_never_lowers_an_admin(keycloak_login, monkeypatch):
    monkeypatch.setattr(settings, "keycloak_admin_roles", "acgc-admin")
    _put_user(EMAIL, "admin", groups=["acgc-admin"])
    keycloak_login([])  # admin role removed in Keycloak
    assert stored_profile(EMAIL).role == "admin"


def test_new_keycloak_user_is_a_viewer_whatever_their_groups(keycloak_login):
    browser = keycloak_login(["acgc-curator", "MSK-Curators"], email="new.kc@mskcc.org")
    assert stored_profile("new.kc@mskcc.org").role == "viewer"
    assert browser.put(GENE_URL, json=GENE_BODY).status_code == 403


# ---------------------------------------------------------------------------
# (e) re-evaluation jobs the curator UI (#118) starts and polls
# ---------------------------------------------------------------------------

REEVALUATE_JOBS = "/v1/curation/reevaluate/jobs"


def test_reevaluate_jobs_require_curator(auth_on):
    """POST/GET re-evaluation jobs (#116), which the curator UI calls, are
    curator-only. A missing route would 404 here, not 403."""
    curator_email = "curator.reevaluate@mskcc.org"
    _put_user(EMAIL, "viewer")
    _put_user(curator_email, "curator")
    viewer = _session(EMAIL)  # stale admin token: the stored viewer role decides
    body = {"subject_type": "gene", "subject_key": "ALK", "tumor_type": "LUAD", "evidence_pmids": ["1"]}
    post = viewer.post(REEVALUATE_JOBS, json=body)
    assert post.status_code == 403
    assert viewer.get(f"{REEVALUATE_JOBS}/missing").status_code == 403
    assert _session(curator_email).get(f"{REEVALUATE_JOBS}/missing").status_code == 404  # past auth
