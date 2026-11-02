"""Applications and reviews through real authentication with a fake MySQL store."""

import asyncio

import httpx
import pytest

from src.config import settings
from src.main import app
from tests import test_admin_users_api as helpers

ADMIN, CURATOR, VIEWER = helpers.ADMIN, helpers.CURATOR, helpers.VIEWER
auth_env, store, redis_backend = helpers.auth_env, helpers.store, helpers.redis_backend
put, session = helpers.put, helpers.session

URL = "/v1/curator-requests"
QUEUE = "/v1/admin/curator-requests"
APPLICATION = {
    "reason": "Curate cancer genes",
    "affiliation": "MSK",
    "expertise": "Genomics",
    "orcid": "0000-0002-1825-0097",
}


def seed():
    put(ADMIN, "admin")
    put(VIEWER)
    put(CURATOR, "curator")


def apply():
    return session(VIEWER).post(URL, json=APPLICATION)


def test_apply_status_duplicate_and_rejection(store):
    seed()
    user = session(VIEWER)
    assert user.get(URL + "/me").json() == {"status": "none", "request": None}
    response = apply()
    assert response.status_code == 201
    request_id = response.json()["id"]
    assert apply().status_code == 409
    assert user.get(URL + "/me").json()["status"] == "pending"
    rejected = session().post(
        f"{QUEUE}/{request_id}/reject", json={"note": "<b>More experience needed</b>"}
    )
    assert rejected.status_code == 200
    mine = user.get(URL + "/me").json()
    assert mine["status"] == "rejected"
    assert mine["request"]["review_note"] == "<b>More experience needed</b>"
    assert apply().status_code == 201


@pytest.mark.parametrize(
    "role,status,code",
    [
        ("curator", "active", 409),
        ("admin", "active", 409),
        ("viewer", "disabled", 403),
        ("viewer", "pending", 403),
    ],
)
def test_eligibility(store, role, status, code):
    put(VIEWER, role, status)
    assert apply().status_code == code


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", ""),
        ("reason", "  "),
        ("reason", "x" * 4001),
        ("affiliation", ""),
        ("affiliation", "x" * 256),
        ("expertise", ""),
        ("expertise", "x" * 4001),
        ("orcid", "https://orcid.org/0000-0002-1825-0097"),
        ("orcid", "0000-0002-1825-009a"),
    ],
)
def test_validation(store, field, value):
    seed()
    assert session(VIEWER).post(URL, json={**APPLICATION, field: value}).status_code == 422


@pytest.mark.parametrize("field", ["reason", "affiliation", "expertise"])
def test_required_fields(store, field):
    seed()
    assert (
        session(VIEWER)
        .post(URL, json={k: v for k, v in APPLICATION.items() if k != field})
        .status_code
        == 422
    )


async def test_approve_grants_current_role_and_one_audit(store):
    seed()
    request_id = apply().json()["id"]
    result = session().post(f"{QUEUE}/{request_id}/approve", json={})
    assert result.status_code == 200
    assert (await store.get_user(VIEWER)).role == "curator"
    changes = (await store.list_role_changes(email=VIEWER)).changes
    assert len(changes) == 1
    assert (changes[0].actor, changes[0].request_id, changes[0].new_value) == (
        ADMIN,
        request_id,
        "curator",
    )
    assert session(VIEWER).get(URL + "/me").json()["status"] == "approved"
    assert session().post(f"{QUEUE}/{request_id}/approve", json={}).status_code == 409


@pytest.mark.parametrize(
    "role,status",
    [("curator", "active"), ("admin", "active"), ("viewer", "disabled"), ("viewer", "pending")],
)
async def test_stale_applicant_fails_without_closing_request(store, role, status):
    seed()
    request_id = apply().json()["id"]
    if role != "viewer":
        await store.set_role(VIEWER, role, actor=ADMIN)
    if status != "active":
        await store.set_status(VIEWER, status, actor=ADMIN)
    before = (await store.list_role_changes(email=VIEWER)).changes
    assert session().post(f"{QUEUE}/{request_id}/approve", json={}).status_code == 409
    assert (await store.get_curator_request(request_id)).status == "pending"
    assert (await store.list_role_changes(email=VIEWER)).changes == before


async def test_concurrent_approval_only_once(store):
    seed()
    request_id = apply().json()["id"]
    cookie = session().cookies.get(settings.auth_cookie_name)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        cookies={settings.auth_cookie_name: cookie},
        headers={"Sec-Fetch-Site": "same-origin"},
    ) as client:
        results = await asyncio.gather(
            *(client.post(f"{QUEUE}/{request_id}/approve", json={}) for _ in range(5))
        )
    assert sorted(r.status_code for r in results) == [200, 409, 409, 409, 409]
    assert len((await store.list_role_changes(email=VIEWER)).changes) == 1


@pytest.mark.parametrize("role", ["viewer", "curator"])
def test_admin_only(store, role):
    seed()
    put(VIEWER, role)
    client = session(VIEWER)
    assert client.get(QUEUE).status_code == 403
    for action in ["approve", "reject"]:
        assert client.post(f"{QUEUE}/1/{action}", json={}).status_code == 403


@pytest.mark.parametrize(
    "endpoint,email", [(URL, VIEWER), (QUEUE + "/1/approve", ADMIN), (QUEUE + "/1/reject", ADMIN)]
)
def test_cross_origin_cookie_write(store, endpoint, email):
    seed()
    assert (
        session(email, same_origin=False)
        .post(endpoint, json=APPLICATION, headers={"Origin": "https://evil.example"})
        .status_code
        == 403
    )


def test_queue_filter_pagination_and_review_validation(store):
    seed()
    first = apply().json()["id"]
    session().post(f"{QUEUE}/{first}/reject", json={})
    second = apply().json()["id"]
    admin = session()
    page = admin.get(QUEUE, params={"limit": 1}).json()
    assert [r["id"] for r in page["requests"]] == [second]
    next_page = admin.get(QUEUE, params={"limit": 1, "cursor": page["next_cursor"]}).json()
    assert [r["id"] for r in next_page["requests"]] == [first]
    assert next_page["next_cursor"] is None
    assert [r["id"] for r in admin.get(QUEUE, params={"status": "pending"}).json()["requests"]] == [
        second
    ]
    for params in [{"status": "invalid"}, {"limit": 0}, {"limit": 201}, {"cursor": "invalid"}]:
        assert admin.get(QUEUE, params=params).status_code == 422
    assert admin.post(f"{QUEUE}/{second}/reject", json={"note": "x" * 1001}).status_code == 422
    assert admin.post(f"{QUEUE}/99999/reject", json={}).status_code == 404


def test_application_without_orcid_is_trimmed(store):
    seed()
    response = session(VIEWER).post(
        URL, json={"reason": " reason ", "affiliation": " MSK ", "expertise": " Genes "}
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["reason"], body["affiliation"], body["expertise"], body["orcid"]) == (
        "reason",
        "MSK",
        "Genes",
        None,
    )


@pytest.mark.parametrize("endpoint,email", [(URL, VIEWER), (QUEUE + "/1/approve", ADMIN)])
def test_cookie_mutations_require_origin_and_json(store, endpoint, email):
    seed()
    assert session(email, same_origin=False).post(endpoint, json=APPLICATION).status_code == 403
    assert session(email).post(endpoint, content="{}").status_code == 415


def test_store_failure_is_503(store):
    seed()
    store.unavailable = True
    # Auth itself fails closed when the store is unavailable.
    assert session(VIEWER).get(URL + "/me").status_code == 503


def test_redis_requires_mysql(redis_backend):
    seed()
    assert session(VIEWER).get(URL + "/me").status_code == 409
    assert "MySQL" in apply().json()["detail"]
    assert session().get(QUEUE).status_code == 409
    assert session().post(QUEUE + "/1/approve", json={}).status_code == 409
