"""Who may set the cancer-associated call and edit fusion-partner precedents (#120).

Same rules as every other curation write (tests/test_curation_roles.py):
require_curator reads the user's CURRENT stored role on each request, for
sessions and API keys, under both user stores (redis and mysql). Viewers get
403 and nothing is stored; curators and admins succeed; a demoted curator is
blocked on the very next request.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.api import curation_routes
from src.main import app
from src.models.schema import FusionPartnerEvidenceResult
from tests.test_curation import store  # noqa: F401  (fixture)
from tests.test_curation_roles import (  # noqa: F401  (auth_on fixture)
    EMAIL,
    _api_key,
    _put_user,
    _session,
    _write,
    auth_on,
)

pytestmark = pytest.mark.usefixtures("user_store_backend")

CALL_URL = "/v1/curation/gene/KRAS?tumor_type=LUAD"
CALL_BODY = {"payload": {"cancer_associated": False}}
PARTNER_URL = "/v1/curation/fusion_partner/ALK?tumor_type=NSCLC"
PARTNER_ALL_URL = "/v1/curation/fusion_partner/ALK"
PARTNER_BODY = {"payload": {"interpretation": "Curated precedent.", "pmids": ["111"]}}

# #120's writes: a curator-set call and partner-precedent overrides (scoped and all tumor types).
WRITES = [
    ("put", CALL_URL, CALL_BODY),
    ("delete", CALL_URL, None),
    ("put", PARTNER_URL, PARTNER_BODY),
    ("delete", PARTNER_URL, None),
    ("put", PARTNER_ALL_URL, PARTNER_BODY),
    ("delete", PARTNER_ALL_URL, None),
]
REEVALUATE_JOBS = "/v1/curation/reevaluate/jobs"
PARTNER_DRAFT = {"subject_type": "fusion_partner", "subject_key": "ALK", "tumor_type": "NSCLC", "evidence_pmids": ["1"]}


@pytest.fixture
def partner_lookup(monkeypatch, auth_on):  # noqa: F811
    async def fake_partner(gene, tumor_type=None, agnostic=False, **_kwargs):
        return FusionPartnerEvidenceResult(
            gene=gene, tumor_type=tumor_type, scope="tumor_type_scoped" if tumor_type else "all_tumor_types",
            has_precedent=True, pmids=["222"],
        )

    monkeypatch.setattr(curation_routes.literature, "retrieve_fusion_partner_evidence", fake_partner)
    return auth_on


@pytest.mark.parametrize(("method", "url", "body"), WRITES)
def test_viewer_session_gets_403(partner_lookup, method, url, body):
    _put_user(EMAIL, "viewer")
    resp = _write(_session(), method, url, body)  # stale admin token: the stored role decides
    assert resp.status_code == 403
    assert "Curator privileges required" in resp.json()["detail"]
    assert partner_lookup.overrides == {} and partner_lookup.history == []


@pytest.mark.parametrize(("method", "url", "body"), WRITES)
def test_viewer_api_key_gets_403(partner_lookup, method, url, body):
    _put_user(EMAIL, "viewer")
    resp = _write(TestClient(app), method, url, body, headers=_api_key(partner_lookup))
    assert resp.status_code == 403
    assert partner_lookup.overrides == {} and partner_lookup.history == []


def test_viewer_partner_reevaluate_job_is_403(partner_lookup):
    _put_user(EMAIL, "viewer")
    assert _session().post(REEVALUATE_JOBS, json=PARTNER_DRAFT).status_code == 403


@pytest.mark.parametrize("role", ["curator", "admin"])
def test_curator_and_admin_sessions_succeed(partner_lookup, role):
    _put_user(EMAIL, role)
    client = _session(token_role="viewer")  # stale token role is ignored
    for method, url, body in WRITES:
        resp = _write(client, method, url, body)
        assert resp.status_code == 200, (method, url, resp.text)
    assert [(e.subject_type, e.action) for e in partner_lookup.history] == [
        ("gene", "save"), ("gene", "revert"),
        ("fusion_partner", "save"), ("fusion_partner", "revert"),
        ("fusion_partner", "save"), ("fusion_partner", "revert"),
    ]
    assert {e.curator_email for e in partner_lookup.history} == {EMAIL}


@pytest.mark.parametrize("role", ["curator", "admin"])
def test_curator_and_admin_api_keys_succeed(partner_lookup, role):
    _put_user(EMAIL, role)
    headers = _api_key(partner_lookup)
    client = TestClient(app)
    for method, url, body in WRITES:
        resp = _write(client, method, url, body, headers=headers)
        assert resp.status_code == 200, (method, url, resp.text)


def test_demoted_curator_is_blocked_on_the_next_request(partner_lookup):
    _put_user(EMAIL, "curator")
    client, headers = _session(token_role="curator"), _api_key(partner_lookup)
    assert client.put(CALL_URL, json=CALL_BODY).status_code == 200
    assert client.put(PARTNER_URL, json=PARTNER_BODY).status_code == 200

    _put_user(EMAIL, "viewer")
    again = {"expected_version": 1, "payload": {"cancer_associated": True}}
    assert client.put(CALL_URL, json=again).status_code == 403
    assert client.delete(PARTNER_URL).status_code == 403
    assert TestClient(app).put(PARTNER_URL, json={**PARTNER_BODY, "expected_version": 1},
                               headers=headers).status_code == 403
    # Still readable, unchanged.
    assert client.get(CALL_URL).json()["override"]["payload"] == {"cancer_associated": False}
    assert client.get(PARTNER_URL).json()["version"] == 1
