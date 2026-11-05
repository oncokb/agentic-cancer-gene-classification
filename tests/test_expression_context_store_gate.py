"""Module store (in-memory + real MySQL when available) and the ACGC pipeline gate."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.expression_context.literature_gate import (
    AcgcPipelineGate,
    LiteratureGate,
    LiteratureGateError,
    is_supported,
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
from src.models.schema import AnnotationResult, GeneAnnotation
from tests.expression_context_helpers import FakeLiteratureGate, gene_file

GATE = "acgc_pipeline_v1"


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


# ------------------------------------------------------------- literature gate


def test_supported_rule():
    assert is_supported(False, ["1"])
    assert not is_supported(True, ["1"])
    assert not is_supported(False, [])


def test_gates_satisfy_protocol():
    assert isinstance(AcgcPipelineGate(lambda: None), LiteratureGate)
    assert isinstance(FakeLiteratureGate(), LiteratureGate)
    assert AcgcPipelineGate.name == GATE


def _result(*annotations):
    return AnnotationResult(
        run_id="r", timestamp="t", fusions_processed=1, genes_annotated=len(annotations),
        annotations=list(annotations),
    )


async def test_pipeline_gate_uses_pipeline_and_cache_with_tumor_type(monkeypatch):
    from src.pipeline import orchestrator

    run_store = object()
    seen = {}

    async def fake_run_pipeline(fusions, **kwargs):
        seen["fusions"] = fusions
        seen.update(kwargs)
        return _result(GeneAnnotation(
            gene="ERBB2", citations=["111", " ", "222"], insufficient_evidence=False,
            cancer_association_rationale="amplified in breast cancer",
            evidence_support_score=0.9,
        ))

    monkeypatch.setattr(orchestrator, "run_pipeline", fake_run_pipeline)
    verdict = await AcgcPipelineGate(lambda: run_store).evaluate("ERBB2", "breast cancer")

    assert seen["fusions"][0].fusion == "ERBB2"
    assert seen["fusions"][0].tumor_type == "breast cancer"
    # The app's run store is passed, so the gene_annotations cache is used.
    assert seen["run_store"] is run_store
    assert seen["mode"] == "full"
    assert verdict.supported and verdict.pmids == ["111", "222"]
    assert verdict.rationale == "amplified in breast cancer"
    assert verdict.tumor_type_query == "breast cancer" and verdict.literature_gate == GATE


@pytest.mark.parametrize(
    "insufficient,citations,supported",
    [(True, ["1"], False), (False, [], False), (False, ["1"], True)],
)
async def test_pipeline_gate_verdict_rule(monkeypatch, insufficient, citations, supported):
    from src.pipeline import orchestrator

    async def fake_run_pipeline(fusions, **kwargs):
        return _result(GeneAnnotation(
            gene="ERBB2", citations=citations, insufficient_evidence=insufficient,
        ))

    monkeypatch.setattr(orchestrator, "run_pipeline", fake_run_pipeline)
    verdict = await AcgcPipelineGate(lambda: None).evaluate("ERBB2", "breast cancer")
    assert verdict.supported is supported
    assert verdict.insufficient_evidence is insufficient


async def test_pipeline_gate_errors(monkeypatch):
    from src.pipeline import orchestrator

    async def boom(fusions, **kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(orchestrator, "run_pipeline", boom)
    with pytest.raises(LiteratureGateError):
        await AcgcPipelineGate(lambda: None).evaluate("ERBB2", "breast cancer")

    async def empty(fusions, **kwargs):
        return _result()

    monkeypatch.setattr(orchestrator, "run_pipeline", empty)
    with pytest.raises(LiteratureGateError):
        await AcgcPipelineGate(lambda: None).evaluate("ERBB2", "breast cancer")


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
