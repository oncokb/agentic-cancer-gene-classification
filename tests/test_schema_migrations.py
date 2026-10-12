"""Startup schema migrations stay correct when several instances start at once."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pymysql
import pytest

from src.pipeline import curation_store, schema_migrations
from src.pipeline.run_store import RunStore


class FakeCursor:
    """Records SQL; `columns` is the live schema, `on_alter` simulates what
    happens when this instance's ALTER runs (e.g. another instance won)."""

    def __init__(self, columns: Dict[str, set], errors: Optional[List[Any]] = None,
                 primary: Optional[List[str]] = None) -> None:
        self.columns = columns
        self.errors = list(errors or [])  # popped per ALTER: an errno, or None for success
        self.primary = primary or ["gene", "tumor_type"]
        self.sql: List[str] = []
        self._result: Any = None

    async def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql.startswith("SHOW COLUMNS"):
            table = sql.split()[3]
            self._result = (params[0],) if params[0] in self.columns.get(table, set()) else None
        elif sql.startswith("SHOW INDEX"):
            self._result = [(None, None, None, i + 1, col) for i, col in enumerate(self.primary)]
        elif sql.startswith("ALTER TABLE"):
            outcome = self.errors.pop(0) if self.errors else None
            table = sql.split()[2]
            if isinstance(outcome, tuple):  # (errno, side effect)
                outcome[1](self)
                outcome = outcome[0]
            if outcome is not None:
                raise pymysql.err.OperationalError(outcome, "simulated")
            if "ADD COLUMN" in sql:
                self.columns.setdefault(table, set()).add(sql.split("ADD COLUMN ")[1].split()[0])
            if "ADD PRIMARY KEY" in sql:
                self.primary = ["gene", "tumor_type"]

    async def fetchone(self):
        return self._result

    async def fetchall(self):
        return self._result


def _won_race(table, column):
    return lambda cursor: cursor.columns.setdefault(table, set()).add(column)


async def test_add_column_uses_instant_when_supported():
    cursor = FakeCursor({"runs": set()})
    await schema_migrations.add_column_if_missing(cursor, "runs", "revision", "INT NOT NULL DEFAULT 0")
    assert cursor.sql[1] == "ALTER TABLE runs ADD COLUMN revision INT NOT NULL DEFAULT 0, ALGORITHM=INSTANT"
    assert "revision" in cursor.columns["runs"]


@pytest.mark.parametrize("instant_errno", [1845, 1846, 1064])
async def test_add_column_falls_back_when_instant_unsupported(instant_errno):
    cursor = FakeCursor({"runs": set()}, errors=[instant_errno])
    await schema_migrations.add_column_if_missing(cursor, "runs", "revision", "INT NOT NULL DEFAULT 0")
    assert cursor.sql[2] == "ALTER TABLE runs ADD COLUMN revision INT NOT NULL DEFAULT 0"
    assert "revision" in cursor.columns["runs"]


@pytest.mark.parametrize("errors", [
    [(1060, _won_race("runs", "revision"))],  # lost the race on the INSTANT attempt
    [1846, (1060, _won_race("runs", "revision"))],  # lost it on the fallback
])
async def test_add_column_tolerates_concurrent_instance(errors):
    cursor = FakeCursor({"runs": set()}, errors=errors)
    await schema_migrations.add_column_if_missing(cursor, "runs", "revision", "INT NOT NULL DEFAULT 0")
    assert "revision" in cursor.columns["runs"]


async def test_add_column_duplicate_error_without_column_still_fails():
    cursor = FakeCursor({"runs": set()}, errors=[1060])
    with pytest.raises(RuntimeError, match="runs.revision is still missing"):
        await schema_migrations.add_column_if_missing(cursor, "runs", "revision", "INT NOT NULL DEFAULT 0")


async def test_add_column_other_errors_propagate():
    cursor = FakeCursor({"runs": set()}, errors=[1142])  # access denied
    with pytest.raises(pymysql.err.OperationalError):
        await schema_migrations.add_column_if_missing(cursor, "runs", "revision", "INT NOT NULL DEFAULT 0")


async def test_present_column_skips_alter():
    cursor = FakeCursor({"runs": {"revision"}})
    await schema_migrations.add_column_if_missing(cursor, "runs", "revision", "INT NOT NULL DEFAULT 0")
    assert not any(sql.startswith("ALTER") for sql in cursor.sql)


async def test_curation_schema_survives_revision_race():
    cursor = FakeCursor({"runs": set()}, errors=[(1060, _won_race("runs", "revision"))])
    await curation_store.CurationStoreMixin()._ensure_curation_schema(cursor)
    assert "revision" in cursor.columns["runs"]


async def test_gene_annotation_primary_key_race_is_verified():
    def other_instance_rekeyed(cursor):
        cursor.primary = ["gene", "tumor_type"]

    cursor = FakeCursor({"gene_annotations": set()}, primary=["gene"],
                        errors=[None, (1068, other_instance_rekeyed)])
    await RunStore(pool=None)._ensure_gene_annotation_schema(cursor)
    assert cursor.primary == ["gene", "tumor_type"]
    assert "tumor_type" in cursor.columns["gene_annotations"]

