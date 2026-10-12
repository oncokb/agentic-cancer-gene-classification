"""Idempotent startup DDL that tolerates concurrent instances.

Two instances starting at once can both see a column missing and both run
ALTER TABLE ... ADD COLUMN; the loser fails with ER_DUP_FIELDNAME (1060) or,
for an index, ER_DUP_KEYNAME (1061). Those errors mean another instance won
the race, so they are swallowed, and the end state is verified instead.
"""

from __future__ import annotations

import logging
from typing import Any

import pymysql

logger = logging.getLogger(__name__)

ER_DUP_FIELDNAME = 1060
ER_DUP_KEYNAME = 1061
ER_MULTIPLE_PRI_KEY = 1068
# ALGORITHM=INSTANT unsupported: for this operation (1845/1846), or at all
# (1064, a syntax error on servers that predate it).
_INSTANT_UNSUPPORTED = frozenset({1845, 1846, 1064})


def _errno(exc: pymysql.err.MySQLError) -> int | None:
    return exc.args[0] if exc.args and isinstance(exc.args[0], int) else None


async def column_exists(cursor: Any, table: str, column: str) -> bool:
    await cursor.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
    return await cursor.fetchone() is not None


async def add_column_if_missing(cursor: Any, table: str, column: str, definition: str) -> None:
    """ADD COLUMN unless present, preferring ALGORITHM=INSTANT (no table copy)
    and falling back to the server default where INSTANT is unsupported."""
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
    instance already applied it; callers verify the end state."""
    try:
        await cursor.execute(sql)
    except pymysql.err.MySQLError as exc:
        if _errno(exc) not in (ER_DUP_FIELDNAME, ER_DUP_KEYNAME, ER_MULTIPLE_PRI_KEY):
            raise
        logger.info("Concurrent schema migration already applied: %s", sql)
