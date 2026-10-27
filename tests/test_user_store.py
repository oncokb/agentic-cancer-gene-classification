"""Implementation-specific UserStore tests that need no MySQL: error mapping
for the MySQL mixin, the fake's outage switch, and cursor helpers. The shared
behavior lives in tests/test_user_store_contract.py."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pymysql
import pytest

from src.auth import UserStoreUnavailable
from src.pipeline.run_store import RunStore
from src.pipeline.user_store import (
    LoginIdentity,
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
