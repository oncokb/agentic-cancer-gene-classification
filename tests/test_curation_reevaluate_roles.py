"""Re-evaluation uses real curator authorization and live roles for both auth methods."""

from datetime import datetime, timezone

import httpx
import pytest

from src import main
from src.api import curation_reevaluate_routes as routes
from src.api_keys import ApiKeyRecord, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser, require_curator
from src.config import settings
from tests.auth_helpers import session_token_for, update_profile
from tests.test_api_keys import store  # noqa: F401 (fixture)

URL = "/v1/curation/reevaluate/jobs"
BODY = {"subject_type": "gene", "subject_key": "ALK", "evidence_pmids": ["1"]}
EMAIL = "curator@mskcc.org"


@pytest.fixture(params=["session", "api_key"])
async def authenticated_client(request, monkeypatch, store, user_store_backend):  # noqa: F811
    # Only fake backend storage and background work; exercise the real auth chain.
    assert routes._curator_dependency is require_curator
    assert require_curator not in main.app.dependency_overrides
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(main, "_track_background_task", lambda coroutine: coroutine.close())
    token = session_token_for(
        AuthenticatedUser(email=EMAIL, name="Curator", domain="mskcc.org", role="curator")
    )
    await routes._jobs.put(
        routes.JobStatus(job_id="owned", owner_email=EMAIL, status="succeeded", draft={})
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        if request.param == "session":
            client.cookies.set(settings.auth_cookie_name, token)
        else:
            key = generate_api_key()
            await store.create_api_key(ApiKeyRecord(
                id="reevaluate-key", key_hash=hash_api_key(key), owner_email=EMAIL,
                name="curator test", created_at=datetime.now(timezone.utc),
            ))
            client.headers["Authorization"] = f"Bearer {key}"
        yield client


@pytest.mark.parametrize("role", ["viewer", "curator", "admin"])
async def test_current_role_guards_create_and_poll(authenticated_client, role):
    # The session retains its original curator claim even when the stored role changes.
    update_profile(EMAIL, role=role)
    created = await authenticated_client.post(URL, json=BODY)
    polled = await authenticated_client.get(f"{URL}/owned")
    expected = 403 if role == "viewer" else 200
    assert created.status_code == expected, created.text
    assert polled.status_code == expected, polled.text
    if role == "viewer":
        assert "Curator privileges required" in created.json()["detail"]
        assert "Curator privileges required" in polled.json()["detail"]
        assert set(routes._jobs.local) == {"owned"}
    else:
        # Poll the newly created job too, preserving owner and response behavior.
        assert (await authenticated_client.get(created.json()["status_url"])).status_code == 200


@pytest.mark.parametrize("next_route", ["create", "get"])
async def test_demoted_curator_is_rejected_on_the_next_request(authenticated_client, next_route):
    created = await authenticated_client.post(URL, json=BODY)
    assert created.status_code == 200, created.text
    status_url = created.json()["status_url"]
    assert (await authenticated_client.get(status_url)).status_code == 200
    update_profile(EMAIL, role="viewer")
    if next_route == "create":
        response = await authenticated_client.post(URL, json=BODY)
    else:
        response = await authenticated_client.get(status_url)
    assert response.status_code == 403, response.text
    assert "Curator privileges required" in response.json()["detail"]
