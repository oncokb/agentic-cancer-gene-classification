"""Integration tests for the MySQL-backed run store.

These run against a real MySQL instance (e.g. `docker compose up mysql`,
or the MYSQL_* env vars pointed at any reachable instance) — no mocking,
since the point is to round-trip through real SQL. Skipped cleanly if no
MySQL is reachable, so the rest of the suite isn't blocked by it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.config import settings
from src.models.schema import GeneAnnotation
from src.pipeline.run_store import (
    RunStore,
    _mysql_connection_kwargs,
    _parse_jdbc_mysql_host_port,
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
