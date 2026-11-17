"""Module-owned persistence: the ``expression_context_results`` table (verdicts).

The statistics bundle tables live in ``bundle_store.py``.

The table is created by this module only when the feature is enabled. It has
no foreign keys to, and adds no columns on, any existing ACGC table. The
module uses its own small aiomysql pool (built from ACGC's MySQL connection
settings, read-only) so it never reaches into the RunStore's internals.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple
from src.expression_context.models import (
    BATCH_GENE_STATUSES,
    BatchCounts,
    BatchGeneRecord,
    BatchGeneStatus,
    BatchRecord,
    BatchStatus,
    HistoryEntry,
    StoredPair,
)

logger = logging.getLogger(__name__)

TABLE_NAME = "expression_context_results"
BATCHES_TABLE = "expression_context_batches"
BATCH_GENES_TABLE = "expression_context_batch_genes"
BATCH_TABLES = (BATCHES_TABLE, BATCH_GENES_TABLE)

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
    hugo_symbol VARCHAR(64) NOT NULL,
    oncotree_code VARCHAR(32) NOT NULL,
    release_id VARCHAR(128) NOT NULL,
    literature_gate VARCHAR(64) NOT NULL,
    surfaced BOOLEAN NOT NULL,
    exclusion_reason VARCHAR(16) NULL,
    stats_json JSON NOT NULL,
    evidence_json JSON NULL,
    computed_at DATETIME(6) NOT NULL,
    requested_by VARCHAR(255) NULL,
    UNIQUE KEY uq_expression_context_pair (hugo_symbol, oncotree_code, release_id, literature_gate),
    INDEX idx_expression_context_gene (hugo_symbol, literature_gate, computed_at),
    INDEX idx_expression_context_computed (literature_gate, computed_at),
    INDEX idx_expression_context_release_gene (release_id, literature_gate, hugo_symbol),
    INDEX idx_expression_context_verdict (hugo_symbol, literature_gate, oncotree_code, computed_at)
)
"""

CREATE_BATCH_TABLES_SQL = (
    f"""
CREATE TABLE IF NOT EXISTS {BATCHES_TABLE} (
    batch_id VARCHAR(64) NOT NULL PRIMARY KEY,
    created_by VARCHAR(255) NULL,
    created_at DATETIME(6) NOT NULL,
    release_id VARCHAR(128) NOT NULL,
    status VARCHAR(16) NOT NULL,
    counts_json JSON NOT NULL,
    error VARCHAR(1000) NULL,
    worker_owner VARCHAR(64) NULL,
    worker_expires_at DATETIME(6) NULL,
    INDEX idx_expression_context_batches_creator (created_by, created_at),
    INDEX idx_expression_context_batches_status (status, created_at)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS {BATCH_GENES_TABLE} (
    batch_id VARCHAR(64) NOT NULL,
    position INT NOT NULL,
    input_symbol VARCHAR(64) NOT NULL,
    hugo_symbol VARCHAR(64) NULL,
    status VARCHAR(32) NOT NULL,
    error VARCHAR(1000) NULL,
    notes VARCHAR(1000) NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (batch_id, position)
)
""",
)

_COLUMNS = (
    "hugo_symbol, oncotree_code, release_id, literature_gate, surfaced, exclusion_reason, "
    "stats_json, evidence_json, computed_at, requested_by"
)

Cursor = Tuple[datetime, str, str]


class InvalidCursor(ValueError):
    pass


def encode_cursor(entry: HistoryEntry) -> str:
    raw = json.dumps([entry.computed_at.isoformat(), entry.hugo_symbol, entry.release_id])
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> Cursor:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        computed_at, symbol, release_id = json.loads(base64.urlsafe_b64decode(padded.encode()))
        when = datetime.fromisoformat(computed_at)
        if not isinstance(symbol, str) or not isinstance(release_id, str):
            raise TypeError
    except (binascii.Error, ValueError, TypeError, UnicodeDecodeError) as exc:
        raise InvalidCursor("invalid cursor") from exc
    return _utc(when), symbol, release_id


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive_utc(value: datetime) -> datetime:
    return _utc(value).replace(tzinfo=None)


def mysql_connection_kwargs(acgc_settings: Any) -> Dict[str, Any]:
    """Connection kwargs from ACGC's public settings, same precedence as the
    RunStore uses: DB_URL (jdbc:mysql://host:port[/db][?...]) and
    DB_USERNAME/DB_PASSWORD win over MYSQL_HOST/PORT/USER/PASSWORD."""
    db_url = (acgc_settings.db_url or "").strip()
    if db_url:
        prefix = "jdbc:mysql://"
        if not db_url.startswith(prefix):
            raise ValueError(f"Expected a jdbc:mysql:// URL, got {db_url!r}")
        host_port = db_url[len(prefix):].split("/", 1)[0].split("?", 1)[0]
        host, sep, port = host_port.rpartition(":")
        if not sep:
            raise ValueError(f"jdbc:mysql URL missing port: {db_url!r}")
        port_number = int(port)
    else:
        host, port_number = acgc_settings.mysql_host, acgc_settings.mysql_port
    return {
        "host": host,
        "port": port_number,
        "user": acgc_settings.db_username or acgc_settings.mysql_user,
        "password": acgc_settings.db_password or acgc_settings.mysql_password,
        "db": acgc_settings.mysql_database,
    }


class ExpressionContextStore(Protocol):
    async def ensure_schema(self) -> None: ...

    async def close(self) -> None: ...

    async def get_verdict_pairs(self, hugo_symbol: str, literature_gate: str) -> Dict[str, StoredPair]: ...

    async def upsert_pairs(self, pairs: List[StoredPair]) -> None: ...

    async def latest_pairs(self, hugo_symbol: str, literature_gate: str) -> List[StoredPair]: ...

    async def list_verdict_history(
        self, literature_gate: str, release_id: str, limit: int, cursor: Optional[Cursor]
    ) -> List[HistoryEntry]: ...

    async def has_checked_gene(self, hugo_symbol: str, literature_gate: str) -> bool: ...

    async def list_checked_genes(
        self, literature_gate: str, limit: int, after_symbol: Optional[str] = None
    ) -> List[str]: ...

    async def create_batch(
        self, batch: BatchRecord, genes: Sequence[BatchGeneRecord]
    ) -> None: ...

    async def get_batch(self, batch_id: str) -> Optional[BatchRecord]: ...

    async def list_batch_genes(
        self, batch_id: str, offset: int = 0, limit: Optional[int] = None
    ) -> List[BatchGeneRecord]: ...

    async def count_batch_genes(self, batch_id: str) -> int: ...

    async def list_batches(
        self, created_by: Optional[str], limit: int = 50
    ) -> List[BatchRecord]: ...

    async def list_unfinished_batches(self) -> List[str]: ...

    async def acquire_batch_lock(
        self, batch_id: str, token: str, ttl_seconds: float
    ) -> bool: ...

    async def renew_batch_lock(
        self, batch_id: str, token: str, ttl_seconds: float
    ) -> bool: ...

    async def release_batch_lock(self, batch_id: str, token: str) -> None: ...

    async def update_batch_gene(
        self,
        batch_id: str,
        position: int,
        status: BatchGeneStatus,
        *,
        error: Optional[str] = None,
        updated_at: Optional[datetime] = None,
        expected_token: Optional[str] = None,
    ) -> Optional[BatchCounts]: ...

    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        error: Optional[str] = None,
        expected_token: Optional[str] = None,
    ) -> Optional[BatchRecord]: ...

    async def cancel_batch(
        self, batch_id: str, *, updated_at: Optional[datetime] = None
    ) -> Optional[BatchRecord]: ...


def compute_batch_counts(genes: Sequence[BatchGeneRecord]) -> BatchCounts:
    counts: Dict[str, int] = {status: 0 for status in BATCH_GENE_STATUSES}
    for gene in genes:
        if gene.status in counts:
            counts[gene.status] += 1
    return BatchCounts(total=len(genes), **counts)


def _history_sort_key(entry: HistoryEntry) -> Cursor:
    return (entry.computed_at, entry.hugo_symbol, entry.release_id)


def _clip(text: Optional[str], limit: int = 1000) -> Optional[str]:
    return None if text is None else str(text)[:limit]


class InMemoryExpressionContextStore:
    """Same semantics as the MySQL store; used by tests and local runs."""

    def __init__(self, *, lock_clock: Callable[[], float] = time.monotonic) -> None:
        self.rows: Dict[Tuple[str, str, str, str], StoredPair] = {}
        self.batches: Dict[str, BatchRecord] = {}
        self.batch_genes: Dict[str, List[BatchGeneRecord]] = {}
        self.batch_locks: Dict[str, Tuple[str, float]] = {}
        self.lock_clock = lock_clock
        self.schema_created = False

    async def ensure_schema(self) -> None:
        self.schema_created = True

    async def close(self) -> None:
        pass

    async def get_verdict_pairs(self, hugo_symbol, literature_gate):
        result = {}
        for row in self.rows.values():
            if (row.hugo_symbol != hugo_symbol or row.literature_gate != literature_gate
                    or not isinstance(row.evidence, dict)
                    or row.evidence.get("literature_gate") != literature_gate):
                continue
            prior = result.get(row.oncotree_code)
            if prior is None or (row.computed_at, row.release_id) > (prior.computed_at, prior.release_id):
                result[row.oncotree_code] = row
        return result

    async def upsert_pairs(self, pairs):
        for pair in pairs:
            key = (pair.hugo_symbol, pair.oncotree_code, pair.release_id, pair.literature_gate)
            self.rows[key] = pair.model_copy(update={"computed_at": _utc(pair.computed_at)})

    async def latest_pairs(self, hugo_symbol, literature_gate):
        rows = [
            row
            for row in self.rows.values()
            if row.hugo_symbol == hugo_symbol and row.literature_gate == literature_gate
        ]
        if not rows:
            return []
        latest_release = max(rows, key=lambda row: row.computed_at).release_id
        return [row for row in rows if row.release_id == latest_release]

    async def list_verdict_history(self, literature_gate, release_id, limit, cursor):
        groups: Dict[str, Dict[str, StoredPair]] = {}
        for row in self.rows.values():
            if row.literature_gate != literature_gate or (
                row.evidence is not None and row.evidence.get("literature_gate") != literature_gate
            ):
                continue
            pairs = groups.setdefault(row.hugo_symbol, {})
            prior = pairs.get(row.oncotree_code)
            if prior is None or (row.computed_at, row.release_id) > (prior.computed_at, prior.release_id):
                pairs[row.oncotree_code] = row
        entries = [HistoryEntry(
            hugo_symbol=symbol, release_id=release_id, literature_gate=literature_gate,
            computed_at=max(row.computed_at for row in pairs.values()),
            pair_count=len(pairs), surfaced_count=0,
        ) for symbol, pairs in groups.items()]
        entries.sort(key=_history_sort_key, reverse=True)
        return [entry for entry in entries if cursor is None or _history_sort_key(entry) < cursor][:limit]

    async def has_checked_gene(self, hugo_symbol, literature_gate):
        return any(row.hugo_symbol == hugo_symbol and row.literature_gate == literature_gate
                   for row in self.rows.values())

    async def list_checked_genes(self, literature_gate, limit, after_symbol=None):
        return sorted({
            row.hugo_symbol for row in self.rows.values()
            if row.literature_gate == literature_gate
            and (after_symbol is None or row.hugo_symbol > after_symbol)
        })[:limit]

    async def create_batch(self, batch: BatchRecord, genes: Sequence[BatchGeneRecord]) -> None:
        ordered = sorted(
            (
                g.model_copy(
                    update={
                        "input_symbol": _clip(g.input_symbol, limit=64) or "",
                        "updated_at": _utc(g.updated_at),
                        "error": _clip(g.error),
                        "notes": _clip(g.notes),
                    }
                )
                for g in genes
            ),
            key=lambda g: g.position,
        )
        counts = compute_batch_counts(ordered)
        self.batches[batch.batch_id] = batch.model_copy(
            update={
                "created_at": _utc(batch.created_at),
                "counts": counts,
                "error": _clip(batch.error),
            }
        )
        self.batch_genes[batch.batch_id] = ordered

    async def get_batch(self, batch_id: str) -> Optional[BatchRecord]:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        genes = self.batch_genes.get(batch_id, [])
        counts = compute_batch_counts(genes)
        if batch.counts != counts:
            batch = batch.model_copy(update={"counts": counts})
            self.batches[batch_id] = batch
        return batch.model_copy(deep=True)

    async def list_batch_genes(
        self, batch_id: str, offset: int = 0, limit: Optional[int] = None
    ) -> List[BatchGeneRecord]:
        genes = sorted(self.batch_genes.get(batch_id, []), key=lambda g: g.position)
        sliced = genes[max(0, offset) :]
        if limit is not None:
            sliced = sliced[: max(0, limit)]
        return [g.model_copy(deep=True) for g in sliced]

    async def count_batch_genes(self, batch_id: str) -> int:
        return len(self.batch_genes.get(batch_id, []))

    async def list_batches(
        self, created_by: Optional[str], limit: int = 50
    ) -> List[BatchRecord]:
        items = [
            b
            for b in self.batches.values()
            if created_by is None or b.created_by == created_by
        ]
        items.sort(key=lambda b: (b.created_at, b.batch_id), reverse=True)
        out: List[BatchRecord] = []
        for b in items[: max(1, limit)]:
            refreshed = await self.get_batch(b.batch_id)
            if refreshed is not None:
                out.append(refreshed)
        return out

    async def list_unfinished_batches(self) -> List[str]:
        unfinished = [
            b
            for b in self.batches.values()
            if b.status in ("queued", "running")
        ]
        unfinished.sort(key=lambda b: (b.created_at, b.batch_id))
        return [b.batch_id for b in unfinished]

    def _holds_batch_lock(self, batch_id: str, token: str) -> bool:
        entry = self.batch_locks.get(batch_id)
        if entry is None:
            return False
        owner, expires = entry
        return owner == token and self.lock_clock() <= expires

    async def acquire_batch_lock(
        self, batch_id: str, token: str, ttl_seconds: float
    ) -> bool:
        if batch_id not in self.batches:
            return False
        now = self.lock_clock()
        entry = self.batch_locks.get(batch_id)
        if entry is None or now > entry[1] or entry[0] == token:
            self.batch_locks[batch_id] = (token, now + ttl_seconds)
            return True
        return False

    async def renew_batch_lock(
        self, batch_id: str, token: str, ttl_seconds: float
    ) -> bool:
        if not self._holds_batch_lock(batch_id, token):
            return False
        self.batch_locks[batch_id] = (token, self.lock_clock() + ttl_seconds)
        return True

    async def release_batch_lock(self, batch_id: str, token: str) -> None:
        entry = self.batch_locks.get(batch_id)
        if entry is not None and entry[0] == token:
            del self.batch_locks[batch_id]

    async def update_batch_gene(
        self,
        batch_id: str,
        position: int,
        status: BatchGeneStatus,
        *,
        error: Optional[str] = None,
        updated_at: Optional[datetime] = None,
        expected_token: Optional[str] = None,
    ) -> Optional[BatchCounts]:
        if batch_id not in self.batches:
            return None
        batch = self.batches[batch_id]
        if batch.status == "cancelled":
            return None
        if expected_token is not None and not self._holds_batch_lock(batch_id, expected_token):
            return None
        when = _utc(updated_at or datetime.now(timezone.utc))
        genes = self.batch_genes.get(batch_id, [])
        for idx, gene in enumerate(genes):
            if gene.position == position:
                genes[idx] = gene.model_copy(
                    update={"status": status, "error": _clip(error), "updated_at": when}
                )
                break
        counts = compute_batch_counts(genes)
        self.batches[batch_id] = self.batches[batch_id].model_copy(update={"counts": counts})
        return counts

    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        error: Optional[str] = None,
        expected_token: Optional[str] = None,
    ) -> Optional[BatchRecord]:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        if batch.status == "cancelled":
            return None
        if expected_token is not None and not self._holds_batch_lock(batch_id, expected_token):
            return None
        genes = self.batch_genes.get(batch_id, [])
        counts = compute_batch_counts(genes)
        updated = batch.model_copy(
            update={"status": status, "counts": counts, "error": _clip(error)}
        )
        self.batches[batch_id] = updated
        return updated.model_copy(deep=True)

    async def cancel_batch(
        self, batch_id: str, *, updated_at: Optional[datetime] = None
    ) -> Optional[BatchRecord]:
        batch = self.batches.get(batch_id)
        if batch is None:
            return None
        if batch.status in ("done", "failed", "cancelled"):
            return await self.get_batch(batch_id)
        when = _utc(updated_at or datetime.now(timezone.utc))
        genes = self.batch_genes.get(batch_id, [])
        for idx, gene in enumerate(genes):
            if gene.status in ("queued", "running"):
                genes[idx] = gene.model_copy(
                    update={
                        "status": "failed",
                        "error": "cancelled",
                        "updated_at": when,
                    }
                )
        counts = compute_batch_counts(genes)
        updated = batch.model_copy(update={"status": "cancelled", "counts": counts})
        self.batches[batch_id] = updated
        return updated.model_copy(deep=True)



class MySQLExpressionContextStore:
    def __init__(self, pool: Any) -> None:
        self._pool = pool

    @property
    def pool(self) -> Any:
        """The module's MySQL pool (shared with the bundle store; closed by close())."""
        return self._pool

    @classmethod
    async def create(cls, maxsize: int = 2) -> "MySQLExpressionContextStore":
        import aiomysql

        from src.config import settings as acgc_settings

        pool = await aiomysql.create_pool(
            **mysql_connection_kwargs(acgc_settings),
            autocommit=True,
            minsize=1,
            maxsize=maxsize,
            pool_recycle=acgc_settings.mysql_pool_recycle_seconds,
        )
        return cls(pool)

    async def close(self) -> None:
        self._pool.close()
        await self._pool.wait_closed()

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(CREATE_TABLE_SQL)
                await cursor.execute(
                    f"SHOW INDEX FROM {TABLE_NAME} WHERE Key_name = 'idx_expression_context_release_gene'"
                )
                if not await cursor.fetchone():
                    try:
                        await cursor.execute(
                            f"ALTER TABLE {TABLE_NAME} ADD INDEX idx_expression_context_release_gene (release_id, literature_gate, hugo_symbol)"
                        )
                    except Exception as exc:
                        # Swallow ONLY duplicate-index error (MySQL error 1061)
                        errno = getattr(exc, "errno", None)
                        if errno is None and getattr(exc, "args", None):
                            errno = exc.args[0]
                        if errno == 1061:
                            logger.debug("idx_expression_context_release_gene index already exists: %s", exc)
                        else:
                            logger.warning("Failed to create idx_expression_context_release_gene index: %s", exc)
                            raise
                await cursor.execute(
                    f"SHOW INDEX FROM {TABLE_NAME} WHERE Key_name = 'idx_expression_context_verdict'"
                )
                if not await cursor.fetchone():
                    try:
                        await cursor.execute(
                            f"ALTER TABLE {TABLE_NAME} ADD INDEX idx_expression_context_verdict "
                            "(hugo_symbol, literature_gate, oncotree_code, computed_at)"
                        )
                    except Exception as exc:
                        errno = getattr(exc, "errno", None)
                        if errno is None and getattr(exc, "args", None):
                            errno = exc.args[0]
                        if errno != 1061:
                            raise
                for ddl in CREATE_BATCH_TABLES_SQL:
                    await cursor.execute(ddl)
                await cursor.execute(
                    f"SHOW INDEX FROM {BATCH_GENES_TABLE} WHERE Key_name = 'idx_expression_context_batch_genes_pos'"
                )
                if await cursor.fetchone():
                    try:
                        await cursor.execute(
                            f"ALTER TABLE {BATCH_GENES_TABLE} DROP INDEX idx_expression_context_batch_genes_pos"
                        )
                    except Exception as exc:
                        errno = getattr(exc, "errno", None)
                        if errno is None and getattr(exc, "args", None):
                            errno = exc.args[0]
                        if errno != 1091 and "doesn't exist" not in str(exc).lower():
                            raise

    @staticmethod
    def _row_to_pair(row: Tuple[Any, ...]) -> StoredPair:
        (symbol, code, release_id, gate, surfaced, reason, stats_json, evidence_json,
         computed_at, requested_by) = row
        return StoredPair(
            hugo_symbol=symbol,
            oncotree_code=code,
            release_id=release_id,
            literature_gate=gate,
            surfaced=bool(surfaced),
            exclusion_reason=reason,
            stats=json.loads(stats_json) if isinstance(stats_json, (str, bytes)) else stats_json,
            evidence=(
                json.loads(evidence_json)
                if isinstance(evidence_json, (str, bytes))
                else evidence_json
            ),
            computed_at=_utc(computed_at),
            requested_by=requested_by,
        )

    async def get_verdict_pairs(self, hugo_symbol, literature_gate):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_COLUMNS} FROM (SELECT {_COLUMNS}, "
                    "ROW_NUMBER() OVER (PARTITION BY oncotree_code "
                    "ORDER BY computed_at DESC, release_id DESC) AS verdict_rank "
                    f"FROM {TABLE_NAME} "
                    "WHERE hugo_symbol = %s AND literature_gate = %s "
                    "AND JSON_UNQUOTE(JSON_EXTRACT(evidence_json, '$.literature_gate')) = %s) "
                    "ranked WHERE verdict_rank = 1",
                    (hugo_symbol, literature_gate, literature_gate),
                )
                rows = await cursor.fetchall()
        result = {}
        for raw in rows:
            pair = self._row_to_pair(raw)
            result.setdefault(pair.oncotree_code, pair)
        return result

    async def upsert_pairs(self, pairs):
        if not pairs:
            return
        values = [
            (
                pair.hugo_symbol,
                pair.oncotree_code,
                pair.release_id,
                pair.literature_gate,
                pair.surfaced,
                pair.exclusion_reason,
                json.dumps(pair.stats),
                json.dumps(pair.evidence) if pair.evidence is not None else None,
                _naive_utc(pair.computed_at),
                pair.requested_by,
            )
            for pair in pairs
        ]
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    f"INSERT INTO {TABLE_NAME} ({_COLUMNS}) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE "
                    "surfaced = VALUES(surfaced), "
                    "exclusion_reason = VALUES(exclusion_reason), "
                    "stats_json = VALUES(stats_json), "
                    "evidence_json = VALUES(evidence_json), "
                    "computed_at = VALUES(computed_at), "
                    "requested_by = VALUES(requested_by)",
                    values,
                )

    async def latest_pairs(self, hugo_symbol, literature_gate):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT release_id FROM {TABLE_NAME} "
                    "WHERE hugo_symbol = %s AND literature_gate = %s "
                    "ORDER BY computed_at DESC LIMIT 1",
                    (hugo_symbol, literature_gate),
                )
                latest = await cursor.fetchone()
                if latest is None:
                    return []
                await cursor.execute(
                    f"SELECT {_COLUMNS} FROM {TABLE_NAME} "
                    "WHERE hugo_symbol = %s AND literature_gate = %s AND release_id = %s",
                    (hugo_symbol, literature_gate, latest[0]),
                )
                rows = await cursor.fetchall()
        return [self._row_to_pair(row) for row in rows]

    async def list_verdict_history(self, literature_gate, release_id, limit, cursor):
        params: List[Any] = [literature_gate, literature_gate]
        having = ""
        if cursor is not None:
            when, symbol, _ = cursor
            having = "HAVING last_computed_at < %s OR (last_computed_at = %s AND hugo_symbol < %s) "
            params.extend([_naive_utc(when), _naive_utc(when), symbol])
        params.append(limit)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as db_cursor:
                await db_cursor.execute(
                    "SELECT hugo_symbol, MAX(computed_at) AS last_computed_at, "
                    "COUNT(DISTINCT oncotree_code) AS pair_count "
                    f"FROM {TABLE_NAME} WHERE literature_gate = %s "
                    "AND (evidence_json IS NULL OR "
                    "JSON_UNQUOTE(JSON_EXTRACT(evidence_json, '$.literature_gate')) = %s) "
                    "GROUP BY hugo_symbol " + having +
                    "ORDER BY last_computed_at DESC, hugo_symbol DESC LIMIT %s", params,
                )
                rows = await db_cursor.fetchall()
        return [HistoryEntry(
            hugo_symbol=symbol, release_id=release_id, literature_gate=literature_gate,
            computed_at=_utc(computed_at), pair_count=int(count), surfaced_count=0,
        ) for symbol, computed_at, count in rows]

    async def has_checked_gene(self, hugo_symbol, literature_gate):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT 1 FROM {TABLE_NAME} "
                    "WHERE hugo_symbol = %s AND literature_gate = %s LIMIT 1",
                    (hugo_symbol, literature_gate),
                )
                return bool(await cursor.fetchall())

    async def list_checked_genes(self, literature_gate, limit, after_symbol=None):
        where = "WHERE literature_gate = %s "
        params: List[Any] = [literature_gate]
        if after_symbol is not None:
            where += "AND hugo_symbol > %s "
            params.append(after_symbol)
        params.append(limit)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT DISTINCT hugo_symbol FROM {TABLE_NAME} "
                    f"{where}ORDER BY hugo_symbol ASC LIMIT %s", params,
                )
                rows = await cursor.fetchall()
        return [row[0] for row in rows]

    @staticmethod
    def _row_to_batch(row: Tuple[Any, ...]) -> BatchRecord:
        batch_id, created_by, created_at, release_id, status, counts_raw, error = row
        counts_dict = (
            json.loads(counts_raw) if isinstance(counts_raw, (str, bytes)) else (counts_raw or {})
        )
        return BatchRecord(
            batch_id=batch_id,
            created_by=created_by,
            created_at=_utc(created_at),
            release_id=release_id,
            status=status,
            counts=BatchCounts.model_validate(counts_dict),
            error=error,
        )

    @staticmethod
    def _row_to_batch_gene(row: Tuple[Any, ...]) -> BatchGeneRecord:
        batch_id, position, input_symbol, hugo_symbol, status, error, notes, updated_at = row
        return BatchGeneRecord(
            batch_id=batch_id,
            position=int(position),
            input_symbol=input_symbol,
            hugo_symbol=hugo_symbol,
            status=status,
            error=error,
            notes=notes,
            updated_at=_utc(updated_at),
        )

    async def _recompute_batch_counts(self, cursor: Any, batch_id: str) -> BatchCounts:
        await cursor.execute(
            f"SELECT status, COUNT(*) FROM {BATCH_GENES_TABLE} "
            "WHERE batch_id = %s GROUP BY status",
            (batch_id,),
        )
        status_rows = await cursor.fetchall()
        counts_map: Dict[str, int] = {status: 0 for status in BATCH_GENE_STATUSES}
        total = 0
        for status_val, cnt in status_rows:
            c = int(cnt)
            total += c
            if status_val in counts_map:
                counts_map[status_val] = c
        counts = BatchCounts(total=total, **counts_map)
        await cursor.execute(
            f"UPDATE {BATCHES_TABLE} SET counts_json = %s WHERE batch_id = %s",
            (json.dumps(counts.model_dump()), batch_id),
        )
        return counts

    async def create_batch(
        self, batch: BatchRecord, genes: Sequence[BatchGeneRecord]
    ) -> None:
        ordered = sorted(genes, key=lambda g: g.position)
        counts = compute_batch_counts(ordered)
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"INSERT INTO {BATCHES_TABLE} "
                        "(batch_id, created_by, created_at, release_id, status, counts_json, error) "
                        "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (
                            batch.batch_id,
                            batch.created_by,
                            _naive_utc(batch.created_at),
                            batch.release_id,
                            batch.status,
                            json.dumps(counts.model_dump()),
                            _clip(batch.error),
                        ),
                    )
                    if ordered:
                        await cursor.executemany(
                            f"INSERT INTO {BATCH_GENES_TABLE} "
                            "(batch_id, position, input_symbol, hugo_symbol, status, error, notes, updated_at) "
                            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            [
                                (
                                    g.batch_id,
                                    g.position,
                                    _clip(g.input_symbol, limit=64) or "",
                                    g.hugo_symbol,
                                    g.status,
                                    _clip(g.error),
                                    _clip(g.notes),
                                    _naive_utc(g.updated_at),
                                )
                                for g in ordered
                            ],
                        )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise

    async def get_batch(self, batch_id: str) -> Optional[BatchRecord]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT batch_id, created_by, created_at, release_id, status, counts_json, error "
                    f"FROM {BATCHES_TABLE} WHERE batch_id = %s",
                    (batch_id,),
                )
                row = await cursor.fetchone()
        return None if row is None else self._row_to_batch(row)

    async def list_batch_genes(
        self, batch_id: str, offset: int = 0, limit: Optional[int] = None
    ) -> List[BatchGeneRecord]:
        sql = (
            "SELECT batch_id, position, input_symbol, hugo_symbol, status, error, notes, updated_at "
            f"FROM {BATCH_GENES_TABLE} WHERE batch_id = %s ORDER BY position ASC"
        )
        params: List[Any] = [batch_id]
        if limit is not None:
            sql += " LIMIT %s OFFSET %s"
            params.extend([max(0, int(limit)), max(0, int(offset))])
        elif offset > 0:
            sql += " LIMIT 18446744073709551615 OFFSET %s"
            params.append(max(0, int(offset)))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, tuple(params))
                rows = await cursor.fetchall()
        return [self._row_to_batch_gene(row) for row in rows]

    async def count_batch_genes(self, batch_id: str) -> int:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT COUNT(*) FROM {BATCH_GENES_TABLE} WHERE batch_id = %s",
                    (batch_id,),
                )
                row = await cursor.fetchone()
        return int(row[0]) if row else 0

    async def list_batches(
        self, created_by: Optional[str], limit: int = 50
    ) -> List[BatchRecord]:
        where = "WHERE created_by = %s " if created_by is not None else ""
        params: List[Any] = [created_by] if created_by is not None else []
        params.append(max(1, int(limit)))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT batch_id, created_by, created_at, release_id, status, counts_json, error "
                    f"FROM {BATCHES_TABLE} {where}ORDER BY created_at DESC, batch_id DESC LIMIT %s",
                    tuple(params),
                )
                rows = await cursor.fetchall()
        return [self._row_to_batch(row) for row in rows]

    async def list_unfinished_batches(self) -> List[str]:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT batch_id FROM {BATCHES_TABLE} "
                    "WHERE status IN ('queued', 'running') "
                    "ORDER BY created_at ASC, batch_id ASC"
                )
                rows = await cursor.fetchall()
        return [row[0] for row in rows]

    async def acquire_batch_lock(
        self, batch_id: str, token: str, ttl_seconds: float
    ) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"UPDATE {BATCHES_TABLE} SET worker_owner = %s, "
                    "worker_expires_at = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND "
                    "WHERE batch_id = %s AND (worker_owner IS NULL "
                    "OR worker_expires_at < UTC_TIMESTAMP(6) OR worker_owner = %s)",
                    (token, int(ttl_seconds * 1_000_000), batch_id, token),
                )
                await cursor.execute(
                    f"SELECT COUNT(*) FROM {BATCHES_TABLE} "
                    "WHERE batch_id = %s AND worker_owner = %s "
                    "AND worker_expires_at >= UTC_TIMESTAMP(6)",
                    (batch_id, token),
                )
                row = await cursor.fetchone()
                return bool(row and row[0])

    async def renew_batch_lock(
        self, batch_id: str, token: str, ttl_seconds: float
    ) -> bool:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"UPDATE {BATCHES_TABLE} SET "
                    "worker_expires_at = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND "
                    "WHERE batch_id = %s AND worker_owner = %s "
                    "AND worker_expires_at >= UTC_TIMESTAMP(6)",
                    (int(ttl_seconds * 1_000_000), batch_id, token),
                )
                await cursor.execute(
                    f"SELECT COUNT(*) FROM {BATCHES_TABLE} "
                    "WHERE batch_id = %s AND worker_owner = %s "
                    "AND worker_expires_at >= UTC_TIMESTAMP(6)",
                    (batch_id, token),
                )
                row = await cursor.fetchone()
                return bool(row and row[0])

    async def release_batch_lock(self, batch_id: str, token: str) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"UPDATE {BATCHES_TABLE} SET worker_owner = NULL, worker_expires_at = NULL "
                    "WHERE batch_id = %s AND worker_owner = %s",
                    (batch_id, token),
                )

    async def update_batch_gene(
        self,
        batch_id: str,
        position: int,
        status: BatchGeneStatus,
        *,
        error: Optional[str] = None,
        updated_at: Optional[datetime] = None,
        expected_token: Optional[str] = None,
    ) -> Optional[BatchCounts]:
        when = _naive_utc(updated_at or datetime.now(timezone.utc))
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    if expected_token is not None:
                        sql = (
                            f"UPDATE {BATCH_GENES_TABLE} g "
                            f"JOIN {BATCHES_TABLE} b ON b.batch_id = g.batch_id "
                            "SET g.status = %s, g.error = %s, g.updated_at = %s "
                            "WHERE g.batch_id = %s AND g.position = %s "
                            "AND b.worker_owner = %s AND b.worker_expires_at >= UTC_TIMESTAMP(6) "
                            "AND b.status != 'cancelled'"
                        )
                        await cursor.execute(
                            sql,
                            (status, _clip(error), when, batch_id, position, expected_token),
                        )
                    else:
                        sql = (
                            f"UPDATE {BATCH_GENES_TABLE} g "
                            f"JOIN {BATCHES_TABLE} b ON b.batch_id = g.batch_id "
                            "SET g.status = %s, g.error = %s, g.updated_at = %s "
                            "WHERE g.batch_id = %s AND g.position = %s "
                            "AND b.status != 'cancelled'"
                        )
                        await cursor.execute(
                            sql,
                            (status, _clip(error), when, batch_id, position),
                        )
                    if cursor.rowcount == 0:
                        await conn.rollback()
                        return None
                    counts = await self._recompute_batch_counts(cursor, batch_id)
                await conn.commit()
                return counts
            except BaseException:
                await conn.rollback()
                raise

    async def update_batch_status(
        self,
        batch_id: str,
        status: BatchStatus,
        *,
        error: Optional[str] = None,
        expected_token: Optional[str] = None,
    ) -> Optional[BatchRecord]:
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    if expected_token is not None:
                        sql = (
                            f"UPDATE {BATCHES_TABLE} SET status = %s, error = %s "
                            "WHERE batch_id = %s AND status != 'cancelled' "
                            "AND worker_owner = %s AND worker_expires_at >= UTC_TIMESTAMP(6)"
                        )
                        await cursor.execute(
                            sql,
                            (status, _clip(error), batch_id, expected_token),
                        )
                    else:
                        sql = (
                            f"UPDATE {BATCHES_TABLE} SET status = %s, error = %s "
                            "WHERE batch_id = %s AND status != 'cancelled'"
                        )
                        await cursor.execute(
                            sql,
                            (status, _clip(error), batch_id),
                        )
                    if cursor.rowcount == 0:
                        await conn.rollback()
                        return None
                    await self._recompute_batch_counts(cursor, batch_id)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return await self.get_batch(batch_id)

    async def cancel_batch(
        self, batch_id: str, *, updated_at: Optional[datetime] = None
    ) -> Optional[BatchRecord]:
        when = _naive_utc(updated_at or datetime.now(timezone.utc))
        async with self._pool.acquire() as conn:
            await conn.begin()
            try:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        f"SELECT status FROM {BATCHES_TABLE} WHERE batch_id = %s FOR UPDATE",
                        (batch_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        await conn.rollback()
                        return None
                    if row[0] not in ("done", "failed", "cancelled"):
                        await cursor.execute(
                            f"UPDATE {BATCH_GENES_TABLE} "
                            "SET status = 'failed', error = 'cancelled', updated_at = %s "
                            "WHERE batch_id = %s AND status IN ('queued', 'running')",
                            (when, batch_id),
                        )
                        await self._recompute_batch_counts(cursor, batch_id)
                        await cursor.execute(
                            f"UPDATE {BATCHES_TABLE} SET status = 'cancelled' WHERE batch_id = %s",
                            (batch_id,),
                        )
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
        return await self.get_batch(batch_id)
