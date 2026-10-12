"""MySQL persistence for curator overrides, mixed into RunStore.

curated_overrides holds one current row per subject (subject_type,
subject_key, tumor_type); curation_history is append-only, one row per save
or revert. Overrides never touch gene_annotations, which stays pipeline-pure.
Kept separate from run_store.py so curation tables evolve independently.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pymysql

from src.models.schema import CuratedOverride, CurationHistoryEntry

logger = logging.getLogger(__name__)

_CREATE_CURATED_OVERRIDES_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS curated_overrides (
    subject_type VARCHAR(16) NOT NULL,
    subject_key VARCHAR(160) NOT NULL,
    tumor_type VARCHAR(128) NOT NULL DEFAULT '',
    payload_json JSON NOT NULL,
    curator_email VARCHAR(255) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    version INT NOT NULL,
    pipeline_pmids_snapshot_json JSON NULL,
    note TEXT NULL,
    PRIMARY KEY (subject_type, subject_key, tumor_type),
    INDEX idx_curated_overrides_updated_at (updated_at)
)
"""

_CREATE_CURATION_HISTORY_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS curation_history (
    id BIGINT AUTO_INCREMENT PRIMARY KEY,
    subject_type VARCHAR(16) NOT NULL,
    subject_key VARCHAR(160) NOT NULL,
    tumor_type VARCHAR(128) NOT NULL DEFAULT '',
    version INT NOT NULL,
    action VARCHAR(16) NOT NULL,
    curator_email VARCHAR(255) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    payload_json JSON NULL,
    pipeline_pmids_snapshot_json JSON NULL,
    note TEXT NULL,
    UNIQUE INDEX idx_curation_history_subject_version (subject_type, subject_key, tumor_type, version)
)
"""

_OVERRIDE_COLUMNS = (
    "subject_type, subject_key, tumor_type, payload_json, curator_email, "
    "created_at, updated_at, version, pipeline_pmids_snapshot_json, note"
)

SubjectId = Tuple[str, str, str]  # (subject_type, subject_key, tumor_type)


class CurationVersionConflict(Exception):
    """The override changed since the caller read it (optimistic concurrency)."""

    def __init__(self, current_version: int):
        super().__init__(f"Curated override is at version {current_version}")
        self.current_version = current_version


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _json_or_none(value: Any) -> Optional[str]:
    return None if value is None else json.dumps(value)


def _loads(value: Any) -> Any:
    if value is None:
        return None
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _override_from_row(row) -> CuratedOverride:
    return CuratedOverride(
        subject_type=row[0],
        subject_key=row[1],
        tumor_type=row[2] or "",
        payload=_loads(row[3]) or {},
        curator_email=row[4],
        created_at=_iso(row[5]),
        updated_at=_iso(row[6]),
        version=row[7],
        pipeline_pmids_snapshot=_loads(row[8]),
        note=row[9],
    )


class CurationStoreMixin:
    """Curated-override methods for RunStore (expects `self._pool`)."""

    _pool: Any

    async def _ensure_curation_schema(self, cursor) -> None:
        await cursor.execute(_CREATE_CURATED_OVERRIDES_TABLE_SQL)
        await cursor.execute(_CREATE_CURATION_HISTORY_TABLE_SQL)

    async def get_curated_override(
        self, subject_type: str, subject_key: str, tumor_type: str
    ) -> Optional[CuratedOverride]:
        found = await self.get_curated_overrides(subject_type, [(subject_key, tumor_type)])
        return found.get((subject_type, subject_key, tumor_type))

    async def get_curated_overrides(
        self, subject_type: str, keys: Iterable[Tuple[str, str]]
    ) -> Dict[SubjectId, CuratedOverride]:
        """Batch lookup of current overrides by (subject_key, tumor_type)."""
        unique_keys = list(dict.fromkeys(keys))
        if not unique_keys:
            return {}
        placeholders = ", ".join(["(%s, %s)"] * len(unique_keys))
        params: List[Any] = [subject_type]
        for key, tumor_type in unique_keys:
            params.extend([key, tumor_type])
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_OVERRIDE_COLUMNS} FROM curated_overrides "
                    f"WHERE subject_type = %s AND (subject_key, tumor_type) IN ({placeholders})",
                    tuple(params),
                )
                rows = await cursor.fetchall()
        records = [_override_from_row(row) for row in rows]
        return {(r.subject_type, r.subject_key, r.tumor_type): r for r in records}

    async def save_curated_override(
        self,
        *,
        subject_type: str,
        subject_key: str,
        tumor_type: str,
        payload: Dict[str, Any],
        curator_email: str,
        expected_version: int,
        pipeline_pmids_snapshot: Optional[List[str]],
        note: Optional[str] = None,
    ) -> CuratedOverride:
        """Create or replace the current override and append a history row.

        expected_version is the version the curator edited (0 when creating);
        raises CurationVersionConflict when it no longer matches."""
        now = _utc_now()
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT version, created_at FROM curated_overrides "
                        "WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s FOR UPDATE",
                        (subject_type, subject_key, tumor_type),
                    )
                    row = await cursor.fetchone()
                    current_version = row[0] if row else 0
                    if current_version != expected_version:
                        raise CurationVersionConflict(current_version)
                    # Versions keep counting across reverts so history stays unique.
                    await cursor.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM curation_history "
                        "WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s",
                        (subject_type, subject_key, tumor_type),
                    )
                    new_version = max(current_version, (await cursor.fetchone())[0]) + 1
                    created_at = row[1] if row else now
                    payload_json = json.dumps(payload)
                    snapshot_json = _json_or_none(pipeline_pmids_snapshot)
                    if row:
                        await cursor.execute(
                            """
                            UPDATE curated_overrides
                            SET payload_json = %s, curator_email = %s, updated_at = %s, version = %s,
                                pipeline_pmids_snapshot_json = %s, note = %s
                            WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s
                            """,
                            (
                                payload_json, curator_email, now, new_version, snapshot_json, note,
                                subject_type, subject_key, tumor_type,
                            ),
                        )
                    else:
                        await cursor.execute(
                            f"INSERT INTO curated_overrides ({_OVERRIDE_COLUMNS}) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                            (
                                subject_type, subject_key, tumor_type, payload_json, curator_email,
                                created_at, now, new_version, snapshot_json, note,
                            ),
                        )
                    await cursor.execute(
                        """
                        INSERT INTO curation_history (
                            subject_type, subject_key, tumor_type, version, action, curator_email,
                            created_at, payload_json, pipeline_pmids_snapshot_json, note
                        )
                        VALUES (%s, %s, %s, %s, 'save', %s, %s, %s, %s, %s)
                        """,
                        (
                            subject_type, subject_key, tumor_type, new_version, curator_email,
                            now, payload_json, snapshot_json, note,
                        ),
                    )
                await conn.commit()
            except pymysql.err.IntegrityError as exc:
                # A concurrent first save inserted the same subject/version.
                await conn.rollback()
                raise CurationVersionConflict(expected_version + 1) from exc
            except BaseException:
                await conn.rollback()
                raise
        return CuratedOverride(
            subject_type=subject_type,
            subject_key=subject_key,
            tumor_type=tumor_type,
            payload=payload,
            curator_email=curator_email,
            created_at=_iso(created_at),
            updated_at=_iso(now),
            version=new_version,
            pipeline_pmids_snapshot=pipeline_pmids_snapshot,
            note=note,
        )

    async def delete_curated_override(
        self,
        *,
        subject_type: str,
        subject_key: str,
        tumor_type: str,
        curator_email: str,
        expected_version: Optional[int] = None,
        note: Optional[str] = None,
    ) -> Optional[int]:
        """Revert a subject to pipeline output, recording the revert in history.

        Returns the reverted-from version, or None when there was no override.
        Raises CurationVersionConflict when expected_version is given and stale."""
        now = _utc_now()
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT version FROM curated_overrides "
                        "WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s FOR UPDATE",
                        (subject_type, subject_key, tumor_type),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        await conn.rollback()
                        return None
                    current_version = row[0]
                    if expected_version is not None and expected_version != current_version:
                        raise CurationVersionConflict(current_version)
                    await cursor.execute(
                        "DELETE FROM curated_overrides "
                        "WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s",
                        (subject_type, subject_key, tumor_type),
                    )
                    await cursor.execute(
                        """
                        INSERT INTO curation_history (
                            subject_type, subject_key, tumor_type, version, action, curator_email,
                            created_at, payload_json, pipeline_pmids_snapshot_json, note
                        )
                        VALUES (%s, %s, %s, %s, 'revert', %s, %s, NULL, NULL, %s)
                        """,
                        (
                            subject_type, subject_key, tumor_type, current_version + 1,
                            curator_email, now, note,
                        ),
                    )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return current_version

    async def list_curation_history(
        self, subject_type: str, subject_key: str, tumor_type: str
    ) -> List[CurationHistoryEntry]:
        """History for one subject, newest first."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT subject_type, subject_key, tumor_type, version, action,
                           curator_email, created_at, payload_json, note
                    FROM curation_history
                    WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s
                    ORDER BY version DESC
                    """,
                    (subject_type, subject_key, tumor_type),
                )
                rows = await cursor.fetchall()
        return [
            CurationHistoryEntry(
                subject_type=row[0],
                subject_key=row[1],
                tumor_type=row[2] or "",
                version=row[3],
                action=row[4],
                curator_email=row[5],
                created_at=_iso(row[6]),
                payload=_loads(row[7]),
                note=row[8],
            )
            for row in rows
        ]

    async def get_run_request(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The request a saved run was made with (used to recover per-gene
        tumor types for runs saved before they were stored on the result)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT request_json FROM runs WHERE run_id = %s", (run_id,))
                row = await cursor.fetchone()
        return None if row is None else _loads(row[0])
