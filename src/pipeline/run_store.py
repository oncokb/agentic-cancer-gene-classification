"""MySQL-backed persistence for annotation run results, keyed by run_id.

Lets a peer fetch a previously-computed run (GET /v1/annotate/{run_id})
without recomputing it, so a curator can share results by link. Uses a
connection pool created once at app startup and reused across requests.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

import aiomysql

from src.config import settings

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NOT NULL
)
"""


class RunStore:
    """Save/fetch annotation runs. Calling code never issues raw SQL."""

    def __init__(self, pool: aiomysql.Pool):
        self._pool = pool

    @classmethod
    async def create(cls) -> "RunStore":
        pool = await aiomysql.create_pool(
            host=settings.mysql_host,
            port=settings.mysql_port,
            user=settings.mysql_user,
            password=settings.mysql_password,
            db=settings.mysql_database,
            autocommit=True,
        )
        store = cls(pool)
        await store._ensure_schema()
        return store

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(_CREATE_TABLE_SQL)

    async def save_run(
        self,
        run_id: str,
        created_at: str,
        request_payload: Dict[str, Any],
        result_payload: Dict[str, Any],
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO runs (run_id, created_at, request_json, result_json) "
                    "VALUES (%s, %s, %s, %s)",
                    (
                        run_id,
                        datetime.fromisoformat(created_at),
                        json.dumps(request_payload),
                        json.dumps(result_payload),
                    ),
                )

    async def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT result_json FROM runs WHERE run_id = %s", (run_id,)
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return json.loads(row[0])

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()
