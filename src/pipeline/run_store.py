"""MySQL-backed persistence for run sharing and reusable gene annotations.

Lets a peer fetch a previously-computed run (GET /v1/annotate/{run_id})
without recomputing it, so a curator can share results by link. Also stores the
latest per-gene annotation for freshness-aware reuse in future runs. Uses a
connection pool created once at app startup and reused across requests.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiomysql
import pymysql

from src.api_keys import ApiKeyRecord
from src.config import settings
from src.models.schema import GeneAnnotation, PMIDEvidenceRecord
from src.pipeline.user_store import UserStoreMixin

logger = logging.getLogger(__name__)

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    request_json JSON NOT NULL,
    result_json JSON NOT NULL,
    owner_email VARCHAR(255) NULL,
    source VARCHAR(16) NULL,
    input_summary VARCHAR(255) CHARACTER SET utf8mb4 NULL,
    input_count INT NULL,
    genes_annotated INT NULL,
    INDEX idx_runs_owner_created_at (owner_email, created_at, run_id)
)
"""

# Columns added to `runs` after it first shipped. Runs saved before then keep
# NULL here: they have no recorded owner and appear in no one's run list.
_RUNS_ADDED_COLUMNS = (
    ("owner_email", "VARCHAR(255) NULL"),
    ("source", "VARCHAR(16) NULL"),
    ("input_summary", "VARCHAR(255) CHARACTER SET utf8mb4 NULL"),
    ("input_count", "INT NULL"),
    ("genes_annotated", "INT NULL"),
)
_RUNS_OWNER_INDEX = "idx_runs_owner_created_at"

# MySQL errors for an ALTER that another pod's concurrent startup already applied.
_ER_DUP_FIELDNAME = 1060
_ER_DUP_KEYNAME = 1061
_ER_LOCK_WAIT_TIMEOUT = 1205
# Errors after which the column ALTER retries with ALGORITHM=INPLACE: INSTANT
# is an unknown algorithm (MySQL < 8.0, error 1800) or the operation isn't
# supported with it (1845/1846). Other INSTANT limitations, e.g. too many
# row versions (4092) or a table that can't take an instant column (4158),
# are deliberately not listed: they fail startup so an operator can run the
# upgrade by hand rather than have pods silently rebuild a large table.
_ER_UNKNOWN_ALTER_ALGORITHM = 1800
_ER_ALTER_OPERATION_NOT_SUPPORTED = 1845
_ER_ALTER_OPERATION_NOT_SUPPORTED_REASON = 1846
_INSTANT_UNSUPPORTED_ERRORS = {
    _ER_UNKNOWN_ALTER_ALGORITHM,
    _ER_ALTER_OPERATION_NOT_SUPPORTED,
    _ER_ALTER_OPERATION_NOT_SUPPORTED_REASON,
}

# Range of a MySQL DATETIME, which a decoded cursor timestamp must fall in.
_MIN_CURSOR_YEAR = 1000
_MAX_CURSOR_YEAR = 9999
_MAX_CURSOR_RUN_ID_LENGTH = 36
_CURSOR_PATTERN = re.compile(r"[A-Za-z0-9_-]+")

INPUT_SUMMARY_MAX_ITEMS = 5
INPUT_SUMMARY_MAX_LENGTH = 255

_CREATE_GENE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gene_annotations (
    gene VARCHAR(64) NOT NULL,
    tumor_type VARCHAR(128) NOT NULL DEFAULT '',
    updated_at DATETIME NOT NULL,
    last_pubmed_checked_at DATETIME NULL,
    in_oncokb BOOLEAN NULL,
    evidence_support_score DOUBLE NOT NULL DEFAULT 0,
    insufficient_evidence BOOLEAN NOT NULL DEFAULT FALSE,
    annotation_json JSON NOT NULL,
    PRIMARY KEY (gene, tumor_type),
    INDEX idx_gene_annotations_gene (gene),
    INDEX idx_gene_annotations_updated_at (updated_at),
    INDEX idx_gene_annotations_last_pubmed_checked_at (last_pubmed_checked_at)
)
"""

_CREATE_PMID_EVIDENCE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS pmid_evidence (
    pmid VARCHAR(16) PRIMARY KEY,
    doi VARCHAR(128) NULL,
    title VARCHAR(512) NOT NULL,
    journal VARCHAR(128) NOT NULL,
    publication_year INT NULL,
    evidence_type ENUM('clinical', 'preclinical', 'case_report', 'review', 'other') DEFAULT 'other',
    oncogenic_role ENUM('oncogene', 'tumor_suppressor', 'resistance', 'neutral', 'unknown') DEFAULT 'unknown',
    distilled_takeaway TEXT NOT NULL,
    supporting_quote TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_journal (journal),
    INDEX idx_evidence_type (evidence_type)
)
"""

_CREATE_FEEDBACK_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id VARCHAR(36) PRIMARY KEY,
    created_at DATETIME NOT NULL,
    category VARCHAR(32) NOT NULL,
    message TEXT NOT NULL,
    contact_email VARCHAR(255) NULL,
    run_id VARCHAR(36) NULL,
    gene VARCHAR(64) NULL,
    page_url VARCHAR(512) NULL,
    user_agent VARCHAR(512) NULL,
    INDEX idx_feedback_created_at (created_at),
    INDEX idx_feedback_run_id (run_id)
)
"""


# Only the SHA-256 of the key is stored, and keys are looked up by that hash;
# nothing derived from the secret is kept in the clear. See src/api_keys.py.
_CREATE_API_KEYS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS api_keys (
    id VARCHAR(36) PRIMARY KEY,
    key_hash CHAR(64) NOT NULL,
    owner_email VARCHAR(255) NOT NULL,
    name VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL,
    last_used_at DATETIME NULL,
    revoked_at DATETIME NULL,
    expires_at DATETIME NULL,
    UNIQUE INDEX idx_api_keys_key_hash (key_hash),
    INDEX idx_api_keys_owner_email (owner_email)
)
"""

_API_KEY_COLUMNS = (
    "id, key_hash, owner_email, name, created_at, last_used_at, revoked_at, expires_at"
)


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


def _normalize_cache_tumor_type(tumor_type: Optional[str]) -> str:
    """Normalize tumor type for the reusable gene annotation cache key."""
    return " ".join((tumor_type or "").strip().lower().split())


def _run_input_strings(request_payload: Dict[str, Any]) -> List[str]:
    """The gene/fusion strings a run was requested for, in request order.

    Handles both AnnotateRequest (`fusions`: strings or FusionInput dicts) and
    GeneAnnotateRequest (`gene`) payloads.
    """
    if "fusions" in request_payload:
        inputs = []
        for item in request_payload.get("fusions") or []:
            value = item.get("fusion") if isinstance(item, dict) else item
            if value:
                inputs.append(str(value))
        return inputs
    gene = request_payload.get("gene")
    return [str(gene)] if gene else []


def summarize_run_inputs(request_payload: Dict[str, Any]) -> tuple[str, int]:
    """A short, bounded label for a run's inputs (e.g. "ALK, EML4::ALK (+3 more)")
    and the total input count, for listing runs without loading their request."""
    inputs = _run_input_strings(request_payload)
    shown = inputs[:INPUT_SUMMARY_MAX_ITEMS]
    summary = ", ".join(shown)
    remaining = len(inputs) - len(shown)
    suffix = f" (+{remaining} more)" if remaining else ""
    if len(summary) + len(suffix) > INPUT_SUMMARY_MAX_LENGTH:
        summary = summary[: INPUT_SUMMARY_MAX_LENGTH - len(suffix) - 1].rstrip() + "…"
    return summary + suffix, len(inputs)


@dataclass(frozen=True)
class RunSummary:
    """One row of a user's run list: metadata only, never the result blob."""

    run_id: str
    created_at: datetime
    source: Optional[str]
    input_summary: Optional[str]
    input_count: Optional[int]
    genes_annotated: Optional[int]


@dataclass(frozen=True)
class RunSummaryPage:
    runs: List[RunSummary]
    # Pass back as `cursor` for the next (older) page; None on the last page.
    next_cursor: Optional[str]


def _encode_runs_cursor(created_at: datetime, run_id: str) -> str:
    raw = f"{created_at.isoformat()}|{run_id}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_runs_cursor(cursor: str) -> tuple[datetime, str]:
    """Inverse of _encode_runs_cursor, returning an aware UTC timestamp.

    Raises ValueError for anything this module didn't produce."""
    try:
        if not isinstance(cursor, str) or not _CURSOR_PATTERN.fullmatch(cursor):
            raise ValueError("not url-safe base64")
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True).decode("utf-8")
        created_at_text, sep, run_id = raw.partition("|")
        if not sep or not run_id or len(run_id) > _MAX_CURSOR_RUN_ID_LENGTH or "|" in run_id:
            raise ValueError("bad cursor shape")
        created_at = datetime.fromisoformat(created_at_text)
        if created_at.tzinfo is None:
            raise ValueError("cursor timestamp has no timezone")
        created_at = created_at.astimezone(timezone.utc)
        if not _MIN_CURSOR_YEAR <= created_at.year <= _MAX_CURSOR_YEAR:
            raise ValueError("cursor timestamp out of range")
        return created_at, run_id
    except (binascii.Error, UnicodeDecodeError, ValueError, OverflowError) as exc:
        raise ValueError("Invalid runs cursor") from exc


def _mysql_errno(exc: pymysql.err.MySQLError) -> Optional[int]:
    return exc.args[0] if exc.args and isinstance(exc.args[0], int) else None


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


class RunsSchemaUpgradeError(RuntimeError):
    """Startup couldn't add the run owner columns to `runs`."""


class RunStore(UserStoreMixin):
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
        try:
            await store._ensure_schema()
        except BaseException:
            await store.close()
            raise
        return store

    async def _ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(_CREATE_TABLE_SQL)
                await self._ensure_runs_schema(cursor)
                await cursor.execute(_CREATE_GENE_TABLE_SQL)
                await self._ensure_gene_annotation_schema(cursor)
                # Only provisioned when PMID distillation is on, so the
                # default-off path makes no schema change beyond main's.
                if settings.pmid_distillation_enabled:
                    await cursor.execute(_CREATE_PMID_EVIDENCE_TABLE_SQL)
                await cursor.execute(_CREATE_FEEDBACK_TABLE_SQL)
                await cursor.execute(_CREATE_API_KEYS_TABLE_SQL)
                await self._ensure_user_schema(cursor)

    async def _ensure_runs_schema(self, cursor) -> None:
        """Add owner/listing columns and index to `runs` tables created before them.

        `runs` can be large, so the columns go in one ALTER that is
        metadata-only where the server supports ALGORITHM=INSTANT, and the
        index is built online (INPLACE, LOCK=NONE). Each DDL waits at most
        `runs_ddl_lock_wait_timeout_seconds` to acquire the table's metadata
        lock; that bounds the wait for other queries, not the DDL's own
        runtime, so an INPLACE fallback or index build on a large table can
        still take long once it has the lock.

        If the columns can't be added, startup fails (RunsSchemaUpgradeError):
        every new run must record its owner, and a restart retries. If only the
        index can't be built, startup continues; listing works without it.
        """
        await cursor.execute("SELECT @@SESSION.lock_wait_timeout")
        previous_timeout = (await cursor.fetchone())[0]
        await cursor.execute(
            "SET SESSION lock_wait_timeout = %s", (settings.runs_ddl_lock_wait_timeout_seconds,)
        )
        try:
            await self._add_runs_owner_columns(cursor)
            await self._add_runs_owner_index(cursor)
        finally:
            await cursor.execute("SET SESSION lock_wait_timeout = %s", (previous_timeout,))

    async def _runs_columns(self, cursor) -> set[str]:
        await cursor.execute("SHOW COLUMNS FROM runs")
        return {row[0] for row in await cursor.fetchall()}

    async def _add_runs_owner_columns(self, cursor) -> None:
        present = await self._runs_columns(cursor)
        missing = [(column, definition) for column, definition in _RUNS_ADDED_COLUMNS if column not in present]
        if not missing:
            return
        add_columns = ", ".join(f"ADD COLUMN {column} {definition}" for column, definition in missing)
        try:
            try:
                await cursor.execute(f"ALTER TABLE runs {add_columns}, ALGORITHM=INSTANT")
            except pymysql.err.MySQLError as exc:
                if _mysql_errno(exc) not in _INSTANT_UNSUPPORTED_ERRORS:
                    raise
                await cursor.execute(f"ALTER TABLE runs {add_columns}, ALGORITHM=INPLACE, LOCK=NONE")
        except pymysql.err.MySQLError as exc:
            if _mysql_errno(exc) == _ER_LOCK_WAIT_TIMEOUT:
                raise RunsSchemaUpgradeError(
                    f"Timed out after {settings.runs_ddl_lock_wait_timeout_seconds}s waiting for the "
                    "metadata lock on `runs` to add the run owner columns, so refusing to start "
                    "(new runs must record their owner). A restart retries; to avoid this, run "
                    f"`ALTER TABLE runs {add_columns}, ALGORITHM=INSTANT` off-peak, or raise "
                    "RUNS_DDL_LOCK_WAIT_TIMEOUT_SECONDS."
                ) from exc
            if _mysql_errno(exc) != _ER_DUP_FIELDNAME:
                raise
        # Re-read: a concurrent pod's upgrade (a duplicate-column error above)
        # may have added only some of them.
        still_missing = [column for column, _ in _RUNS_ADDED_COLUMNS if column not in await self._runs_columns(cursor)]
        if still_missing:
            raise RunsSchemaUpgradeError(
                f"`runs` is still missing run owner columns {still_missing} after the upgrade; "
                "refusing to start. A restart retries."
            )

    async def _add_runs_owner_index(self, cursor) -> None:
        await cursor.execute(f"SHOW INDEX FROM runs WHERE Key_name = '{_RUNS_OWNER_INDEX}'")
        if await cursor.fetchall():
            return
        try:
            await cursor.execute(
                f"ALTER TABLE runs ADD INDEX {_RUNS_OWNER_INDEX} (owner_email, created_at, run_id), "
                "ALGORITHM=INPLACE, LOCK=NONE"
            )
        except pymysql.err.MySQLError as exc:
            if _mysql_errno(exc) == _ER_LOCK_WAIT_TIMEOUT:
                # Owner columns are in place, so runs are still saved with their
                # owner; listing just scans instead of using the index.
                logger.warning(
                    "Timed out after %ss waiting for the metadata lock on `runs` to add index %s; "
                    "starting without it (run listing is slower until a restart adds it).",
                    settings.runs_ddl_lock_wait_timeout_seconds,
                    _RUNS_OWNER_INDEX,
                )
            elif _mysql_errno(exc) != _ER_DUP_KEYNAME:
                raise

    async def _ensure_gene_annotation_schema(self, cursor) -> None:
        """Migrate older gene-only annotation caches to gene + tumor-type keys."""
        await cursor.execute("SHOW COLUMNS FROM gene_annotations LIKE 'tumor_type'")
        if await cursor.fetchone() is None:
            await cursor.execute(
                "ALTER TABLE gene_annotations "
                "ADD COLUMN tumor_type VARCHAR(128) NOT NULL DEFAULT '' AFTER gene"
            )

        await cursor.execute("SHOW INDEX FROM gene_annotations WHERE Key_name = 'PRIMARY'")
        rows = await cursor.fetchall()
        primary_columns = [row[4] for row in sorted(rows, key=lambda row: row[3])]
        if primary_columns == ["gene"]:
            await cursor.execute(
                "ALTER TABLE gene_annotations "
                "DROP PRIMARY KEY, ADD PRIMARY KEY (gene, tumor_type)"
            )

    async def save_run(
        self,
        run_id: str,
        created_at: str,
        request_payload: Dict[str, Any],
        result_payload: Dict[str, Any],
        *,
        owner_email: Optional[str] = None,
        source: Optional[str] = None,
    ) -> None:
        input_summary, input_count = summarize_run_inputs(request_payload)
        genes_annotated = result_payload.get("genes_annotated")
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "INSERT INTO runs (run_id, created_at, request_json, result_json, "
                    "owner_email, source, input_summary, input_count, genes_annotated) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        run_id,
                        _to_utc_datetime(created_at),
                        json.dumps(request_payload),
                        json.dumps(result_payload),
                        owner_email.strip().lower() if owner_email else None,
                        source,
                        input_summary,
                        input_count,
                        genes_annotated if isinstance(genes_annotated, int) else None,
                    ),
                )

    async def list_runs_for_owner(
        self,
        owner_email: str,
        limit: int = 50,
        cursor: Optional[str] = None,
    ) -> RunSummaryPage:
        """A user's saved runs, newest first, `limit` per page.

        Reads only the listing columns (never request/result JSON). `cursor` is
        the previous page's `next_cursor`; raises ValueError if it is malformed.
        """
        if limit < 1:
            raise ValueError("limit must be at least 1")
        before = _decode_runs_cursor(cursor) if cursor is not None else None
        sql = (
            "SELECT run_id, created_at, source, input_summary, input_count, genes_annotated "
            "FROM runs WHERE owner_email = %s"
        )
        params: list = [owner_email.strip().lower()]
        if before is not None:
            before_created_at, before_run_id = before
            before_naive = _to_utc_datetime(before_created_at)
            sql += " AND (created_at < %s OR (created_at = %s AND run_id < %s))"
            params += [before_naive, before_naive, before_run_id]
        # One extra row tells us whether another page exists.
        sql += " ORDER BY created_at DESC, run_id DESC LIMIT %s"
        params.append(limit + 1)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as db_cursor:
                await db_cursor.execute(sql, tuple(params))
                rows = await db_cursor.fetchall()
        runs = [
            RunSummary(
                run_id=row[0],
                created_at=_from_mysql_datetime(row[1]),
                source=row[2],
                input_summary=row[3],
                input_count=row[4],
                genes_annotated=row[5],
            )
            for row in rows[:limit]
        ]
        next_cursor = None
        if len(rows) > limit:
            last = runs[-1]
            next_cursor = _encode_runs_cursor(last.created_at, last.run_id)
        return RunSummaryPage(runs=runs, next_cursor=next_cursor)

    async def update_run_result(self, run_id: str, result_payload: Dict[str, Any]) -> None:
        """Overwrite a saved run's result (e.g. once a background job, like fusion
        evidence retrieval, completes after the initial save)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE runs SET result_json = %s WHERE run_id = %s",
                    (json.dumps(result_payload), run_id),
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
        tumor_type: Optional[str] = None,
    ) -> None:
        """Upsert the latest reusable annotation for a canonical gene and tumor context."""
        payload = annotation.model_dump()
        cache_tumor_type = _normalize_cache_tumor_type(tumor_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO gene_annotations (
                        gene,
                        tumor_type,
                        updated_at,
                        last_pubmed_checked_at,
                        in_oncokb,
                        evidence_support_score,
                        insufficient_evidence,
                        annotation_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
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
                        cache_tumor_type,
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

    async def get_gene_annotation(
        self,
        gene: str,
        tumor_type: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return cached annotation payload and freshness metadata for a gene/tumor context."""
        cache_tumor_type = _normalize_cache_tumor_type(tumor_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT annotation_json, updated_at, last_pubmed_checked_at, tumor_type
                    FROM gene_annotations
                    WHERE gene = %s AND tumor_type = %s
                    """,
                    (gene, cache_tumor_type),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "annotation": json.loads(row[0]),
            "updated_at": _from_mysql_datetime(row[1]),
            "last_pubmed_checked_at": _from_mysql_datetime(row[2]),
            "tumor_type": row[3] or None,
        }

    async def mark_gene_pubmed_checked(
        self,
        gene: str,
        checked_at: str | datetime,
        annotation: GeneAnnotation | None = None,
        tumor_type: Optional[str] = None,
    ) -> None:
        """Record that PubMed freshness was checked without needing regeneration."""
        checked = _to_utc_datetime(checked_at)
        cache_tumor_type = _normalize_cache_tumor_type(tumor_type)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                if annotation is None:
                    await cursor.execute(
                        "UPDATE gene_annotations SET last_pubmed_checked_at = %s "
                        "WHERE gene = %s AND tumor_type = %s",
                        (checked, gene, cache_tumor_type),
                    )
                else:
                    payload = annotation.model_dump()
                    await cursor.execute(
                        """
                        UPDATE gene_annotations
                        SET last_pubmed_checked_at = %s, annotation_json = %s
                        WHERE gene = %s AND tumor_type = %s
                        """,
                        (checked, json.dumps(payload), gene, cache_tumor_type),
                    )

    async def get_pmid_evidence_batch(self, pmids: List[str]) -> Dict[str, PMIDEvidenceRecord]:
        """Return cached distilled-abstract records for any of `pmids` that
        have already been distilled, keyed by PMID. PMIDs with no cached
        record are simply absent from the result — callers fall back to the
        raw abstract for those (see synthesis.py's _build_user_prompt)."""
        if not pmids:
            return {}
        placeholders = ", ".join(["%s"] * len(pmids))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"""
                    SELECT pmid, doi, title, journal, publication_year, evidence_type,
                           oncogenic_role, distilled_takeaway, supporting_quote,
                           created_at, updated_at
                    FROM pmid_evidence
                    WHERE pmid IN ({placeholders})
                    """,
                    tuple(pmids),
                )
                rows = await cursor.fetchall()
        records: Dict[str, PMIDEvidenceRecord] = {}
        for row in rows:
            record = PMIDEvidenceRecord(
                pmid=row[0],
                doi=row[1],
                title=row[2],
                journal=row[3],
                publication_year=row[4],
                evidence_type=row[5],
                oncogenic_role=row[6],
                distilled_takeaway=row[7],
                supporting_quote=row[8],
                created_at=_from_mysql_datetime(row[9]),
                updated_at=_from_mysql_datetime(row[10]),
            )
            records[record.pmid] = record
        return records

    async def save_pmid_evidence_batch(self, records: List[PMIDEvidenceRecord]) -> None:
        """Upsert distilled-abstract records. Published papers are permanent
        historical records, so — unlike the gene-annotation cache above —
        there is no TTL/age-based eviction here; a record is only ever
        overwritten by a fresher distillation of the same PMID."""
        if not records:
            return
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    """
                    INSERT INTO pmid_evidence (
                        pmid, doi, title, journal, publication_year, evidence_type,
                        oncogenic_role, distilled_takeaway, supporting_quote
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        doi = VALUES(doi),
                        title = VALUES(title),
                        journal = VALUES(journal),
                        publication_year = VALUES(publication_year),
                        evidence_type = VALUES(evidence_type),
                        oncogenic_role = VALUES(oncogenic_role),
                        distilled_takeaway = VALUES(distilled_takeaway),
                        supporting_quote = VALUES(supporting_quote)
                    """,
                    [
                        (
                            record.pmid,
                            record.doi,
                            record.title,
                            record.journal,
                            record.publication_year,
                            record.evidence_type,
                            record.oncogenic_role,
                            record.distilled_takeaway,
                            record.supporting_quote,
                        )
                        for record in records
                    ],
                )

    async def save_feedback(
        self,
        feedback_id: str,
        created_at: str | datetime,
        category: str,
        message: str,
        contact_email: Optional[str],
        run_id: Optional[str],
        gene: Optional[str],
        page_url: Optional[str],
        user_agent: Optional[str],
    ) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO feedback (
                        feedback_id, created_at, category, message,
                        contact_email, run_id, gene, page_url, user_agent
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        feedback_id,
                        _to_utc_datetime(created_at),
                        category,
                        message,
                        contact_email,
                        run_id,
                        gene,
                        page_url,
                        user_agent,
                    ),
                )

    # -- API keys ------------------------------------------------------------

    @staticmethod
    def _api_key_from_row(row) -> ApiKeyRecord:
        return ApiKeyRecord(
            id=row[0],
            key_hash=row[1],
            owner_email=row[2],
            name=row[3],
            created_at=_from_mysql_datetime(row[4]),
            last_used_at=_from_mysql_datetime(row[5]),
            revoked_at=_from_mysql_datetime(row[6]),
            expires_at=_from_mysql_datetime(row[7]),
        )

    async def create_api_key(self, record: ApiKeyRecord) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"INSERT INTO api_keys ({_API_KEY_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        record.id,
                        record.key_hash,
                        record.owner_email,
                        record.name,
                        _to_utc_datetime(record.created_at),
                        _to_utc_datetime(record.last_used_at) if record.last_used_at else None,
                        _to_utc_datetime(record.revoked_at) if record.revoked_at else None,
                        _to_utc_datetime(record.expires_at) if record.expires_at else None,
                    ),
                )

    async def get_api_key_by_hash(self, key_hash: str) -> Optional[ApiKeyRecord]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE key_hash = %s",
                    (key_hash,),
                )
                row = await cursor.fetchone()
        return self._api_key_from_row(row) if row else None

    async def get_api_key(self, key_id: str) -> Optional[ApiKeyRecord]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_API_KEY_COLUMNS} FROM api_keys WHERE id = %s", (key_id,)
                )
                row = await cursor.fetchone()
        return self._api_key_from_row(row) if row else None

    async def list_api_keys(self, owner_email: Optional[str] = None) -> List[ApiKeyRecord]:
        """List keys for one owner, or every key when owner_email is None."""
        sql = f"SELECT {_API_KEY_COLUMNS} FROM api_keys"
        params: tuple = ()
        if owner_email is not None:
            sql += " WHERE owner_email = %s"
            params = (owner_email,)
        sql += " ORDER BY created_at DESC"
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)
                rows = await cursor.fetchall()
        return [self._api_key_from_row(row) for row in rows]

    async def revoke_api_key(self, key_id: str, revoked_at: datetime) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE api_keys SET revoked_at = %s WHERE id = %s AND revoked_at IS NULL",
                    (_to_utc_datetime(revoked_at), key_id),
                )

    async def touch_api_key(self, key_id: str, used_at: datetime) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "UPDATE api_keys SET last_used_at = %s WHERE id = %s",
                    (_to_utc_datetime(used_at), key_id),
                )

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()
