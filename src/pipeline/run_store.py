"""MySQL-backed persistence for run sharing and reusable gene annotations.

Lets a peer fetch a previously-computed run (GET /v1/annotate/{run_id})
without recomputing it, so a curator can share results by link. Also stores the
latest per-gene annotation for freshness-aware reuse in future runs. Uses a
connection pool created once at app startup and reused across requests.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import aiomysql

from src.config import settings
from src.models.schema import GeneAnnotation

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NOT NULL
)
"""

_CREATE_GENE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gene_annotations (
    gene VARCHAR(64) PRIMARY KEY,
    updated_at DATETIME NOT NULL,
    last_pubmed_checked_at DATETIME NULL,
    in_oncokb BOOLEAN NULL,
    evidence_support_score DOUBLE NOT NULL DEFAULT 0,
    insufficient_evidence BOOLEAN NOT NULL DEFAULT FALSE,
    annotation_json JSON NOT NULL,
    INDEX idx_gene_annotations_updated_at (updated_at),
    INDEX idx_gene_annotations_last_pubmed_checked_at (last_pubmed_checked_at)
)
"""


def _to_utc_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _from_mysql_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc)
    return value.replace(tzinfo=timezone.utc)


def _parse_jdbc_mysql_host_port(url: str) -> tuple[str, int]:
    """Parse host and port from a jdbc:mysql:// URL, e.g.
    "jdbc:mysql://host:3306" or "jdbc:mysql://host:3306/dbname?useSSL=false".
    """
    prefix = "jdbc:mysql://"
    if not url.startswith(prefix):
        raise ValueError(f"Expected a jdbc:mysql:// URL, got {url!r}")
    host_port = url[len(prefix) :].split("/", 1)[0].split("?", 1)[0]
    host, sep, port = host_port.rpartition(":")
    if not sep:
        raise ValueError(f"jdbc:mysql URL missing port: {url!r}")
    return host, int(port)


def _mysql_connection_kwargs() -> Dict[str, Any]:
    if settings.db_url:
        host, port = _parse_jdbc_mysql_host_port(settings.db_url)
    else:
        host, port = settings.mysql_host, settings.mysql_port
    return {
        "host": host,
        "port": port,
        "user": settings.db_username or settings.mysql_user,
        "password": settings.db_password or settings.mysql_password,
        "db": settings.mysql_database,
    }


class RunStore:
    """Save/fetch runs and reusable gene annotations. Calling code never issues raw SQL."""

    def __init__(self, pool: aiomysql.Pool):
        self._pool = pool

    @classmethod
    async def create(cls) -> "RunStore":
        pool = await aiomysql.create_pool(
            **_mysql_connection_kwargs(),
            autocommit=True,
        )
        store = cls(pool)
        await store._ensure_schema()
        return store

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(_CREATE_TABLE_SQL)
                await cursor.execute(_CREATE_GENE_TABLE_SQL)

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
                        _to_utc_datetime(created_at),
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

    async def save_gene_annotation(
        self,
        annotation: GeneAnnotation,
        updated_at: str | datetime,
    ) -> None:
        """Upsert the latest reusable annotation for a canonical gene."""
        payload = annotation.model_dump()
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO gene_annotations (
                        gene,
                        updated_at,
                        last_pubmed_checked_at,
                        in_oncokb,
                        evidence_support_score,
                        insufficient_evidence,
                        annotation_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        updated_at = VALUES(updated_at),
                        last_pubmed_checked_at = VALUES(last_pubmed_checked_at),
                        in_oncokb = VALUES(in_oncokb),
                        evidence_support_score = VALUES(evidence_support_score),
                        insufficient_evidence = VALUES(insufficient_evidence),
                        annotation_json = VALUES(annotation_json)
                    """,
                    (
                        annotation.gene,
                        _to_utc_datetime(updated_at),
                        _to_utc_datetime(annotation.last_pubmed_checked_at)
                        if annotation.last_pubmed_checked_at
                        else None,
                        annotation.in_oncokb,
                        annotation.evidence_support_score,
                        annotation.insufficient_evidence,
                        json.dumps(payload),
                    ),
                )

    async def get_gene_annotation(self, gene: str) -> Optional[Dict[str, Any]]:
        """Return cached annotation payload and freshness metadata for a gene."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT annotation_json, updated_at, last_pubmed_checked_at
                    FROM gene_annotations
                    WHERE gene = %s
                    """,
                    (gene,),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "annotation": json.loads(row[0]),
            "updated_at": _from_mysql_datetime(row[1]),
            "last_pubmed_checked_at": _from_mysql_datetime(row[2]),
        }

    async def mark_gene_pubmed_checked(
        self,
        gene: str,
        checked_at: str | datetime,
        annotation: GeneAnnotation | None = None,
    ) -> None:
        """Record that PubMed freshness was checked without needing regeneration."""
        checked = _to_utc_datetime(checked_at)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                if annotation is None:
                    await cursor.execute(
                        "UPDATE gene_annotations SET last_pubmed_checked_at = %s WHERE gene = %s",
                        (checked, gene),
                    )
                else:
                    payload = annotation.model_dump()
                    await cursor.execute(
                        """
                        UPDATE gene_annotations
                        SET last_pubmed_checked_at = %s, annotation_json = %s
                        WHERE gene = %s
                        """,
                        (checked, json.dumps(payload), gene),
                    )

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()
