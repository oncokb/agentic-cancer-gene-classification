"""Run listing exercises real authentication with a metadata-only mock store."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src import api_keys, auth, main
from src.api_keys import ApiKeyRecord, InMemoryRateLimiter, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser
from src.config import settings
from src.pipeline.run_store import RunSummary, RunSummaryPage, _encode_runs_cursor
from tests.auth_helpers import session_token_for


def summary(run_id, day=1, **overrides):
    data = dict(
        run_id=run_id, created_at=datetime(2026, 10, day, tzinfo=timezone.utc),
        source="session", input_summary="ALK", input_count=1, genes_annotated=1,
    )
    data.update(overrides)
    return RunSummary(**data)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "runs-list-test-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth, "_redis_client", None)
    monkeypatch.setattr(api_keys, "_memory_limiter", InMemoryRateLimiter())
    store = SimpleNamespace(list_runs_for_owner=AsyncMock(return_value=RunSummaryPage([], None)))
    # Patch State's backing dict, not the attribute: pytest>=9.2 records
    # setattr's old value from __dict__, which State never uses, so undo
    # would delattr twice and abort the rest of the teardown.
    monkeypatch.setitem(main.app.state._state, "run_store", store)
    # No lifespan: these API tests intentionally do not connect to MySQL.
    test_client = TestClient(main.app)
    yield test_client, store
    test_client.close()


def session(email="a@mskcc.org"):
    token = session_token_for(AuthenticatedUser(email=email, name="User", domain="mskcc.org"))
    return {"Authorization": f"Bearer {token}"}


def test_owner_scoping_and_metadata_contract(client):
    # Session tokens normalize email already; API-key tests cover owner normalization.
    http, store = client
    rows = {"a@mskcc.org": [summary("a", 3)], "b@mskcc.org": [summary("b", 2)]}

    async def list_owner(owner, **kwargs):
        return RunSummaryPage(rows.get(owner, []), None)

    store.list_runs_for_owner.side_effect = list_owner
    for owner in rows:
        response = http.get("/v1/runs?owner_email=b@mskcc.org", headers=session(owner.upper()))
        assert response.status_code == 200
        assert response.json() == {
            "runs": [{
                "run_id": owner[0], "created_at": f"2026-10-0{3 if owner[0] == 'a' else 2}T00:00:00+00:00",
                "source": "session", "input_summary": "ALK", "input_count": 1,
                "genes_annotated": 1, "view_url": f"/?run={owner[0]}",
            }], "next_cursor": None,
        }
        store.list_runs_for_owner.assert_awaited_with(owner, limit=20, cursor=None)


def test_api_key_uses_key_owner(client, monkeypatch):
    http, store = client
    key = generate_api_key()
    store.get_api_key_by_hash = AsyncMock(return_value=ApiKeyRecord(
        id="runs-list-key", key_hash=hash_api_key(key), owner_email="KeyOwner@mskcc.org",
        name="test", created_at=datetime.now(timezone.utc), last_used_at=datetime.now(timezone.utc),
    ))
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 1000)
    response = http.get("/v1/runs", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    store.list_runs_for_owner.assert_awaited_once_with("keyowner@mskcc.org", limit=20, cursor=None)


def test_auth_disabled_local_user(client, monkeypatch):
    http, store = client
    monkeypatch.setattr(settings, "auth_enabled", False)
    assert http.get("/v1/runs").json() == {"runs": [], "next_cursor": None}
    store.list_runs_for_owner.assert_awaited_once_with("local@internal", limit=20, cursor=None)


def test_paging_until_null(client):
    http, store = client
    rows = [summary("new", 3), summary("middle", 2), summary("old", 1)]
    cursors = [_encode_runs_cursor(row.created_at, row.run_id) for row in rows[:2]]
    store.list_runs_for_owner.side_effect = [
        RunSummaryPage([row], cursors[i] if i < 2 else None) for i, row in enumerate(rows)
    ]
    cursor = None
    seen = []
    for i in range(3):
        params = {"limit": 1}
        if cursor is not None:
            params["cursor"] = cursor
        response = http.get("/v1/runs", params=params, headers=session())
        assert response.status_code == 200
        store.list_runs_for_owner.assert_awaited_with("a@mskcc.org", limit=1, cursor=cursor)
        seen.extend(row["run_id"] for row in response.json()["runs"])
        cursor = response.json()["next_cursor"]
    assert seen == ["new", "middle", "old"]
    assert cursor is None


@pytest.mark.parametrize("cursor", ["bad-cursor", ""])
def test_bad_cursor_422(client, cursor):
    http, store = client

    response = http.get("/v1/runs", params={"cursor": cursor}, headers=session())
    assert response.status_code == 422
    assert response.json()["detail"] == "Invalid cursor"
    store.list_runs_for_owner.assert_not_awaited()


@pytest.mark.parametrize("limit", ["0", "-1", "101", "abc", "1.5"])
def test_invalid_limit_422(client, limit):
    http, store = client
    response = http.get("/v1/runs", params={"limit": limit}, headers=session())
    assert response.status_code == 422
    assert "limit" in str(response.json()["detail"])
    store.list_runs_for_owner.assert_not_awaited()


@pytest.mark.parametrize("limit", [1, 100])
def test_valid_limit_bounds(client, limit):
    http, store = client
    assert http.get("/v1/runs", params={"limit": limit}, headers=session()).status_code == 200
    store.list_runs_for_owner.assert_awaited_once_with("a@mskcc.org", limit=limit, cursor=None)


def test_store_value_error_returns_500(client):
    _, store = client
    store.list_runs_for_owner.side_effect = ValueError("private row handling error")
    http = TestClient(main.app, raise_server_exceptions=False)
    response = http.get("/v1/runs", headers=session())
    http.close()
    assert response.status_code == 500
    assert "private row handling error" not in response.text


def test_unauthenticated_401(client):
    http, store = client
    assert http.get("/v1/runs").status_code == 401
    store.list_runs_for_owner.assert_not_awaited()


@pytest.mark.parametrize("missing_attribute", [False, True])
def test_store_missing_503(client, monkeypatch, missing_attribute):
    http, _ = client
    if missing_attribute:
        monkeypatch.delitem(main.app.state._state, "run_store")
    else:
        monkeypatch.setitem(main.app.state._state, "run_store", None)
    response = http.get("/v1/runs", headers=session())
    assert response.status_code == 503
    assert response.json()["detail"] == "Run storage is unavailable."


def test_null_metadata_and_utc_timestamp(client):
    http, store = client
    store.list_runs_for_owner.return_value = RunSummaryPage([summary(
        "legacy", source=None, input_summary=None, input_count=None, genes_annotated=None,
        created_at=datetime(2026, 10, 1, 2, tzinfo=timezone(timedelta(hours=2))),
    )], None)
    response = http.get("/v1/runs", headers=session())
    assert response.status_code == 200
    row = response.json()["runs"][0]
    assert row["created_at"] == "2026-10-01T00:00:00+00:00"
    assert all(row[key] is None for key in ("source", "input_summary", "input_count", "genes_annotated"))


def test_response_excludes_blobs_and_owner(client):
    http, store = client
    row = SimpleNamespace(
        **vars(summary("metadata-only")), owner_email="a@mskcc.org",
        request_json={"private": "input"}, result_json={"private": "result"},
    )
    store.list_runs_for_owner.return_value = RunSummaryPage([row], None)
    response = http.get("/v1/runs", headers=session())
    assert response.status_code == 200
    assert set(response.json()["runs"][0]) == {
        "run_id", "created_at", "source", "input_summary", "input_count", "genes_annotated", "view_url",
    }
    assert "private" not in response.text
