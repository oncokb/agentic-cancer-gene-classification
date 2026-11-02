"""User-store failures never log a driver's message: MySQL error text can
quote row data such as emails. Runs the real MySQL store code (RunStore /
UserStoreMixin) against a stand-in connection whose every statement fails
with an OperationalError naming an email, through the app's real paths."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pymysql
import pytest
from fastapi.testclient import TestClient

from src import auth as auth_module
from src import main as main_module
from src.api_keys import ApiKeyRecord, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser, create_oauth_state, create_session_token
from src.config import settings
from src.main import app
from src.pipeline.run_store import RunStore
from src.pipeline.user_store import LoginIdentity
from tests.test_api_keys import FakeStore

LEAKED = "reviewer@example.invalid"
EMAIL = "curator@mskcc.org"


def _driver_error():
    return pymysql.err.OperationalError(1644, f"usage update rejected for {LEAKED}")


class _FailingCursor:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        raise _driver_error()


class _FailingConnection:
    async def begin(self):
        pass

    def cursor(self):
        return _FailingCursor()

    async def commit(self):
        pass

    async def rollback(self):
        # The rollback fails too, with its own email-quoting message.
        raise pymysql.err.InterfaceError(0, f"rollback lost while handling {LEAKED}")

    def close(self):
        pass


class _FailingPool:
    @asynccontextmanager
    async def acquire(self):
        yield _FailingConnection()


@pytest.fixture
def store(monkeypatch):
    real = RunStore(_FailingPool())
    monkeypatch.setattr(settings, "user_store_backend", "mysql")
    monkeypatch.setattr(auth_module, "_mysql_user_store", real)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agcg_dev_mode", False)
    monkeypatch.setattr(settings, "auth_secret_key", "log-privacy-secret")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "")
    monkeypatch.setattr(main_module, "record_user_action", lambda *args, **kwargs: None)
    return real


@pytest.fixture(autouse=True)
def _keep_app_run_store():
    """A failed startup leaves lifespan's run_store on app.state; put it back."""
    previous = getattr(app.state, "run_store", None)
    yield
    if previous is None:
        if hasattr(app.state, "run_store"):
            del app.state.run_store
    else:
        app.state.run_store = previous


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def _assert_no_email(caplog):
    assert caplog.records, "expected the failure to be logged"
    for record in caplog.records:
        text = record.getMessage()
        if record.exc_info:
            text += logging.Formatter().formatException(record.exc_info)
        assert "@" not in text, (record.name, record.levelname, text)
    assert "@" not in caplog.text


async def test_store_methods_log_class_and_code_only(store, logs):
    for call in (
        lambda: store.get_user(EMAIL),
        lambda: store.incr_activity(EMAIL, 1),
        lambda: store.upsert_login(LoginIdentity(email=EMAIL, name="C"), default_role="viewer",
                                   default_status="active"),
        lambda: store.grant_bootstrap_admin(EMAIL, reason="r"),
        lambda: store.list_users(),
    ):
        with pytest.raises(auth_module.UserStoreUnavailable) as raised:
            await call()
        # The driver error stays chained for callers; it's just never logged.
        assert isinstance(raised.value.__cause__, pymysql.err.OperationalError)
        assert "@" not in str(raised.value)
    _assert_no_email(logs)
    assert "OperationalError (MySQL error 1644)" in logs.text
    assert "InterfaceError (MySQL error 0)" in logs.text  # the failed rollbacks


async def test_activity_write_failure_logs_no_email(store, logs):
    await auth_module.record_user_annotation_activity(EMAIL, count=2)
    _assert_no_email(logs)
    assert auth_module.ACTIVITY_WRITE_FAILED_LOG_KEY in logs.text


def test_session_check_failure_logs_no_email(store, logs):
    client = TestClient(app)
    client.cookies.set(
        settings.auth_cookie_name,
        create_session_token(AuthenticatedUser(email=EMAIL, name="C", domain="mskcc.org")),
    )
    resp = client.get("/v1/annotate/does-not-exist")
    assert resp.status_code == 503
    assert "@" not in resp.text
    _assert_no_email(logs)


def test_api_key_owner_check_failure_logs_no_email(store, logs):
    keys = FakeStore()
    key = generate_api_key()
    keys.keys["key-1"] = ApiKeyRecord(
        id="key-1", key_hash=hash_api_key(key), owner_email=EMAIL, name="script",
        created_at=datetime.now(timezone.utc),
    )
    previous = getattr(app.state, "run_store", None)
    app.state.run_store = keys
    try:
        resp = TestClient(app).get("/v1/annotate/does-not-exist", headers={"Authorization": f"Bearer {key}"})
    finally:
        if previous is None:
            del app.state.run_store
        else:
            app.state.run_store = previous
    assert resp.status_code == 503
    _assert_no_email(logs)


def test_sign_in_failure_logs_no_email(store, logs, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "test-client")
    monkeypatch.setattr(main_module, "exchange_google_code", AsyncMock(return_value={"access_token": "t"}))
    monkeypatch.setattr(
        main_module, "get_google_user_info", AsyncMock(return_value={"email": EMAIL, "email_verified": True}),
    )
    resp = TestClient(app).get(
        "/auth/callback/google", params={"code": "c", "state": create_oauth_state("/")}, follow_redirects=False,
    )
    assert resp.status_code == 503
    assert "set-cookie" not in resp.headers
    _assert_no_email(logs)


def test_admin_list_failure_logs_no_email(store, logs, monkeypatch):
    async def admin_lookup(email):
        return auth_module.UserProfile(
            email=EMAIL, name="A", domain="mskcc.org", role="admin", status="active",
            first_login_at="2026-10-01T00:00:00+00:00", last_login_at="2026-10-01T00:00:00+00:00",
        )

    monkeypatch.setattr(auth_module, "load_user_profile_strict", admin_lookup)  # let the request in
    client = TestClient(app)
    client.cookies.set(
        settings.auth_cookie_name,
        create_session_token(AuthenticatedUser(email=EMAIL, name="A", domain="mskcc.org", role="admin")),
    )
    assert client.get("/auth/users").status_code == 503
    _assert_no_email(logs)


@pytest.mark.parametrize("emails", ["", "kuangf@mskcc.org"])
def test_startup_failure_carries_no_driver_message(store, logs, monkeypatch, emails):
    monkeypatch.setattr(settings, "bootstrap_admin_emails", emails)
    monkeypatch.setattr(main_module, "RunStore", SimpleNamespace(create=AsyncMock(return_value=store)))
    store.close = AsyncMock()
    with pytest.raises(auth_module.UserStoreStartupError) as raised:
        with TestClient(app):
            pass
    error = raised.value
    assert "@" not in str(error) and LEAKED not in str(error)
    assert "OperationalError (MySQL error 1644)" in str(error)
    # Raised without its cause, so the server's startup traceback can't print it.
    assert error.__cause__ is None and error.__suppress_context__
    _assert_no_email(logs)


async def test_bootstrap_grant_failure_at_startup_names_no_email(monkeypatch, logs):
    # count_admins succeeds, the grant fails with a driver error quoting an email.
    from src.pipeline.user_store_fake import FakeUserStore

    fake = FakeUserStore()
    await fake.upsert_login(LoginIdentity(email="kuangf@mskcc.org", name="K"), default_role="viewer",
                            default_status="active")

    async def broken(email, *, reason):
        raise auth_module.UserStoreUnavailable("User store grant_bootstrap_admin failed") from _driver_error()

    monkeypatch.setattr(fake, "grant_bootstrap_admin", broken)
    monkeypatch.setattr(auth_module, "_mysql_user_store", fake)
    monkeypatch.setattr(settings, "bootstrap_admin_emails", "kuangf@mskcc.org")
    with pytest.raises(auth_module.UserStoreStartupError) as raised:
        await auth_module.bootstrap_admins_at_startup()
    assert "@" not in str(raised.value)
    assert "entry 1" in str(raised.value) and "MySQL error 1644" in str(raised.value)
