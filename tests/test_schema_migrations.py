"""Startup schema migrations stay correct when several instances start at once."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

import pymysql
import pytest

from src.pipeline import schema_migrations


class FakeCursor:
    """Records SQL; `columns` is the live schema and `errors` simulates ALTER races."""

    def __init__(self, columns: Dict[str, set], errors: Optional[List[Any]] = None) -> None:
        self.columns = columns
        self.errors = list(errors or [])  # popped per ALTER: an errno, or None for success
        self.sql: List[str] = []
        self.params: List[Any] = []
        self._result: Any = None

    async def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)
        if sql.startswith("SHOW COLUMNS"):
            table = sql.split()[3]
            self._result = next(
                ((name,) for name in self.columns.get(table, set()) if _like_matches(name, params[0])),
                None,
            )
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

    async def fetchone(self):
        return self._result


def _like_matches(value: str, pattern: str) -> bool:
    regex = ""
    index = 0
    while index < len(pattern):
        character = pattern[index]
        if character == "\\" and index + 1 < len(pattern):
            index += 1
            regex += re.escape(pattern[index])
        elif character == "_":
            regex += "."
        elif character == "%":
            regex += ".*"
        else:
            regex += re.escape(character)
        index += 1
    return re.fullmatch(regex, value) is not None


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


@pytest.mark.parametrize(
    ("table", "column"),
    [("runs; DROP TABLE users", "revision"), ("runs", "owner-email"), ("1runs", "revision")],
)
async def test_invalid_identifier_is_rejected_before_sql(table, column):
    cursor = FakeCursor({"runs": set()})
    with pytest.raises(ValueError, match="Invalid SQL identifier"):
        await schema_migrations.add_column_if_missing(cursor, table, column, "INT")
    assert cursor.sql == []


async def test_show_columns_escapes_underscore_in_column_name():
    cursor = FakeCursor({"runs": {"ownerXemail"}})
    await schema_migrations.add_column_if_missing(cursor, "runs", "owner_email", "VARCHAR(255)")
    assert cursor.params[0] == (r"owner\_email",)
    assert "owner_email" in cursor.columns["runs"]


@pytest.mark.parametrize("duplicate_errno", [1060, 1061, 1068])
async def test_concurrent_safe_ddl_tolerates_duplicate_errors(duplicate_errno):
    sql = "ALTER TABLE runs ADD COLUMN revision INT"
    cursor = FakeCursor({"runs": set()}, errors=[duplicate_errno])
    await schema_migrations.run_concurrent_safe_ddl(cursor, sql)
    assert cursor.sql == [sql]


async def test_concurrent_safe_ddl_propagates_other_errors():
    cursor = FakeCursor({"runs": set()}, errors=[1142])
    with pytest.raises(pymysql.err.OperationalError):
        await schema_migrations.run_concurrent_safe_ddl(cursor, "ALTER TABLE runs ADD COLUMN revision INT")
