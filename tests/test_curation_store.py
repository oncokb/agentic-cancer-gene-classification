"""Tests for the curated_overrides / curation_history store.

Lock-error retry runs against a scripted fake pool. The rest run against a
real MySQL like tests/test_run_store.py and are skipped when none is reachable.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

import pymysql
import pytest

from src.pipeline import curation_store
from src.pipeline.curation_store import CurationStorageContention, CurationVersionConflict
from src.pipeline.run_store import RunStore

SUBJECT = {"subject_type": "gene", "subject_key": "CURTEST1", "tumor_type": "luad"}


# ---------------------------------------------------------------------------
# Lock-error retry (scripted fake pool; no MySQL needed)
# ---------------------------------------------------------------------------


class _ScriptedCursor:
    def __init__(self, pool):
        self.pool = pool
        self._result = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, sql, params=None):
        self.pool.statements.append(sql.split()[0])
        if "FOR UPDATE" in sql and self.pool.lock_errors:
            raise pymysql.err.OperationalError(self.pool.lock_errors.pop(0), "Deadlock found")
        if sql.lstrip().startswith("SELECT COALESCE"):
            self._result = (0,)
        elif sql.lstrip().startswith("SELECT version"):
            self._result = (self.pool.current_version,) if self.pool.current_version else None
        else:
            self._result = None

    async def fetchone(self):
        return self._result


class _ScriptedConn:
    def __init__(self, pool):
        self.pool = pool

    async def begin(self):
        self.pool.transactions += 1

    async def commit(self):
        self.pool.commits += 1

    async def rollback(self):
        self.pool.rollbacks += 1

    def cursor(self):
        return _ScriptedCursor(self.pool)


class _ScriptedPool:
    """Raises the given MySQL errnos on successive FOR UPDATE selects."""

    def __init__(self, lock_errors, current_version=0):
        self.lock_errors = list(lock_errors)
        self.current_version = current_version
        self.statements = []
        self.transactions = self.commits = self.rollbacks = 0

    @asynccontextmanager
    async def acquire(self):
        yield _ScriptedConn(self)


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(curation_store, "_LOCK_RETRY_BACKOFF_SECONDS", 0)


async def test_save_retries_deadlock_then_succeeds(no_backoff):
    pool = _ScriptedPool(lock_errors=[1213, 1205])
    store = RunStore(pool)
    saved = await _save(store, 0, {"citations": ["111"]})
    assert saved.version == 1
    assert pool.transactions == 3 and pool.rollbacks == 2 and pool.commits == 1


async def test_save_exhausting_lock_retries_unchanged_version_is_contention(no_backoff):
    pool = _ScriptedPool(lock_errors=[1213, 1213, 1213], current_version=0)
    with pytest.raises(CurationStorageContention):
        await _save(RunStore(pool), 0, {"citations": ["111"]})
    assert pool.transactions == 3 and pool.commits == 0


async def test_save_exhausting_lock_retries_changed_version_is_conflict(no_backoff):
    pool = _ScriptedPool(lock_errors=[1213, 1213, 1213], current_version=3)
    with pytest.raises(CurationVersionConflict) as conflict:
        await _save(RunStore(pool), 0, {"citations": ["111"]})
    assert conflict.value.current_version == 3  # rechecked after the retries


async def test_delete_exhausting_lock_retries_without_version_is_contention(no_backoff):
    pool = _ScriptedPool(lock_errors=[1205, 1205, 1205], current_version=2)
    with pytest.raises(CurationStorageContention):
        await RunStore(pool).delete_curated_override(**SUBJECT, curator_email="c@mskcc.org")


async def test_non_lock_operational_errors_are_not_retried(no_backoff):
    pool = _ScriptedPool(lock_errors=[2006])  # server has gone away
    with pytest.raises(pymysql.err.OperationalError):
        await _save(RunStore(pool), 0, {"citations": ["111"]})
    assert pool.transactions == 1


async def test_delete_with_expected_version_after_revert_is_conflict(no_backoff):
    pool = _ScriptedPool(lock_errors=[], current_version=0)
    store = RunStore(pool)
    with pytest.raises(CurationVersionConflict) as conflict:
        await store.delete_curated_override(**SUBJECT, curator_email="c@mskcc.org", expected_version=2)
    assert conflict.value.current_version == 0
    assert await store.delete_curated_override(**SUBJECT, curator_email="c@mskcc.org") is None


# ---------------------------------------------------------------------------
# Real MySQL
# ---------------------------------------------------------------------------


@pytest.fixture
async def mysql_store():
    try:
        store = await RunStore.create()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM curated_overrides WHERE subject_key LIKE 'CURTEST%%'")
            await cursor.execute("DELETE FROM curation_history WHERE subject_key LIKE 'CURTEST%%'")
    yield store
    await store.close()


async def _save(store, expected_version, payload, **extra):
    return await store.save_curated_override(
        **SUBJECT,
        payload=payload,
        curator_email="curator@mskcc.org",
        expected_version=expected_version,
        pipeline_pmids_snapshot=extra.pop("snapshot", ["111"]),
        **extra,
    )


async def test_save_get_conflict_revert_history_roundtrip(mysql_store):
    run_store = mysql_store
    assert await run_store.get_curated_override("gene", "CURTEST1", "luad") is None

    first = await _save(run_store, 0, {"citations": ["111"]}, note="first")
    assert first.version == 1

    fetched = await run_store.get_curated_override("gene", "CURTEST1", "luad")
    assert fetched.payload == {"citations": ["111"]}
    assert fetched.pipeline_pmids_snapshot == ["111"]
    assert fetched.curator_email == "curator@mskcc.org"

    with pytest.raises(CurationVersionConflict) as conflict:
        await _save(run_store, 0, {"citations": ["222"]})
    assert conflict.value.current_version == 1

    second = await _save(run_store, 1, {"citations": ["222"]}, snapshot=None)
    assert second.version == 2
    assert second.created_at == first.created_at
    batch = await run_store.get_curated_overrides("gene", [("CURTEST1", "luad"), ("CURTEST2", "")])
    assert list(batch) == [("gene", "CURTEST1", "luad")]
    assert batch[("gene", "CURTEST1", "luad")].pipeline_pmids_snapshot is None

    with pytest.raises(CurationVersionConflict):
        await run_store.delete_curated_override(**SUBJECT, curator_email="c@mskcc.org", expected_version=1)
    assert await run_store.delete_curated_override(**SUBJECT, curator_email="c@mskcc.org", note="undo") == 2
    assert await run_store.get_curated_override("gene", "CURTEST1", "luad") is None
    assert await run_store.delete_curated_override(**SUBJECT, curator_email="c@mskcc.org") is None
    with pytest.raises(CurationVersionConflict):
        await run_store.delete_curated_override(**SUBJECT, curator_email="c@mskcc.org", expected_version=2)

    third = await _save(run_store, 0, {"citations": ["333"]})
    assert third.version == 4  # continues after the revert's history row

    history = await run_store.list_curation_history("gene", "CURTEST1", "luad")
    assert [(e.version, e.action) for e in history] == [(4, "save"), (3, "revert"), (2, "save"), (1, "save")]
    assert history[1].payload is None and history[1].note == "undo"
    assert history[3].payload == {"citations": ["111"]} and history[3].note == "first"


async def test_concurrent_first_saves_yield_one_winner_and_conflicts(mysql_store):
    results = await asyncio.gather(
        *(_save(mysql_store, 0, {"citations": [str(100 + i)]}) for i in range(5)),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, Exception)]
    assert len(winners) == 1 and winners[0].version == 1
    assert all(isinstance(r, CurationVersionConflict) for r in results if r not in winners)
    history = await mysql_store.list_curation_history("gene", "CURTEST1", "luad")
    assert [e.version for e in history] == [1]


async def test_run_updates_are_key_scoped_and_compare_and_swap(mysql_store):
    run_id = "curtest-run-0001"
    async with mysql_store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))
    await mysql_store.save_run(
        run_id, "2026-10-12T12:00:00+00:00", {"fusions": []},
        {"run_id": run_id, "annotations": [{"gene": "KAT6A", "citations": ["111"]}], "fusion_evidence": []},
    )
    stored, revision = await mysql_store.get_run_with_revision(run_id)
    assert revision == 0 and "annotation_tumor_types" not in stored

    # Key-scoped atomic updates (also creating the map when absent).
    await mysql_store.set_run_annotation_tumor_types(run_id, {"KAT6A": "AML", 'odd"gene\\x': None})
    await mysql_store.set_run_annotation_tumor_types(run_id, {"BRAF": "melanoma"})
    await mysql_store.set_run_fusion_evidence(run_id, [{"fusion": "KAT6A::CREBBP", "pmids": ["222"]}])
    current, current_revision = await mysql_store.get_run_with_revision(run_id)
    assert current_revision == 3
    assert current["annotation_tumor_types"] == {"KAT6A": "AML", 'odd"gene\\x': None, "BRAF": "melanoma"}
    assert current["fusion_evidence"] == [{"fusion": "KAT6A::CREBBP", "pmids": ["222"]}]
    assert current["annotations"] == [{"gene": "KAT6A", "citations": ["111"]}]

    # A whole-result write from the stale read is refused; from the fresh one it lands.
    stale = {**stored, "annotations": [{"gene": "KAT6A", "citations": []}]}
    assert await mysql_store.update_run_result_if_unchanged(run_id, stale, revision) is False
    fresh = {**current, "annotations": [{"gene": "KAT6A", "citations": []}]}
    assert await mysql_store.update_run_result_if_unchanged(run_id, fresh, current_revision) is True
    final, final_revision = await mysql_store.get_run_with_revision(run_id)
    assert final == fresh and final_revision == 4
    await mysql_store.update_run_result(run_id, final)  # plain writes bump the revision too
    assert (await mysql_store.get_run_with_revision(run_id))[1] == 5
