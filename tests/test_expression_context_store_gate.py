"""Module stores (in-memory + real MySQL when available)."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from src.expression_context.bundle_store import (
    BUNDLE_TABLES,
    CREATE_BUNDLE_TABLES_SQL,
    STATE_TABLE,
    BundleRecord,
    InMemoryBundleStore,
    LoaderLockNotHeld,
    MySQLBundleStore,
)

from src.expression_context.models import HistoryEntry, StoredPair
from src.expression_context.store import (
    CREATE_TABLE_SQL,
    TABLE_NAME,
    InMemoryExpressionContextStore,
    InvalidCursor,
    MySQLExpressionContextStore,
    decode_cursor,
    encode_cursor,
)
from tests.expression_context_helpers import gene_file

GATE = "acgc_tumor_specific_v2"


def _pair(symbol, code, release="r1", gate=GATE, minutes_ago=0, surfaced=False, evidence=None):
    return StoredPair(
        hugo_symbol=symbol, oncotree_code=code, release_id=release, literature_gate=gate,
        surfaced=surfaced, exclusion_reason=None if surfaced else "stats",
        stats={**gene_file()["cancer_types"][0], "oncotree_code": code},
        evidence=evidence,
        computed_at=datetime.now(timezone.utc).replace(microsecond=0)
        - timedelta(minutes=minutes_ago),
        requested_by="alice@example.org",
    )


# ---------------------------------------------------------------- cursors


def test_cursor_round_trip():
    entry = HistoryEntry(
        hugo_symbol="ERBB2", release_id="r1", literature_gate=GATE,
        computed_at=datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc), pair_count=1,
        surfaced_count=0,
    )
    assert decode_cursor(encode_cursor(entry)) == (entry.computed_at, "ERBB2", "r1")


@pytest.mark.parametrize("bad", ["", "!!!", "bm90IGpzb24", "WzEsMl0"])
def test_bad_cursor_rejected(bad):
    with pytest.raises(InvalidCursor):
        decode_cursor(bad)


def test_ddl_is_module_owned_and_keyed_as_contracted():
    assert TABLE_NAME == "expression_context_results"
    assert "CREATE TABLE IF NOT EXISTS expression_context_results" in CREATE_TABLE_SQL
    assert "UNIQUE KEY" in CREATE_TABLE_SQL
    assert "(hugo_symbol, oncotree_code, release_id, literature_gate)" in CREATE_TABLE_SQL
    assert "FOREIGN KEY" not in CREATE_TABLE_SQL.upper()
    for column in ("surfaced", "exclusion_reason", "stats_json", "evidence_json",
                   "computed_at", "requested_by"):
        assert column in CREATE_TABLE_SQL


# ------------------------------------------------------------ store contract


async def _exercise_store(store):
    await store.upsert_pairs([
        _pair("ERBB2", "BRCA", release="r1", minutes_ago=10, surfaced=True,
              evidence={"pmids": ["1"]}),
        _pair("ERBB2", "STAD", release="r1", minutes_ago=10),
        _pair("ERBB2", "BRCA", release="r2", minutes_ago=5),
        _pair("EGFR", "LUAD", release="r2", minutes_ago=1, surfaced=True),
        _pair("EGFR", "LUAD", release="r2", gate="other_gate", minutes_ago=0),
    ])
    pairs = await store.get_pairs("ERBB2", "r1", GATE)
    assert set(pairs) == {"BRCA", "STAD"}
    assert pairs["BRCA"].evidence == {"pmids": ["1"]} and pairs["BRCA"].surfaced is True
    assert pairs["BRCA"].computed_at.tzinfo is not None

    # Upsert replaces on the unique key.
    await store.upsert_pairs([_pair("ERBB2", "STAD", release="r1", minutes_ago=10, surfaced=True)])
    assert (await store.get_pairs("ERBB2", "r1", GATE))["STAD"].surfaced is True

    latest = await store.latest_pairs("ERBB2", GATE)
    assert {(p.release_id, p.oncotree_code) for p in latest} == {("r2", "BRCA")}
    assert await store.latest_pairs("NOPE", GATE) == []

    history = await store.list_history(GATE, 10, None)
    assert [(e.hugo_symbol, e.release_id) for e in history] == [
        ("EGFR", "r2"), ("ERBB2", "r2"), ("ERBB2", "r1"),
    ]
    assert history[2].pair_count == 2 and history[2].surfaced_count == 2
    after = (history[0].computed_at, history[0].hugo_symbol, history[0].release_id)
    page = await store.list_history(GATE, 1, after)
    assert [(e.hugo_symbol, e.release_id) for e in page] == [("ERBB2", "r2")]


async def test_in_memory_store_contract():
    store = InMemoryExpressionContextStore()
    await store.ensure_schema()
    assert store.schema_created
    await _exercise_store(store)


@pytest.fixture
async def mysql_store():
    try:
        store = await MySQLExpressionContextStore.create()
        await store.ensure_schema()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(f"DELETE FROM {TABLE_NAME}")
    yield store
    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
    await store.close()


async def test_mysql_store_contract(mysql_store):
    await _exercise_store(mysql_store)


# ------------------------------------------------------------- bundle store


def test_bundle_ddl_is_module_owned():
    assert BUNDLE_TABLES == (
        "expression_context_bundles", "expression_context_stats",
        "expression_context_aliases", "expression_context_state",
    )
    ddl = "\n".join(CREATE_BUNDLE_TABLES_SQL)
    for table in BUNDLE_TABLES:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in ddl
    assert "FOREIGN KEY" not in ddl.upper()
    assert "PRIMARY KEY (bundle_tag, hugo_symbol)" in ddl
    assert "PRIMARY KEY (bundle_tag, alias)" in ddl


def _record(tag, release_id="datahub-aaaaaaaaaaaa-m2-bbbbbbbbbbbb", status="loading"):
    return BundleRecord(
        bundle_tag=tag, release_id=release_id, schema_version="1", method_version="2",
        pubmed_snapshot_date=date(2026, 11, 2), gene_count=2, sha256="a" * 64, size_bytes=10,
        manifest={"schema_version": "1", "release_id": release_id, "cancer_types": []},
        status=status, load_started_at=datetime(2026, 11, 6, tzinfo=timezone.utc),
        published_at=datetime(2026, 11, 2, 6, tzinfo=timezone.utc),
        release_url=f"https://github.com/o/r/releases/tag/{tag}",
    )


async def _exercise_bundle_store(bundles):
    t0 = datetime(2026, 11, 6, 12, 0, 0, 123456, tzinfo=timezone.utc)
    assert (await bundles.get_state()).active_bundle_tag is None
    L = "loader"
    assert await bundles.acquire_loader_lock(L, 60)

    await bundles.begin_load(_record("tag-a"), L)
    await bundles.add_genes("tag-a", [("ERBB2", 2064, b'{"g": "erbb2"}'), ("MYCN", None, b'{"g": 2}')], L)
    await bundles.add_aliases("tag-a", [("HER2", "ERBB2"), ("NEU", "ERBB2")], L)
    assert not await bundles.activate("tag-a", t0, L)  # never while loading
    assert (await bundles.get_state()).active_bundle_tag is None
    await bundles.finish_load("tag-a", 2, t0, L)
    assert await bundles.activate("tag-a", t0, L)
    record = await bundles.get_bundle("tag-a")
    assert record.status == "ready" and record.loaded_rows == 2 and record.finished_at == t0
    assert record.pubmed_snapshot_date == date(2026, 11, 2)
    assert record.published_at == datetime(2026, 11, 2, 6, tzinfo=timezone.utc)
    assert record.manifest["release_id"].startswith("datahub-")
    assert await bundles.get_gene_doc("tag-a", "ERBB2") == b'{"g": "erbb2"}'
    assert await bundles.get_gene_doc("tag-a", "NOPE") is None
    assert await bundles.get_alias("tag-a", "HER2") == "ERBB2"
    assert await bundles.get_alias("tag-a", "XYZ") is None
    state = await bundles.get_state()
    assert state.active_bundle_tag == "tag-a" and state.activated_at == t0

    # A failed load cleans up after itself and never touches the pointer.
    await bundles.begin_load(_record("tag-bad"), L)
    await bundles.add_genes("tag-bad", [("ERBB2", 2064, b"{}")], L)
    await bundles.fail_load("tag-bad", "sha mismatch" * 200, t0, L)
    bad = await bundles.get_bundle("tag-bad")
    assert bad.status == "failed" and len(bad.error) == 1000
    assert await bundles.get_gene_doc("tag-bad", "ERBB2") is None
    assert not await bundles.activate("tag-bad", t0, L)
    assert (await bundles.get_state()).active_bundle_tag == "tag-a"

    # Re-loading a tag replaces whatever rows it had (idempotent import).
    await bundles.begin_load(_record("tag-b"), L)
    await bundles.add_genes("tag-b", [("STALE", None, b"{}")], L)
    await bundles.begin_load(_record("tag-b"), L)
    await bundles.add_genes("tag-b", [("ERBB2", 2064, b'{"v": 2}'), ("MYCN", None, b"{}")], L)
    await bundles.add_genes("tag-b", [("ERBB2", 2064, b'{"v": 3}')], L)  # upsert
    await bundles.finish_load("tag-b", 2, t0, L)
    assert await bundles.get_gene_doc("tag-b", "STALE") is None
    assert await bundles.get_gene_doc("tag-b", "ERBB2") == b'{"v": 3}'
    assert await bundles.activate("tag-b", t0 + timedelta(hours=1), L)
    assert await bundles.activate("tag-b", t0 + timedelta(hours=2), L)  # same tag: previous kept
    state = await bundles.get_state()
    assert (state.active_bundle_tag, state.previous_bundle_tag) == ("tag-b", "tag-a")
    assert (await bundles.get_bundle("tag-a")).retired_at is None  # still the previous

    # Pruning: grace on the database clock, exclusions from the current pointers.
    grace = 0.3
    assert await bundles.prune(L, grace) == []  # tag-bad (never active) starts its grace now
    assert (await bundles.get_bundle("tag-bad")).retired_at is not None
    await asyncio.sleep(grace + 0.1)
    assert await bundles.prune(L, grace) == ["tag-bad"]
    assert await bundles.get_bundle("tag-bad") is None
    await bundles.begin_load(_record("tag-c"), L)
    await bundles.add_genes("tag-c", [("ERBB2", 2064, b'{"v": "c"}')], L)
    await bundles.finish_load("tag-c", 1, t0, L)
    assert await bundles.activate("tag-c", t0, L)  # tag-a leaves the pair: retired at the swap
    assert (await bundles.get_bundle("tag-a")).retired_at is not None
    assert await bundles.prune(L, grace) == []  # inside the grace period
    assert await bundles.get_gene_doc("tag-a", "ERBB2") == b'{"g": "erbb2"}'
    await asyncio.sleep(grace + 0.1)
    assert await bundles.activate("tag-a", t0, L)  # rollback: tag-a is active again
    assert (await bundles.get_bundle("tag-a")).retired_at is None
    state = await bundles.get_state()
    assert (state.active_bundle_tag, state.previous_bundle_tag) == ("tag-a", "tag-c")
    assert await bundles.prune(L, grace) == []  # tag-b retired just now; tag-a kept
    await asyncio.sleep(grace + 0.1)
    assert await bundles.prune(L, grace) == ["tag-b"]
    assert await bundles.get_gene_doc("tag-b", "ERBB2") is None
    assert await bundles.get_gene_doc("tag-a", "ERBB2") == b'{"g": "erbb2"}'
    assert await bundles.get_alias("tag-a", "HER2") == "ERBB2"

    retry = t0 + timedelta(minutes=15)
    await bundles.mark_error(t0, "rate limited", retry)
    state = await bundles.get_state()
    assert (state.last_error, state.last_error_at, state.next_check_after) == ("rate limited", t0, retry)
    await bundles.mark_checked(retry)
    state = await bundles.get_state()
    assert state.last_checked_at == retry and state.last_error is None
    assert state.next_check_after is None and state.active_bundle_tag == "tag-a"
    await bundles.release_loader_lock(L)

    # The single-loader lease: compare-and-set acquire/renew, owner-only release.
    assert await bundles.acquire_loader_lock("pod-a", 0.3)
    assert not await bundles.acquire_loader_lock("pod-b", 0.3)
    assert await bundles.renew_loader_lock("pod-a", 0.3)
    assert not await bundles.renew_loader_lock("pod-b", 0.3)
    await bundles.release_loader_lock("pod-b")  # not the owner: no effect
    assert not await bundles.acquire_loader_lock("pod-b", 0.3)
    await asyncio.sleep(0.4)  # pod-a's lease expires (judged on the store's clock)
    assert not await bundles.renew_loader_lock("pod-a", 0.3)
    assert await bundles.acquire_loader_lock("pod-b", 0.3)
    # Every write is fenced on holding the lease.
    assert not await bundles.activate("tag-c", t0, "pod-a")
    with pytest.raises(LoaderLockNotHeld):
        await bundles.add_genes("tag-c", [("TP53", 7157, b"{}")], "pod-a")
    assert await bundles.activate("tag-c", t0, "pod-b")
    await bundles.release_loader_lock("pod-a")  # stale owner: no effect
    assert not await bundles.acquire_loader_lock("pod-c", 0.3)
    await bundles.release_loader_lock("pod-b")
    assert await bundles.acquire_loader_lock("pod-c", 0.3)
    await bundles.release_loader_lock("pod-c")


async def test_in_memory_bundle_store_contract():
    bundles = InMemoryBundleStore()
    await bundles.ensure_schema()
    assert bundles.schema_created
    await _exercise_bundle_store(bundles)


@pytest.fixture
async def mysql_bundle_store():
    try:
        store = await MySQLExpressionContextStore.create(maxsize=4)
        bundles = MySQLBundleStore(store.pool)
        await bundles.ensure_schema()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    async with store.pool.acquire() as conn:
        async with conn.cursor() as cursor:
            for table in BUNDLE_TABLES:
                await cursor.execute(f"DROP TABLE IF EXISTS {table}")
    await bundles.ensure_schema()
    await bundles.ensure_schema()  # idempotent; the state row is created once
    async with store.pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(f"SELECT COUNT(*) FROM {STATE_TABLE}")
            assert (await cursor.fetchone())[0] == 1
    yield bundles
    async with store.pool.acquire() as conn:
        async with conn.cursor() as cursor:
            for table in BUNDLE_TABLES:
                await cursor.execute(f"DROP TABLE IF EXISTS {table}")
    await store.close()


async def test_mysql_bundle_store_contract(mysql_bundle_store):
    await _exercise_bundle_store(mysql_bundle_store)


@pytest.mark.parametrize(
    "overrides",
    [
        {},
        {"db_url": "jdbc:mysql://db.internal:3307/acgc?useSSL=false",
         "db_username": "svc", "db_password": "pw"},
        {"mysql_host": "h", "mysql_port": 1234, "mysql_user": "u", "mysql_password": "p",
         "mysql_database": "d"},
    ],
)
def test_connection_kwargs_match_run_store(monkeypatch, overrides):
    """The module derives its pool kwargs from public settings with the same
    precedence as the RunStore (compared here only; the module never imports it)."""
    from src.config import settings
    from src.expression_context.store import mysql_connection_kwargs
    from src.pipeline import run_store

    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    assert mysql_connection_kwargs(settings) == run_store._mysql_connection_kwargs()


def test_connection_kwargs_reject_bad_db_url(monkeypatch):
    from src.config import settings
    from src.expression_context.store import mysql_connection_kwargs

    monkeypatch.setattr(settings, "db_url", "mysql://nope")
    with pytest.raises(ValueError):
        mysql_connection_kwargs(settings)


def test_module_does_not_import_run_store_private_helpers():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "src/expression_context/store.py").read_text()
    assert "_mysql_connection_kwargs" not in source
    assert "run_store" not in source
