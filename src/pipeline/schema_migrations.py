"""Idempotent startup DDL that tolerates concurrent instances.

Two instances starting at once can both see a column missing and both run
ALTER TABLE ... ADD COLUMN; the loser fails with ER_DUP_FIELDNAME (1060) or,
for an index, ER_DUP_KEYNAME (1061). Those errors mean another instance won
the race, so they are swallowed, and the end state is verified instead.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pymysql

logger = logging.getLogger(__name__)

ER_DUP_FIELDNAME = 1060
ER_DUP_KEYNAME = 1061
ER_MULTIPLE_PRI_KEY = 1068
# ALGORITHM=INSTANT unsupported: for this operation (1845/1846), or at all
# (1064, a syntax error on servers that predate it).
_INSTANT_UNSUPPORTED = frozenset({1845, 1846, 1064})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def _errno(exc: pymysql.err.MySQLError) -> int | None:
    return exc.args[0] if exc.args and isinstance(exc.args[0], int) else None


def _validate_identifier(value: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"Invalid SQL identifier: {value!r}")


async def column_exists(cursor: Any, table: str, column: str) -> bool:
    _validate_identifier(table)
    _validate_identifier(column)
    # An exact match, not SHOW COLUMNS ... LIKE: a LIKE pattern needs escaping
    # for `_`, and backslash escapes mean something else under NO_BACKSLASH_ESCAPES.
    await cursor.execute(f"SHOW COLUMNS FROM {table} WHERE Field = %s", (column,))
    return await cursor.fetchone() is not None


async def add_column_if_missing(cursor: Any, table: str, column: str, definition: str) -> None:
    """ADD COLUMN unless present, preferring ALGORITHM=INSTANT (no table copy)
    and falling back to the server default where INSTANT is unsupported.
    `definition` must be a trusted code constant, never user input.
    """
    if await column_exists(cursor, table, column):
        return
    alter = f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
    try:
        try:
            await cursor.execute(f"{alter}, ALGORITHM=INSTANT")
        except pymysql.err.MySQLError as exc:
            if _errno(exc) not in _INSTANT_UNSUPPORTED:
                raise
            await cursor.execute(alter)
    except pymysql.err.MySQLError as exc:
        if _errno(exc) != ER_DUP_FIELDNAME:
            raise
        logger.info("%s.%s was added concurrently by another instance", table, column)
    if not await column_exists(cursor, table, column):
        raise RuntimeError(f"Schema migration failed: {table}.{column} is still missing")


async def run_concurrent_safe_ddl(cursor: Any, sql: str) -> None:
    """Run DDL whose duplicate-column/key/primary-key errors mean another
    instance already applied it; callers verify the end state. For a multi-column
    ALTER that loses with 1060, the statement is all-or-nothing: verify every
    column or issue one ADD COLUMN per statement.
    """
    try:
        await cursor.execute(sql)
    except pymysql.err.MySQLError as exc:
        if _errno(exc) not in (ER_DUP_FIELDNAME, ER_DUP_KEYNAME, ER_MULTIPLE_PRI_KEY):
            raise
        logger.info("Concurrent schema migration already applied: %s", sql)
