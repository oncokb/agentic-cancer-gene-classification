"""Integration tests for the MySQL-backed run store.

These run against a real MySQL instance (e.g. `docker compose up mysql`,
or the MYSQL_* env vars pointed at any reachable instance) — no mocking,
since the point is to round-trip through real SQL. Skipped cleanly if no
MySQL is reachable, so the rest of the suite isn't blocked by it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.models.schema import GeneAnnotation
from src.pipeline.run_store import RunStore


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


async def test_save_and_get_round_trip(run_store):
    await run_store.save_run(
        "11111111-1111-1111-1111-111111111111",
        "2026-07-30T19:24:12.406639+00:00",
        {"fusions": ["TP53::BRAF"]},
        {"run_id": "11111111-1111-1111-1111-111111111111", "annotations": []},
    )

    stored = await run_store.get_run("11111111-1111-1111-1111-111111111111")

    assert stored == {"run_id": "11111111-1111-1111-1111-111111111111", "annotations": []}


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

    await run_store.save_gene_annotation(annotation, updated_at)
    stored = await run_store.get_gene_annotation("BRAF")

    assert stored is not None
    assert stored["annotation"]["gene"] == "BRAF"
    assert stored["annotation"]["evidence_support_score"] == 0.8
    assert stored["updated_at"] == updated_at

    await run_store.mark_gene_pubmed_checked("BRAF", updated_at, annotation)
    stored = await run_store.get_gene_annotation("BRAF")

    assert stored is not None
    assert stored["last_pubmed_checked_at"] == updated_at
