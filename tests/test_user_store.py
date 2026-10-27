"""Implementation-specific UserStore tests that need no MySQL: error mapping
for the MySQL mixin, the fake's outage switch, and cursor helpers. The shared
behavior lives in tests/test_user_store_contract.py."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pymysql
import pytest

from src.auth import UserStoreUnavailable
from src.pipeline.run_store import RunStore
from src.pipeline.user_store import (
    LoginIdentity,
    UserNotFound,
    decode_id_cursor,
    decode_user_cursor,
    encode_id_cursor,
    encode_user_cursor,
    escape_like,
)
from src.pipeline.user_store_fake import FakeUserStore


class _BrokenPool:
    """Every acquire fails like a lost MySQL connection."""

    @asynccontextmanager
    async def acquire(self):
        raise pymysql.err.OperationalError(2003, "Can't connect to MySQL server")
        yield  # pragma: no cover


@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.get_user("a@x.example"),
        lambda s: s.upsert_login(
            LoginIdentity(email="a@x.example", name="A"), default_role="viewer", default_status="active"
        ),
        lambda s: s.set_role("a@x.example", "curator", actor="b@x.example"),
        lambda s: s.incr_activity("a@x.example", 1),
        lambda s: s.list_users(),
        lambda s: s.count_admins(),
        lambda s: s.create_curator_request("a@x.example", reason="r", affiliation="a", expertise="e"),
        lambda s: s.decide_request(1, True, "b@x.example"),
        lambda s: s.list_role_changes(),
    ],
)
@pytest.mark.parametrize("make_store", [lambda: RunStore(_BrokenPool()), FakeUserStore])
async def test_storage_failures_raise_user_store_unavailable(make_store, call):
    store = make_store()
    if isinstance(store, FakeUserStore):
        store.unavailable = True
    with pytest.raises(UserStoreUnavailable):
        await call(store)


async def test_invalid_input_is_value_error_before_touching_storage():
    store = RunStore(_BrokenPool())
    with pytest.raises(ValueError):
        await store.get_user("not-an-email")
    with pytest.raises(ValueError):
        await store.set_role("a@x.example", "owner", actor="b@x.example")
    with pytest.raises(ValueError):
        await store.list_users(cursor="%%%")
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            await store.upsert_login(
                LoginIdentity(email="a@x.example", name="A", claims={"x": [bad]}),
                default_role="viewer",
                default_status="active",
            )


class _DyingCursor:
    """execute() blocks forever ("hang"), raises a deadlock ("deadlock"), or
    returns no row ("empty")."""

    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        if self.conn.mode == "hang":
            self.conn.executing.set()
            await asyncio.Event().wait()
        if self.conn.mode == "deadlock":
            raise pymysql.err.OperationalError(1213, "Deadlock found")

    async def fetchone(self):
        return None


class _DyingConn:
    """Like an aiomysql connection whose statement was cancelled: rollback
    fails because the connection is already gone."""

    def __init__(self, mode):
        self.mode = mode
        self.executing = asyncio.Event()
        self.closed = False

    async def begin(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        raise pymysql.err.InterfaceError(0, "Not connected")

    def close(self):
        self.closed = True

    def cursor(self):
        return _DyingCursor(self)


class _DyingPool:
    def __init__(self, mode):
        self.conn = _DyingConn(mode)

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


async def test_cancellation_survives_failed_rollback_and_discards_connection():
    pool = _DyingPool("hang")
    task = asyncio.create_task(RunStore(pool).set_role("a@x.example", "admin", actor="b@x.example"))
    await pool.conn.executing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pool.conn.closed


async def test_store_error_keeps_original_cause_when_rollback_fails():
    pool = _DyingPool("deadlock")
    with pytest.raises(UserStoreUnavailable) as raised:
        await RunStore(pool).set_role("a@x.example", "admin", actor="b@x.example")
    assert raised.value.__cause__.args[0] == 1213
    assert pool.conn.closed


async def test_domain_error_survives_failed_rollback():
    pool = _DyingPool("empty")
    with pytest.raises(UserNotFound):
        await RunStore(pool).set_role("a@x.example", "admin", actor="b@x.example")
    assert pool.conn.closed


def test_cursor_round_trips():
    at = datetime(2026, 10, 1, 12, 0, 0, 5, tzinfo=timezone.utc)
    assert decode_user_cursor(encode_user_cursor(at, "a|b@x.example")) == (at, "a|b@x.example")
    assert decode_id_cursor(encode_id_cursor(42)) == 42
    for bad in ("", "!!", encode_id_cursor(1).replace("M", "*")):
        with pytest.raises(ValueError):
            decode_user_cursor(bad)
    with pytest.raises(ValueError):
        decode_id_cursor(encode_user_cursor(at, "a@x.example"))


def test_escape_like():
    assert escape_like(r"a_b%c\d") == r"a\_b\%c\\d"
