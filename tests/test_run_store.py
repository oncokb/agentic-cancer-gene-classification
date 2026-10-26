"""Integration tests for the MySQL-backed run store.

These run against a real MySQL instance (e.g. `docker compose up mysql`,
or the MYSQL_* env vars pointed at any reachable instance) — no mocking,
since the point is to round-trip through real SQL. Skipped cleanly if no
MySQL is reachable, so the rest of the suite isn't blocked by it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import aiomysql
import pytest

from src.config import settings
from src.models.schema import GeneAnnotation
from src.pipeline.run_store import (
    RunStore,
    _mysql_connection_kwargs,
    _parse_jdbc_mysql_host_port,
    summarize_run_inputs,
)


def test_parse_jdbc_mysql_host_port_bare():
    assert _parse_jdbc_mysql_host_port("jdbc:mysql://db.example.com:3306") == (
        "db.example.com",
        3306,
    )


def test_parse_jdbc_mysql_host_port_with_db_and_query_params():
    assert _parse_jdbc_mysql_host_port(
        "jdbc:mysql://db.example.com:3306/mydb?useSSL=false"
    ) == ("db.example.com", 3306)


def test_parse_jdbc_mysql_host_port_rejects_non_jdbc_url():
    with pytest.raises(ValueError, match="Expected a jdbc:mysql"):
        _parse_jdbc_mysql_host_port("mysql://db.example.com:3306")


def test_parse_jdbc_mysql_host_port_requires_port():
    with pytest.raises(ValueError, match="missing port"):
        _parse_jdbc_mysql_host_port("jdbc:mysql://db.example.com")


def test_mysql_connection_kwargs_prefers_db_url_over_mysql_host_port(monkeypatch):
    monkeypatch.setattr(settings, "db_url", "jdbc:mysql://from-jdbc:3307")
    monkeypatch.setattr(settings, "mysql_host", "from-discrete-fields")
    monkeypatch.setattr(settings, "mysql_port", 9999)
    monkeypatch.setattr(settings, "db_username", "")
    monkeypatch.setattr(settings, "db_password", "")
    monkeypatch.setattr(settings, "mysql_user", "discrete-user")
    monkeypatch.setattr(settings, "mysql_password", "discrete-pass")
    monkeypatch.setattr(settings, "mysql_database", "mydb")

    kwargs = _mysql_connection_kwargs()

    assert kwargs == {
        "host": "from-jdbc",
        "port": 3307,
        "user": "discrete-user",
        "password": "discrete-pass",
        "db": "mydb",
    }


def test_mysql_connection_kwargs_prefers_db_username_password_when_set(monkeypatch):
    monkeypatch.setattr(settings, "db_url", "jdbc:mysql://from-jdbc:3307")
    monkeypatch.setattr(settings, "db_username", "admin")
    monkeypatch.setattr(settings, "db_password", "admin-secret")
    monkeypatch.setattr(settings, "mysql_user", "discrete-user")
    monkeypatch.setattr(settings, "mysql_password", "discrete-pass")
    monkeypatch.setattr(settings, "mysql_database", "mydb")

    kwargs = _mysql_connection_kwargs()

    assert kwargs["user"] == "admin"
    assert kwargs["password"] == "admin-secret"


def test_mysql_connection_kwargs_falls_back_to_discrete_fields_when_no_db_url(
    monkeypatch,
):
    monkeypatch.setattr(settings, "db_url", "")
    monkeypatch.setattr(settings, "mysql_host", "localhost")
    monkeypatch.setattr(settings, "mysql_port", 3306)
    monkeypatch.setattr(settings, "db_username", "")
    monkeypatch.setattr(settings, "db_password", "")
    monkeypatch.setattr(settings, "mysql_user", "agcg")
    monkeypatch.setattr(settings, "mysql_password", "agcg")
    monkeypatch.setattr(settings, "mysql_database", "agcg")

    kwargs = _mysql_connection_kwargs()

    assert kwargs == {
        "host": "localhost",
        "port": 3306,
        "user": "agcg",
        "password": "agcg",
        "db": "agcg",
    }


@pytest.fixture
async def run_store():
    try:
        store = await RunStore.create()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM gene_annotations")
            await cursor.execute("DELETE FROM runs")
    yield store
    await store.close()


async def test_run_store_connects_via_jdbc_db_url(monkeypatch):
    """Real connection through the DB_URL path, against the same MySQL the
    other tests use — proves the JDBC parsing produces a genuinely working
    connection, not just correctly-shaped kwargs."""
    monkeypatch.setattr(
        settings, "db_url", f"jdbc:mysql://{settings.mysql_host}:{settings.mysql_port}"
    )
    monkeypatch.setattr(settings, "db_username", settings.mysql_user)
    monkeypatch.setattr(settings, "db_password", settings.mysql_password)

    try:
        store = await RunStore.create()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")

    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT 1")
            row = await cursor.fetchone()
    await store.close()

    assert row == (1,)


async def test_save_and_get_round_trip(run_store):
    await run_store.save_run(
        "11111111-1111-1111-1111-111111111111",
        "2026-07-30T19:24:12.406639+00:00",
        {"fusions": ["TP53::BRAF"]},
        {"run_id": "11111111-1111-1111-1111-111111111111", "annotations": []},
    )

    stored = await run_store.get_run("11111111-1111-1111-1111-111111111111")

    assert stored == {"run_id": "11111111-1111-1111-1111-111111111111", "annotations": []}


async def test_update_run_result_overwrites_saved_result(run_store):
    await run_store.save_run(
        "22222222-2222-2222-2222-222222222222",
        "2026-07-30T19:24:12.406639+00:00",
        {"fusions": ["EML4::ALK"]},
        {"run_id": "22222222-2222-2222-2222-222222222222", "annotations": [], "fusion_evidence": []},
    )

    await run_store.update_run_result(
        "22222222-2222-2222-2222-222222222222",
        {
            "run_id": "22222222-2222-2222-2222-222222222222",
            "annotations": [],
            "fusion_evidence": [{"fusion": "EML4::ALK", "well_supported": True}],
        },
    )

    stored = await run_store.get_run("22222222-2222-2222-2222-222222222222")

    assert stored["fusion_evidence"] == [{"fusion": "EML4::ALK", "well_supported": True}]


async def test_get_run_returns_none_for_missing_id(run_store):
    stored = await run_store.get_run("does-not-exist")

    assert stored is None


async def test_gene_annotation_cache_round_trip(run_store):
    annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["TP53::BRAF"],
        in_oncokb=True,
        cancer_associated=True,
        citations=["12345"],
        insufficient_evidence=False,
        evidence_support_score=0.8,
        evidence_support_explanation="Strong evidence support.",
        cache_status="refreshed",
    )
    updated_at = datetime(2026, 7, 30, 19, 24, 12, tzinfo=timezone.utc)

    await run_store.save_gene_annotation(annotation, updated_at, tumor_type="LUAD")
    stored = await run_store.get_gene_annotation("BRAF", tumor_type="luad")

    assert stored is not None
    assert stored["annotation"]["gene"] == "BRAF"
    assert stored["annotation"]["evidence_support_score"] == 0.8
    assert stored["updated_at"] == updated_at
    assert stored["tumor_type"] == "luad"

    assert await run_store.get_gene_annotation("BRAF", tumor_type="melanoma") is None

    await run_store.mark_gene_pubmed_checked("BRAF", updated_at, annotation, tumor_type="LUAD")
    stored = await run_store.get_gene_annotation("BRAF", tumor_type="LUAD")

    assert stored is not None
    assert stored["last_pubmed_checked_at"] == updated_at


# -- Run ownership ------------------------------------------------------------

_LEGACY_RUNS_SQL = """
CREATE TABLE runs (
    run_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NOT NULL
)
"""


async def _runs_columns(store) -> set[str]:
    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SHOW COLUMNS FROM runs")
            return {row[0] for row in await cursor.fetchall()}


async def test_schema_upgrade_adds_owner_columns_to_a_legacy_runs_table(run_store):
    async with run_store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DROP TABLE runs")
            await cursor.execute(_LEGACY_RUNS_SQL)
            await cursor.execute(
                "INSERT INTO runs VALUES (%s, %s, %s, %s)",
                ("legacy-run", datetime(2026, 1, 1), '{"fusions": ["ALK"]}', '{"run_id": "legacy-run"}'),
            )

    await run_store._ensure_schema()
    await run_store._ensure_schema()  # idempotent on an already-upgraded table

    assert {"owner_email", "source", "input_summary", "input_count", "genes_annotated"} <= (
        await _runs_columns(run_store)
    )
    async with run_store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SHOW INDEX FROM runs WHERE Key_name = 'idx_runs_owner_created_at'")
            assert [row[4] for row in await cursor.fetchall()] == ["owner_email", "created_at", "run_id"]
            await cursor.execute("SELECT owner_email, source FROM runs WHERE run_id = 'legacy-run'")
            assert await cursor.fetchone() == (None, None)
    # Pre-existing runs stay openable by link but are in no one's list.
    assert await run_store.get_run("legacy-run") == {"run_id": "legacy-run"}
    assert (await run_store.list_runs_for_owner("local@internal")).runs == []


async def test_list_runs_for_owner_newest_first_paged_and_scoped(run_store):
    for i, (owner, second) in enumerate(
        [("curator@mskcc.org", 1), ("other@mskcc.org", 2), ("curator@mskcc.org", 3), ("curator@mskcc.org", 3)]
    ):
        await run_store.save_run(
            f"run-{i}",
            f"2026-10-26T12:00:0{second}+00:00",
            {"fusions": [{"fusion": "ALK"}, {"fusion": f"GENE{i}"}]},
            {"run_id": f"run-{i}", "genes_annotated": i, "annotations": [{"gene": "ALK"}]},
            owner_email=owner,
            source="session" if i else "api_key",
        )

    first = await run_store.list_runs_for_owner("Curator@mskcc.org", limit=2)
    # Same-second runs tie-break on run_id, newest first.
    assert [run.run_id for run in first.runs] == ["run-3", "run-2"]
    assert first.runs[0].created_at == datetime(2026, 10, 26, 12, 0, 3, tzinfo=timezone.utc)
    assert first.runs[0].input_summary == "ALK, GENE3"
    assert (first.runs[0].input_count, first.runs[0].genes_annotated) == (2, 3)
    assert not hasattr(first.runs[0], "result")

    second = await run_store.list_runs_for_owner("curator@mskcc.org", limit=2, cursor=first.next_cursor)
    assert [run.run_id for run in second.runs] == ["run-0"]
    assert second.runs[0].source == "api_key"
    assert second.next_cursor is None


async def test_list_runs_limit_one_pages_through_same_second_runs(run_store):
    for run_id in ("run-a", "run-b", "run-c"):
        await run_store.save_run(
            run_id, "2026-10-26T12:00:02+00:00", {"gene": "ALK"}, {}, owner_email="a@mskcc.org"
        )

    seen, cursor = [], None
    while True:
        page = await run_store.list_runs_for_owner("a@mskcc.org", limit=1, cursor=cursor)
        seen += [run.run_id for run in page.runs]
        if page.next_cursor is None:
            break
        cursor = page.next_cursor

    assert seen == ["run-c", "run-b", "run-a"]


async def test_list_runs_orders_fractional_and_non_utc_timestamps_by_instant(run_store):
    saved = {
        "run-east": "2026-10-26T08:00:01.300000-04:00",  # 12:00:01.3 UTC
        "run-utc": "2026-10-26T11:00:02.200000+00:00",  # 11:00:02.2 UTC
        "run-west": "2026-10-26T13:30:03.100000+01:30",  # 12:00:03.1 UTC
    }
    for run_id, created_at in saved.items():
        await run_store.save_run(run_id, created_at, {"gene": "ALK"}, {}, owner_email="a@mskcc.org")

    first = await run_store.list_runs_for_owner("a@mskcc.org", limit=2)
    rest = await run_store.list_runs_for_owner("a@mskcc.org", limit=2, cursor=first.next_cursor)

    assert [run.run_id for run in first.runs + rest.runs] == ["run-west", "run-east", "run-utc"]
    # DATETIME keeps whole seconds, in UTC.
    assert first.runs[0].created_at == datetime(2026, 10, 26, 12, 0, 3, tzinfo=timezone.utc)


async def test_update_run_result_keeps_owner_and_listing_fields(run_store):
    await run_store.save_run(
        "run-1",
        "2026-10-26T12:00:00+00:00",
        {"fusions": [{"fusion": "EML4::ALK"}]},
        {"run_id": "run-1", "genes_annotated": 2},
        owner_email="a@mskcc.org",
        source="api_key",
    )

    await run_store.update_run_result("run-1", {"run_id": "run-1", "fusion_evidence": []})

    [run] = (await run_store.list_runs_for_owner("a@mskcc.org")).runs
    assert (run.run_id, run.source, run.input_summary, run.input_count, run.genes_annotated) == (
        "run-1",
        "api_key",
        "EML4::ALK",
        1,
        2,
    )
    assert await run_store.get_run("run-1") == {"run_id": "run-1", "fusion_evidence": []}


async def test_multibyte_input_summary_round_trips(run_store):
    inputs = ["融合遺伝子" * 40 + f"::{i}" for i in range(6)] + ["ÉML4::ALK 🧬"]
    expected, _ = summarize_run_inputs({"fusions": inputs})

    await run_store.save_run(
        "run-1", "2026-10-26T12:00:00+00:00", {"fusions": inputs}, {}, owner_email="a@mskcc.org"
    )
    await run_store.save_run(
        "run-2", "2026-10-26T12:00:01+00:00", {"fusions": ["ÉML4::ALK 🧬"]}, {}, owner_email="a@mskcc.org"
    )

    runs = (await run_store.list_runs_for_owner("a@mskcc.org")).runs
    assert [run.input_summary for run in runs] == ["ÉML4::ALK 🧬", expected]
    assert len(expected) == 255


async def test_schema_upgrade_lock_timeout_starts_degraded(run_store, monkeypatch):
    """A long-running reader holding `runs` makes the column ALTER wait; it
    gives up after the configured timeout instead of stalling startup."""
    async with run_store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DROP TABLE runs")
            await cursor.execute(_LEGACY_RUNS_SQL)
    monkeypatch.setattr(settings, "runs_ddl_lock_wait_timeout_seconds", 1)

    holder = await aiomysql.connect(**_mysql_connection_kwargs(), autocommit=False)
    try:
        async with holder.cursor() as cursor:
            await cursor.execute("BEGIN")
            await cursor.execute("SELECT COUNT(*) FROM runs")  # holds a metadata lock until commit
        await run_store._ensure_schema()
    finally:
        await holder.rollback()
        holder.close()

    assert not run_store._owner_columns_ready
    assert "owner_email" not in await _runs_columns(run_store)
    await run_store.save_run("run-1", "2026-10-26T12:00:00+00:00", {"gene": "ALK"}, {}, owner_email="a@mskcc.org")
    assert await run_store.get_run("run-1") == {}
    assert (await run_store.list_runs_for_owner("a@mskcc.org")).runs == []

    await run_store._ensure_schema()  # lock released: the next startup completes the upgrade

    assert run_store._owner_columns_ready
    assert "owner_email" in await _runs_columns(run_store)
