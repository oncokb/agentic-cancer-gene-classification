"""MySQL persistence for curator overrides, mixed into RunStore.

curated_overrides holds one current row per subject (subject_type,
subject_key, tumor_type); curation_history is append-only, one row per save
or revert. Overrides never touch gene_annotations, which stays pipeline-pure.
Kept separate from run_store.py so curation tables evolve independently.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Tuple, TypeVar

import pymysql

from src.models.schema import CuratedOverride, CurationHistoryEntry

logger = logging.getLogger(__name__)

T = TypeVar("T")

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

# ER_LOCK_DEADLOCK / ER_LOCK_WAIT_TIMEOUT: concurrent first saves of the same
# subject both take gap locks under SELECT ... FOR UPDATE and one deadlocks.
_LOCK_RETRY_ERRNOS = frozenset({1213, 1205})
_LOCK_RETRY_ATTEMPTS = 3
_LOCK_RETRY_BACKOFF_SECONDS = 0.05


class CurationVersionConflict(Exception):
    """The override changed since the caller read it (optimistic concurrency)."""

    def __init__(self, current_version: int):
        super().__init__(f"Curated override is at version {current_version}")
        self.current_version = current_version


class CurationStorageContention(Exception):
    """Lock errors persisted through every retry while the override was still
    at the caller's version: nothing conflicts, the write should just be retried."""


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


def _json_member_path(*keys: str) -> str:
    """A MySQL JSON path ($."a"."b") with each key quoted and escaped."""
    quoted = ('"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"' for key in keys)
    return "$." + ".".join(quoted)


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
        # runs.revision: bumped by every result_json write, so whole-result
        # rewrites can compare-and-swap instead of clobbering concurrent ones.
        await cursor.execute("SHOW COLUMNS FROM runs LIKE 'revision'")
        if await cursor.fetchone() is None:
            await cursor.execute("ALTER TABLE runs ADD COLUMN revision INT NOT NULL DEFAULT 0")

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

    async def _current_override_version(self, subject_type: str, subject_key: str, tumor_type: str) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT version FROM curated_overrides "
                    "WHERE subject_type = %s AND subject_key = %s AND tumor_type = %s",
                    (subject_type, subject_key, tumor_type),
                )
                row = await cursor.fetchone()
        return row[0] if row else 0

    async def _with_lock_retry(
        self, attempt: Callable[[], Awaitable[T]], subject: SubjectId, expected_version: Optional[int]
    ) -> T:
        """Run a write transaction, retrying MySQL deadlocks/lock-wait timeouts.

        When retries are exhausted, or a concurrent first save won the insert
        race (IntegrityError), re-read the current version: if it differs from
        expected_version the caller's view is stale (CurationVersionConflict,
        HTTP 409); otherwise it is plain contention (CurationStorageContention,
        a retryable 503). Never a 500."""
        for attempt_number in range(1, _LOCK_RETRY_ATTEMPTS + 1):
            try:
                return await attempt()
            except pymysql.err.IntegrityError:
                break
            except pymysql.err.OperationalError as exc:
                if not exc.args or exc.args[0] not in _LOCK_RETRY_ERRNOS:
                    raise
                logger.warning(
                    "Curation write for %s hit MySQL lock error %s (attempt %d/%d)",
                    subject, exc.args[0], attempt_number, _LOCK_RETRY_ATTEMPTS,
                )
                if attempt_number < _LOCK_RETRY_ATTEMPTS:
                    await asyncio.sleep(_LOCK_RETRY_BACKOFF_SECONDS * attempt_number)
        current_version = await self._current_override_version(*subject)
        if expected_version is not None and current_version != expected_version:
            raise CurationVersionConflict(current_version)
        raise CurationStorageContention(f"Curation write for {subject} kept hitting lock errors")

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
        return await self._with_lock_retry(
            lambda: self._save_curated_override_once(
                subject_type=subject_type,
                subject_key=subject_key,
                tumor_type=tumor_type,
                payload=payload,
                curator_email=curator_email,
                expected_version=expected_version,
                pipeline_pmids_snapshot=pipeline_pmids_snapshot,
                note=note,
            ),
            (subject_type, subject_key, tumor_type),
            expected_version,
        )

    async def _save_curated_override_once(
        self,
        *,
        subject_type: str,
        subject_key: str,
        tumor_type: str,
        payload: Dict[str, Any],
        curator_email: str,
        expected_version: int,
        pipeline_pmids_snapshot: Optional[List[str]],
        note: Optional[str],
    ) -> CuratedOverride:
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
        Raises CurationVersionConflict when expected_version is given and stale,
        including when the override it names was already reverted."""
        return await self._with_lock_retry(
            lambda: self._delete_curated_override_once(
                subject_type=subject_type,
                subject_key=subject_key,
                tumor_type=tumor_type,
                curator_email=curator_email,
                expected_version=expected_version,
                note=note,
            ),
            (subject_type, subject_key, tumor_type),
            expected_version,
        )

    async def _delete_curated_override_once(
        self,
        *,
        subject_type: str,
        subject_key: str,
        tumor_type: str,
        curator_email: str,
        expected_version: Optional[int],
        note: Optional[str],
    ) -> Optional[int]:
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
                        if expected_version is not None:
                            raise CurationVersionConflict(0)
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

    # -- runs: concurrency-safe result_json updates -------------------------

    async def get_run_with_revision(self, run_id: str) -> Optional[Tuple[Dict[str, Any], int]]:
        """A saved run's result plus its revision, for update_run_result_if_unchanged."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT result_json, revision FROM runs WHERE run_id = %s", (run_id,))
                row = await cursor.fetchone()
        return None if row is None else (_loads(row[0]), row[1])

    async def update_run_result_if_unchanged(
        self, run_id: str, result_payload: Dict[str, Any], expected_revision: int
    ) -> bool:
        """Replace result_json only if nobody wrote the run since it was read at
        expected_revision (compare-and-swap). False means reload and retry."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE runs SET result_json = %s, revision = revision + 1 "
                    "WHERE run_id = %s AND revision = %s",
                    (json.dumps(result_payload), run_id, expected_revision),
                )
                return cursor.rowcount == 1

    async def set_run_annotation_tumor_types(self, run_id: str, entries: Dict[str, Optional[str]]) -> None:
        """Atomically merge confirmed gene -> tumor type entries into the run's
        annotation_tumor_types, touching no other part of result_json."""
        if not entries:
            return
        assignments = ", ".join(["%s, %s"] * len(entries))
        params: List[Any] = []
        for gene, tumor_type in entries.items():
            params.extend([_json_member_path("annotation_tumor_types", gene), tumor_type])
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    UPDATE runs SET
                        result_json = JSON_SET(
                            JSON_SET(
                                result_json, '$.annotation_tumor_types',
                                COALESCE(JSON_EXTRACT(result_json, '$.annotation_tumor_types'), JSON_OBJECT())
                            ),
                            {assignments}
                        ),
                        revision = revision + 1
                    WHERE run_id = %s
                    """,
                    (*params, run_id),
                )

    async def set_run_fusion_evidence(self, run_id: str, fusion_evidence: List[Dict[str, Any]]) -> None:
        """Atomically replace only the run's fusion_evidence."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE runs SET result_json = JSON_SET(result_json, '$.fusion_evidence', CAST(%s AS JSON)), "
                    "revision = revision + 1 WHERE run_id = %s",
                    (json.dumps(fusion_evidence), run_id),
                )

    async def get_run_request(self, run_id: str) -> Optional[Dict[str, Any]]:
        """The request a saved run was made with (used to recover per-gene
        tumor types for runs saved before they were stored on the result)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT request_json FROM runs WHERE run_id = %s", (run_id,))
                row = await cursor.fetchone()
        return None if row is None else _loads(row[0])
