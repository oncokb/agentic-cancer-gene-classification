"""Integration tests for the MySQL curated_overrides / curation_history tables.

Run against a real MySQL like tests/test_run_store.py; skipped when none is reachable.
"""

from __future__ import annotations

import pytest

from src.pipeline.curation_store import CurationVersionConflict
from src.pipeline.run_store import RunStore

SUBJECT = {"subject_type": "gene", "subject_key": "CURTEST1", "tumor_type": "luad"}


@pytest.fixture
async def run_store():
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


async def test_save_get_conflict_revert_history_roundtrip(run_store):
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

    third = await _save(run_store, 0, {"citations": ["333"]})
    assert third.version == 4  # continues after the revert's history row

    history = await run_store.list_curation_history("gene", "CURTEST1", "luad")
    assert [(e.version, e.action) for e in history] == [(4, "save"), (3, "revert"), (2, "save"), (1, "save")]
    assert history[1].payload is None and history[1].note == "undo"
    assert history[3].payload == {"citations": ["111"]} and history[3].note == "first"
