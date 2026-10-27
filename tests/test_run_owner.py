"""Run ownership: who started each saved run, how, and the run-list query.

Covers the owner/source attribution at every run save site (session, API key
and auth-disabled callers), the input summary, and the RunStore schema
upgrade and list query against a fake cursor, so they run without MySQL. The
real-SQL round trips live in tests/test_run_store.py.
"""

from __future__ import annotations

import base64
import inspect
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import pymysql
import pytest
from fastapi.testclient import TestClient

from src import api_keys as api_keys_module
from src import auth as auth_module
from src import main
from src.api.run_persistence import run_owner
from src.api_keys import ApiKeyRecord, InMemoryRateLimiter, generate_api_key, hash_api_key
from src.auth import AuthenticatedUser
from src.config import settings
from src.models.schema import AnnotationResult, GeneAnnotation
from src.pipeline import run_store as run_store_module
from src.pipeline.run_store import (
    INPUT_SUMMARY_MAX_LENGTH,
    RunStore,
    RunsSchemaUpgradeError,
    _decode_runs_cursor,
    _encode_runs_cursor,
    summarize_run_inputs,
)
from tests.auth_helpers import session_token_for

# ---------------------------------------------------------------------------
# Input summary
# ---------------------------------------------------------------------------


def test_summary_of_annotate_request_lists_inputs_in_order():
    payload = {"fusions": [{"fusion": "EML4::ALK", "tumor_type": "LUAD"}, {"fusion": "TP53"}, "BRAF"]}

    assert summarize_run_inputs(payload) == ("EML4::ALK, TP53, BRAF", 3)


def test_summary_of_single_gene_request():
    assert summarize_run_inputs({"gene": "ALK", "tumor_type": None}) == ("ALK", 1)


def test_summary_caps_listed_inputs_and_counts_the_rest():
    payload = {"fusions": [{"fusion": f"GENE{i}"} for i in range(12)]}

    summary, count = summarize_run_inputs(payload)

    assert summary == "GENE0, GENE1, GENE2, GENE3, GENE4 (+7 more)"
    assert count == 12


def test_summary_is_bounded_in_length():
    payload = {"fusions": [{"fusion": "X" * 200 + f"::{i}"} for i in range(8)]}

    summary, count = summarize_run_inputs(payload)

    assert len(summary) <= INPUT_SUMMARY_MAX_LENGTH
    assert summary.endswith("… (+3 more)")
    assert count == 8


def test_summary_of_empty_request():
    assert summarize_run_inputs({"fusions": []}) == ("", 0)


# ---------------------------------------------------------------------------
# Owner attribution
# ---------------------------------------------------------------------------


def _user(**overrides) -> AuthenticatedUser:
    data = dict(email="Curator@MSKCC.org", name="Curator", domain="mskcc.org")
    data.update(overrides)
    return AuthenticatedUser(**data)


def test_run_owner_session_user(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)

    assert run_owner(_user()) == {"owner_email": "curator@mskcc.org", "source": "session"}


def test_run_owner_api_key_user_is_the_key_owner(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)

    owner = run_owner(_user(auth_method="api_key", api_key_id="key-1", provider="api_key"))

    assert owner == {"owner_email": "curator@mskcc.org", "source": "api_key"}


def test_run_owner_auth_disabled_is_the_local_user(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)

    assert run_owner(None) == {"owner_email": "local@internal", "source": "local"}
    assert run_owner(_user()) == {"owner_email": "local@internal", "source": "local"}


def test_run_owner_without_a_user_is_unowned(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)

    assert run_owner(None) == {"owner_email": None, "source": None}


# ---------------------------------------------------------------------------
# Every save site records the owner
# ---------------------------------------------------------------------------


class RecordingStore:
    """In-memory RunStore stand-in: api-key lookup plus run saves with owners."""

    def __init__(self):
        self.keys: Dict[str, ApiKeyRecord] = {}
        self.saved: list[dict] = []

    @classmethod
    async def create(cls):
        return _STORE

    async def close(self):
        pass

    async def save_run(self, run_id, timestamp, request_payload, result_payload, **owner):
        self.saved.append({"run_id": run_id, "request": request_payload, **owner})

    async def get_gene_annotation(self, gene, tumor_type=None):
        return None

    async def save_gene_annotation(self, annotation, now, tumor_type=None):
        pass

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        return next((k for k in self.keys.values() if k.key_hash == key_hash), None)

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        pass


_STORE = RecordingStore()


@pytest.fixture
def store(monkeypatch):
    global _STORE
    _STORE = RecordingStore()
    monkeypatch.setattr(main, "RunStore", RecordingStore)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "run-owner-test-secret-123456")
    monkeypatch.setattr(settings, "allowed_email_domains", "mskcc.org")
    monkeypatch.setattr(settings, "allowed_emails", "")
    monkeypatch.setattr(settings, "api_key_rate_limit_per_minute", 1000)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(auth_module, "_redis_client", None)
    monkeypatch.setattr(api_keys_module, "_memory_limiter", InMemoryRateLimiter())

    async def fake_run_pipeline(fusions, local_backend=None, run_store=None, force_refresh=False, **kwargs):
        annotations = [GeneAnnotation(gene=item.fusion.upper(), in_oncokb=True) for item in fusions]
        on_annotation = kwargs.get("on_annotation")
        for annotation in annotations:
            if on_annotation:
                await on_annotation(annotation)
        return AnnotationResult(
            run_id=f"run-{len(_STORE.saved)}",
            timestamp="2026-10-26T12:00:00+00:00",
            fusions_processed=len(fusions),
            genes_annotated=len(annotations),
            annotations=annotations,
        )

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    return _STORE


def _api_key_headers(store: RecordingStore, owner_email: str = "Scripter@mskcc.org") -> dict:
    key = generate_api_key()
    store.keys["key-1"] = ApiKeyRecord(
        id="key-1",
        key_hash=hash_api_key(key),
        owner_email=owner_email,
        name="nightly",
        created_at=datetime.now(timezone.utc),
    )
    return {"Authorization": f"Bearer {key}"}


def _session_cookies(email: str = "curator@mskcc.org") -> dict:
    token = session_token_for(AuthenticatedUser(email=email, name="Curator", domain="mskcc.org"))
    return {settings.auth_cookie_name: token}


def _poll(client, status_url, headers=None, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        body = client.get(status_url, headers=headers).json()
        if body["status"] in ("complete", "failed") or time.monotonic() > deadline:
            return body
        time.sleep(0.02)


# (method, path, json body, how a completed job is polled — None for synchronous routes)
SAVE_SITES = [
    pytest.param("post", "/v1/annotate", {"fusions": ["ALK", "EML4::ALK"]}, None, id="annotate"),
    pytest.param("post", "/v1/annotate/jobs", {"fusions": ["ALK", "EML4::ALK"]}, "/v1/annotate/jobs/", id="annotate_job"),
    pytest.param("post", "/v1/genes/query/jobs", {"genes": ["ALK", "TP53"]}, "/v1/genes/query/jobs/", id="gene_query_job"),
    pytest.param("post", "/v1/annotate/gene", {"gene": "ALK"}, None, id="annotate_gene"),
    pytest.param("post", "/v1/genes/query", {"genes": ["ALK", "TP53"]}, None, id="gene_query"),
    pytest.param("get", "/v1/genes/ALK", None, None, id="gene_query_get"),
]


def _call_save_site(client, method, path, body, job_prefix, *, headers=None):
    kwargs = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 200, response.text
    if job_prefix:
        job = _poll(client, job_prefix + response.json()["job_id"], headers=headers)
        assert job["status"] == "complete", job


@pytest.mark.parametrize("method,path,body,job_prefix", SAVE_SITES)
def test_save_site_records_session_owner(store, method, path, body, job_prefix):
    with TestClient(main.app) as client:
        client.cookies.update(_session_cookies("Curator@mskcc.org"))
        _call_save_site(client, method, path, body, job_prefix)

    assert len(store.saved) == 1
    assert store.saved[0]["owner_email"] == "curator@mskcc.org"
    assert store.saved[0]["source"] == "session"


@pytest.mark.parametrize("method,path,body,job_prefix", SAVE_SITES)
def test_save_site_records_api_key_owner(store, method, path, body, job_prefix):
    with TestClient(main.app) as client:
        headers = _api_key_headers(store, owner_email="Scripter@mskcc.org")
        _call_save_site(client, method, path, body, job_prefix, headers=headers)

    assert len(store.saved) == 1
    assert store.saved[0]["owner_email"] == "scripter@mskcc.org"
    assert store.saved[0]["source"] == "api_key"


@pytest.mark.parametrize("method,path,body,job_prefix", SAVE_SITES)
def test_save_site_with_auth_disabled_records_local_user(store, monkeypatch, method, path, body, job_prefix):
    monkeypatch.setattr(settings, "auth_enabled", False)

    with TestClient(main.app) as client:
        _call_save_site(client, method, path, body, job_prefix)

    assert len(store.saved) == 1
    assert store.saved[0]["owner_email"] == "local@internal"
    assert store.saved[0]["source"] == "local"


# ---------------------------------------------------------------------------
# RunStore against a fake cursor
# ---------------------------------------------------------------------------


class FakeCursor:
    """Answers the statements _ensure_runs_schema issues and records every
    statement; `rows` is returned for any other SELECT.

    `alter_errors` holds one MySQL error code (or None for success) per ALTER,
    consumed in order. A duplicate-column/key error also applies the change,
    as the concurrent pod that caused it would have, unless
    `duplicate_applies` is False (that pod added only some columns).
    """

    def __init__(self, columns=(), indexes=(), rows=(), alter_errors=(), duplicate_applies=True):
        self.columns = set(columns)
        self.indexes = set(indexes)
        self.rows = list(rows)
        self.alter_errors = list(alter_errors)
        self.duplicate_applies = duplicate_applies
        self.lock_wait_timeout = 31536000
        self.executed: list[tuple[str, tuple]] = []
        self._result: list = []

    def _apply_alter(self, sql):
        self.columns.update(part.split()[0] for part in sql.split("ADD COLUMN ")[1:])
        if "ADD INDEX " in sql:
            self.indexes.add(sql.split("ADD INDEX ")[1].split()[0])

    async def execute(self, sql, params=()):
        self.executed.append((sql, params))
        if sql == "SELECT @@SESSION.lock_wait_timeout":
            self._result = [(self.lock_wait_timeout,)]
        elif sql.startswith("SET SESSION lock_wait_timeout"):
            self.lock_wait_timeout = params[0]
        elif sql == "SHOW COLUMNS FROM runs":
            self._result = [(column,) for column in ["run_id", "created_at", *sorted(self.columns)]]
        elif sql.startswith("SHOW INDEX FROM runs"):
            index = sql.split("'")[1]
            self._result = [("runs", 1, index)] if index in self.indexes else []
        elif sql.startswith("ALTER TABLE runs"):
            code = self.alter_errors.pop(0) if self.alter_errors else None
            if code in (1060, 1061) and self.duplicate_applies:
                self._apply_alter(sql)
            if code is not None:
                raise pymysql.err.OperationalError(code, "failed")
            self._apply_alter(sql)
        else:
            self._result = self.rows

    async def fetchone(self):
        return self._result[0] if self._result else None

    async def fetchall(self):
        return list(self._result)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor):
        self._cursor = cursor

    def acquire(self):
        pool = self

        class _Conn:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            def cursor(self):
                return pool._cursor

        return _Conn()


_RUNS_COLUMNS = {"owner_email", "source", "input_summary", "input_count", "genes_annotated"}


def _alters(cursor: FakeCursor) -> list[str]:
    return [sql for sql, _ in cursor.executed if sql.startswith("ALTER")]


async def test_runs_schema_upgrade_adds_columns_instantly_and_index_online_once():
    cursor = FakeCursor()  # a pre-ownership `runs` table
    store = RunStore(FakePool(cursor))

    await store._ensure_runs_schema(cursor)

    assert cursor.columns == _RUNS_COLUMNS
    assert cursor.indexes == {"idx_runs_owner_created_at"}
    add_columns, add_index = _alters(cursor)
    assert add_columns.count("ADD COLUMN") == 5 and add_columns.endswith(", ALGORITHM=INSTANT")
    assert add_index.endswith(", ALGORITHM=INPLACE, LOCK=NONE")

    cursor.executed.clear()
    await store._ensure_runs_schema(cursor)

    assert _alters(cursor) == []


async def test_runs_schema_upgrade_bounds_the_lock_wait_and_restores_it(monkeypatch):
    monkeypatch.setattr(settings, "runs_ddl_lock_wait_timeout_seconds", 7)
    cursor = FakeCursor()

    await RunStore(FakePool(cursor))._ensure_runs_schema(cursor)

    timeouts = [params[0] for sql, params in cursor.executed if sql.startswith("SET SESSION lock_wait_timeout")]
    assert timeouts == [7, 31536000]
    first_alter = next(i for i, (sql, _) in enumerate(cursor.executed) if sql.startswith("ALTER"))
    assert cursor.executed[first_alter - 2][0].startswith("SET SESSION lock_wait_timeout")


async def test_runs_schema_upgrade_adds_only_missing_columns():
    cursor = FakeCursor(columns={"owner_email", "source"})

    await RunStore(FakePool(cursor))._ensure_runs_schema(cursor)

    add_columns = _alters(cursor)[0]
    assert "owner_email" not in add_columns and add_columns.count("ADD COLUMN") == 3
    assert cursor.columns == _RUNS_COLUMNS


@pytest.mark.parametrize("instant_error", [1800, 1845, 1846])
async def test_runs_schema_upgrade_falls_back_to_inplace_without_instant(instant_error):
    cursor = FakeCursor(alter_errors=[instant_error])
    store = RunStore(FakePool(cursor))

    await store._ensure_runs_schema(cursor)

    instant, inplace, _index = _alters(cursor)
    assert instant.endswith("ALGORITHM=INSTANT")
    assert inplace.replace(", ALGORITHM=INPLACE, LOCK=NONE", "") == instant.replace(", ALGORITHM=INSTANT", "")
    assert cursor.columns == _RUNS_COLUMNS


async def test_runs_schema_upgrade_tolerates_a_concurrent_upgrade():
    cursor = FakeCursor(alter_errors=[1060, 1061])
    store = RunStore(FakePool(cursor))

    await store._ensure_runs_schema(cursor)

    assert len(_alters(cursor)) == 2
    assert cursor.columns == _RUNS_COLUMNS


async def test_runs_schema_upgrade_reraises_other_errors():
    cursor = FakeCursor(alter_errors=[1146])  # table doesn't exist

    with pytest.raises(pymysql.err.OperationalError):
        await RunStore(FakePool(cursor))._ensure_runs_schema(cursor)
    assert cursor.lock_wait_timeout == 31536000  # restored even on failure


@pytest.mark.parametrize("alter_errors", [[1205], [1845, 1205]], ids=["instant", "inplace-fallback"])
async def test_column_lock_timeout_fails_startup_with_a_clear_error(alter_errors, monkeypatch):
    monkeypatch.setattr(settings, "runs_ddl_lock_wait_timeout_seconds", 7)
    cursor = FakeCursor(alter_errors=alter_errors)

    with pytest.raises(RunsSchemaUpgradeError) as raised:
        await RunStore(FakePool(cursor))._ensure_runs_schema(cursor)

    message = str(raised.value)
    assert "Timed out after 7s" in message and "refusing to start" in message
    assert "ALTER TABLE runs ADD COLUMN owner_email" in message  # the manual fix
    assert isinstance(raised.value.__cause__, pymysql.err.OperationalError)
    assert cursor.columns == set()
    assert not any("ADD INDEX" in sql for sql in _alters(cursor))
    assert cursor.lock_wait_timeout == 31536000


async def test_columns_still_missing_after_a_concurrent_upgrade_fails_startup():
    cursor = FakeCursor(alter_errors=[1060], duplicate_applies=False)

    with pytest.raises(RunsSchemaUpgradeError, match="still missing run owner columns"):
        await RunStore(FakePool(cursor))._ensure_runs_schema(cursor)


async def test_failed_upgrade_closes_the_pool(monkeypatch):
    closed = []

    class Pool(FakePool):
        def close(self):
            closed.append(True)

        async def wait_closed(self):
            pass

    async def fake_create_pool(**kwargs):
        return Pool(FakeCursor(alter_errors=[1205]))

    monkeypatch.setattr("src.pipeline.run_store.aiomysql.create_pool", fake_create_pool)
    monkeypatch.setattr(RunStore, "_ensure_schema", lambda self: self._ensure_runs_schema(self._pool._cursor))

    with pytest.raises(RunsSchemaUpgradeError):
        await RunStore.create()
    assert closed == [True]


def test_no_code_path_saves_a_run_without_owner_columns():
    source = inspect.getsource(run_store_module)
    inserts = re.findall(r"INSERT INTO runs \(([^)]*)\)", source)

    assert inserts, "expected at least one INSERT INTO runs"
    for columns in inserts:
        assert "owner_email" in columns and "source" in columns and "input_summary" in columns


async def test_index_lock_timeout_keeps_owner_columns_usable(caplog):
    cursor = FakeCursor(alter_errors=[None, 1205])
    store = RunStore(FakePool(cursor))

    with caplog.at_level(logging.WARNING, logger="src.pipeline.run_store"):
        await store._ensure_runs_schema(cursor)

    assert cursor.columns == _RUNS_COLUMNS
    assert cursor.indexes == set()
    assert "idx_runs_owner_created_at" in caplog.text
    assert cursor.lock_wait_timeout == 31536000

    # Startup continued, and new runs still record their owner.
    await store.save_run(
        "run-1", "2026-10-26T12:00:00+00:00", {"gene": "ALK"}, {}, owner_email="A@mskcc.org", source="session"
    )
    sql, params = cursor.executed[-1]
    assert "owner_email, source, input_summary" in sql
    assert params[4:7] == ("a@mskcc.org", "session", "ALK")


async def test_save_run_writes_owner_source_and_summary():
    cursor = FakeCursor()
    store = RunStore(FakePool(cursor))

    await store.save_run(
        "run-1",
        "2026-10-26T12:00:00+00:00",
        {"fusions": [{"fusion": "EML4::ALK"}, {"fusion": "TP53"}]},
        {"run_id": "run-1", "genes_annotated": 3},
        owner_email="Curator@MSKCC.org",
        source="api_key",
    )

    sql, params = cursor.executed[-1]
    assert "owner_email, source, input_summary, input_count, genes_annotated" in sql
    assert params[4:] == ("curator@mskcc.org", "api_key", "EML4::ALK, TP53", 2, 3)


async def test_save_run_without_owner_stays_unowned():
    cursor = FakeCursor()

    await RunStore(FakePool(cursor)).save_run("run-1", "2026-10-26T12:00:00+00:00", {"gene": "ALK"}, {})

    _, params = cursor.executed[-1]
    assert params[4:] == (None, None, "ALK", 1, None)


def _row(run_id: str, second: int):
    return (run_id, datetime(2026, 10, 26, 12, 0, second), "session", run_id.upper(), 1, 1)


async def test_list_runs_for_owner_reads_no_result_blob_and_pages():
    cursor = FakeCursor(rows=[_row("run-c", 3), _row("run-b", 2), _row("run-a", 1)])
    store = RunStore(FakePool(cursor))

    page = await store.list_runs_for_owner("Curator@MSKCC.org", limit=2)

    sql, params = cursor.executed[-1]
    assert "result_json" not in sql and "request_json" not in sql
    assert "ORDER BY created_at DESC, run_id DESC LIMIT %s" in sql
    assert params == ("curator@mskcc.org", 3)
    assert [run.run_id for run in page.runs] == ["run-c", "run-b"]
    assert page.runs[0].created_at == datetime(2026, 10, 26, 12, 0, 3, tzinfo=timezone.utc)
    assert page.runs[0].input_summary == "RUN-C"
    assert page.next_cursor is not None

    cursor.rows = [_row("run-a", 1)]
    page = await store.list_runs_for_owner("curator@mskcc.org", limit=2, cursor=page.next_cursor)

    sql, params = cursor.executed[-1]
    assert "(created_at < %s OR (created_at = %s AND run_id < %s))" in sql
    assert params == (
        "curator@mskcc.org",
        datetime(2026, 10, 26, 12, 0, 2),
        datetime(2026, 10, 26, 12, 0, 2),
        "run-b",
        3,
    )
    assert [run.run_id for run in page.runs] == ["run-a"]
    assert page.next_cursor is None


async def test_list_runs_for_owner_rejects_bad_input():
    store = RunStore(FakePool(FakeCursor()))

    with pytest.raises(ValueError):
        await store.list_runs_for_owner("curator@mskcc.org", cursor="not-a-cursor")
    with pytest.raises(ValueError):
        await store.list_runs_for_owner("curator@mskcc.org", limit=0)


def test_runs_cursor_round_trip():
    created_at = datetime(2026, 10, 26, 12, 0, 2, tzinfo=timezone.utc)

    assert _decode_runs_cursor(_encode_runs_cursor(created_at, "run-b")) == (created_at, "run-b")


def test_runs_cursor_round_trip_keeps_fractional_seconds_and_normalizes_to_utc():
    eastern = timezone(timedelta(hours=-4))
    created_at = datetime(2026, 10, 26, 8, 0, 2, 406639, tzinfo=eastern)

    decoded, run_id = _decode_runs_cursor(_encode_runs_cursor(created_at, "run-b"))

    assert decoded == datetime(2026, 10, 26, 12, 0, 2, 406639, tzinfo=timezone.utc)
    assert decoded.utcoffset() == timedelta(0)
    assert run_id == "run-b"


def _raw_cursor(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


@pytest.mark.parametrize(
    "cursor",
    [
        pytest.param("", id="empty"),
        pytest.param("not-a-cursor", id="garbage"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00|run-b") + "!", id="invalid-base64-char"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00|run-b").replace("_", "/") + "+/", id="std-alphabet"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00|run-b") + "=", id="stray-padding"),
        pytest.param("A", id="truncated"),
        pytest.param(base64.urlsafe_b64encode(b"\xff\xfe|run").decode(), id="not-utf8"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00"), id="no-separator"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00|"), id="empty-run-id"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00|a|b"), id="extra-separator"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02+00:00|" + "x" * 37), id="run-id-too-long"),
        pytest.param(_raw_cursor("yesterday|run-b"), id="bad-timestamp"),
        pytest.param(_raw_cursor("2026-10-26T12:00:02|run-b"), id="naive-timestamp"),
        pytest.param(_raw_cursor("0001-01-01T00:00:00+05:00|run-b"), id="timezone-underflow"),
        pytest.param(_raw_cursor("9999-12-31T23:59:59-05:00|run-b"), id="timezone-overflow"),
        pytest.param(_raw_cursor("0999-12-31T00:00:00+00:00|run-b"), id="before-mysql-datetime"),
    ],
)
def test_malformed_runs_cursor_is_a_value_error(cursor):
    with pytest.raises(ValueError, match="Invalid runs cursor"):
        _decode_runs_cursor(cursor)


async def test_list_runs_for_owner_rejects_malformed_cursor_before_querying():
    cursor = FakeCursor()

    with pytest.raises(ValueError):
        await RunStore(FakePool(cursor)).list_runs_for_owner("a@mskcc.org", cursor=_raw_cursor("x|y"))
    assert cursor.executed == []


async def test_list_runs_limit_one_pages_between_equal_timestamps():
    same_second = [_row("run-c", 2), _row("run-b", 2), _row("run-a", 2)]
    cursor = FakeCursor(rows=same_second[:2])
    store = RunStore(FakePool(cursor))

    page = await store.list_runs_for_owner("a@mskcc.org", limit=1)
    assert [run.run_id for run in page.runs] == ["run-c"]

    cursor.rows = same_second[1:]
    page = await store.list_runs_for_owner("a@mskcc.org", limit=1, cursor=page.next_cursor)

    _, params = cursor.executed[-1]
    # Resumes inside the same second, strictly after run-c.
    assert params[1:] == (datetime(2026, 10, 26, 12, 0, 2), datetime(2026, 10, 26, 12, 0, 2), "run-c", 2)
    assert [run.run_id for run in page.runs] == ["run-b"]


def test_multibyte_summary_is_bounded_in_characters():
    payload = {"fusions": [{"fusion": "融合遺伝子" * 40 + f"::{i}"} for i in range(6)]}

    summary, count = summarize_run_inputs(payload)

    assert len(summary) == INPUT_SUMMARY_MAX_LENGTH
    assert summary.endswith("… (+1 more)")
    assert summary.encode("utf-8").decode("utf-8") == summary
    assert count == 6
