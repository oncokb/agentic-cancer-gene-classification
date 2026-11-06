"""Module-owned persistence for statistics bundles.

Four tables, all created by this module only when the feature is enabled, with
no foreign keys to (and no columns on) any existing ACGC table:

- ``expression_context_bundles``: one row per bundle the cluster has loaded or
  tried to load (manifest, provenance, status ``loading|ready|failed``).
- ``expression_context_stats``: one row per (bundle_tag, gene) holding the
  gene's JSON document from ``genes.jsonl.gz``, zlib-compressed.
- ``expression_context_aliases``: (bundle_tag, alias) → canonical symbol.
- ``expression_context_state``: a single row (id = 1) holding the *active
  bundle pointer*, the cluster-wide refresh bookkeeping (last GitHub check,
  back-off, last error) and the *single-loader lease* (owner token + expiry,
  set and renewed by compare-and-set on the database clock). Switching
  bundles is one UPDATE of this row, fenced on holding the lease.

A bundle's rows are written while its status is ``loading`` and are never read
until the pointer names it, so readers only ever see a complete bundle. A
bundle that stops being active/previous is marked ``retired_at`` and deleted
only after a grace period, so pods with a cached pointer keep finding it.
"""

from __future__ import annotations

import json
import logging
import time
import zlib
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

BUNDLES_TABLE = "expression_context_bundles"
STATS_TABLE = "expression_context_stats"
ALIASES_TABLE = "expression_context_aliases"
STATE_TABLE = "expression_context_state"
BUNDLE_TABLES = (BUNDLES_TABLE, STATS_TABLE, ALIASES_TABLE, STATE_TABLE)

_DELETE_CHUNK = 5000
_ZLIB_LEVEL = 6

CREATE_BUNDLE_TABLES_SQL = (
    f"""
CREATE TABLE IF NOT EXISTS {BUNDLES_TABLE} (
    bundle_tag VARCHAR(128) NOT NULL PRIMARY KEY,
    release_id VARCHAR(128) NOT NULL,
    schema_version VARCHAR(16) NOT NULL,
    method_version VARCHAR(16) NOT NULL,
    pubmed_snapshot_date DATE NOT NULL,
    published_at DATETIME(6) NULL,
    release_url VARCHAR(512) NULL,
    gene_count INT NOT NULL,
    sha256 CHAR(64) NOT NULL,
    size_bytes BIGINT NOT NULL,
    manifest_json JSON NOT NULL,
    status VARCHAR(16) NOT NULL,
    loaded_rows INT NOT NULL DEFAULT 0,
    error VARCHAR(1000) NULL,
    load_started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6) NULL,
    retired_at DATETIME(6) NULL,
    INDEX idx_expression_context_bundles_release (release_id)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS {STATS_TABLE} (
    bundle_tag VARCHAR(128) NOT NULL,
    hugo_symbol VARCHAR(64) NOT NULL,
    entrez_gene_id INT NULL,
    doc_zlib MEDIUMBLOB NOT NULL,
    PRIMARY KEY (bundle_tag, hugo_symbol)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS {ALIASES_TABLE} (
    bundle_tag VARCHAR(128) NOT NULL,
    alias VARCHAR(64) NOT NULL,
    hugo_symbol VARCHAR(64) NOT NULL,
    PRIMARY KEY (bundle_tag, alias)
)
""",
    f"""
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (
    id TINYINT NOT NULL PRIMARY KEY,
    active_bundle_tag VARCHAR(128) NULL,
    previous_bundle_tag VARCHAR(128) NULL,
    activated_at DATETIME(6) NULL,
    last_checked_at DATETIME(6) NULL,
    next_check_after DATETIME(6) NULL,
    last_error VARCHAR(1000) NULL,
    last_error_at DATETIME(6) NULL,
    loader_owner VARCHAR(64) NULL,
    loader_expires_at DATETIME(6) NULL
)
""",
)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    return None if value is None else _utc(value).replace(tzinfo=None)


def _clip(text: Optional[str], limit: int = 1000) -> Optional[str]:
    return None if text is None else text[:limit]


@dataclass(frozen=True)
class BundleRecord:
    bundle_tag: str
    release_id: str
    schema_version: str
    method_version: str
    pubmed_snapshot_date: date
    gene_count: int
    sha256: str
    size_bytes: int
    manifest: Dict[str, Any]
    status: str  # loading | ready | failed
    load_started_at: datetime
    published_at: Optional[datetime] = None
    release_url: Optional[str] = None
    loaded_rows: int = 0
    error: Optional[str] = None
    finished_at: Optional[datetime] = None
    retired_at: Optional[datetime] = None


@dataclass(frozen=True)
class BundleState:
    active_bundle_tag: Optional[str] = None
    previous_bundle_tag: Optional[str] = None
    activated_at: Optional[datetime] = None
    last_checked_at: Optional[datetime] = None
    next_check_after: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None


GeneRow = Tuple[str, Optional[int], bytes]  # (hugo_symbol, entrez_gene_id, raw JSON line)
AliasRow = Tuple[str, str]  # (alias, hugo_symbol)


class BundleStore(Protocol):
    async def ensure_schema(self) -> None: ...

    async def close(self) -> None: ...

    async def get_state(self) -> BundleState: ...

    async def mark_checked(self, at: datetime) -> None: ...

    async def mark_error(self, at: datetime, error: str, retry_at: Optional[datetime]) -> None: ...

    async def get_bundle(self, bundle_tag: str) -> Optional[BundleRecord]: ...

    async def begin_load(self, record: BundleRecord) -> None: ...

    async def add_genes(self, bundle_tag: str, rows: Sequence[GeneRow]) -> None: ...

    async def add_aliases(self, bundle_tag: str, rows: Sequence[AliasRow]) -> None: ...

    async def finish_load(self, bundle_tag: str, loaded_rows: int, at: datetime) -> None: ...

    async def fail_load(self, bundle_tag: str, error: str, at: datetime) -> None: ...

    async def activate(
        self, bundle_tag: str, at: datetime, lock_token: Optional[str] = None
    ) -> bool: ...

    async def prune(self, keep: Set[str], at: datetime, grace_seconds: float) -> List[str]: ...

    async def acquire_loader_lock(self, token: str, ttl_seconds: float) -> bool: ...

    async def renew_loader_lock(self, token: str, ttl_seconds: float) -> bool: ...

    async def release_loader_lock(self, token: str) -> None: ...

    async def get_gene_doc(self, bundle_tag: str, hugo_symbol: str) -> Optional[bytes]: ...

    async def get_alias(self, bundle_tag: str, alias: str) -> Optional[str]: ...


# --------------------------------------------------------------------------
# In-memory (tests and local runs)
# --------------------------------------------------------------------------


@dataclass
class InMemoryBundleStore:
    """Same semantics as the MySQL store."""

    state: BundleState = field(default_factory=BundleState)
    bundles: Dict[str, BundleRecord] = field(default_factory=dict)
    genes: Dict[Tuple[str, str], Tuple[Optional[int], bytes]] = field(default_factory=dict)
    aliases: Dict[Tuple[str, str], str] = field(default_factory=dict)
    schema_created: bool = False
    gene_batches: List[int] = field(default_factory=list)
    loader_owner: Optional[str] = None
    loader_expires: float = 0.0
    lock_clock: Callable[[], float] = time.monotonic

    async def ensure_schema(self) -> None:
        self.schema_created = True

    async def close(self) -> None:
        pass

    async def get_state(self) -> BundleState:
        return self.state

    async def mark_checked(self, at):
        self.state = replace(
            self.state, last_checked_at=_utc(at), next_check_after=None,
            last_error=None, last_error_at=None,
        )

    async def mark_error(self, at, error, retry_at):
        self.state = replace(
            self.state, last_error=_clip(error), last_error_at=_utc(at),
            next_check_after=_utc(retry_at),
        )

    async def get_bundle(self, bundle_tag):
        return self.bundles.get(bundle_tag)

    def _drop_rows(self, bundle_tag: str) -> None:
        for key in [key for key in self.genes if key[0] == bundle_tag]:
            del self.genes[key]
        for key in [key for key in self.aliases if key[0] == bundle_tag]:
            del self.aliases[key]

    async def begin_load(self, record):
        self._drop_rows(record.bundle_tag)
        self.bundles[record.bundle_tag] = replace(record, status="loading", loaded_rows=0)

    async def add_genes(self, bundle_tag, rows):
        self.gene_batches.append(len(rows))
        for symbol, entrez, doc in rows:
            self.genes[(bundle_tag, symbol)] = (entrez, bytes(doc))

    async def add_aliases(self, bundle_tag, rows):
        for alias, symbol in rows:
            self.aliases[(bundle_tag, alias)] = symbol

    async def finish_load(self, bundle_tag, loaded_rows, at):
        self.bundles[bundle_tag] = replace(
            self.bundles[bundle_tag], status="ready", loaded_rows=loaded_rows,
            finished_at=_utc(at), error=None,
        )

    async def fail_load(self, bundle_tag, error, at):
        self._drop_rows(bundle_tag)
        if bundle_tag in self.bundles:
            self.bundles[bundle_tag] = replace(
                self.bundles[bundle_tag], status="failed", error=_clip(error),
                finished_at=_utc(at), loaded_rows=0,
            )

    def _holds(self, token: Optional[str]) -> bool:
        return token is not None and self.loader_owner == token and self.lock_clock() <= self.loader_expires

    async def activate(self, bundle_tag, at, lock_token=None):
        record = self.bundles.get(bundle_tag)
        if record is None or record.status != "ready":
            return False
        if lock_token is not None and not self._holds(lock_token):
            return False
        previous = self.state.previous_bundle_tag
        if self.state.active_bundle_tag != bundle_tag:
            previous = self.state.active_bundle_tag
        self.state = replace(
            self.state, active_bundle_tag=bundle_tag, previous_bundle_tag=previous,
            activated_at=_utc(at),
        )
        return True

    async def prune(self, keep, at, grace_seconds):
        at = _utc(at)
        removed = []
        for tag, record in sorted(self.bundles.items()):
            if tag in keep:
                if record.retired_at is not None:
                    self.bundles[tag] = replace(record, retired_at=None)
            elif record.retired_at is None:
                self.bundles[tag] = replace(record, retired_at=at)
            elif (at - record.retired_at).total_seconds() >= grace_seconds:
                removed.append(tag)
        for tag in removed:
            self._drop_rows(tag)
            del self.bundles[tag]
        return removed

    async def acquire_loader_lock(self, token, ttl_seconds):
        now = self.lock_clock()
        if self.loader_owner is None or now > self.loader_expires:
            self.loader_owner, self.loader_expires = token, now + ttl_seconds
        return self.loader_owner == token

    async def renew_loader_lock(self, token, ttl_seconds):
        if not self._holds(token):
            return False
        self.loader_expires = self.lock_clock() + ttl_seconds
        return True

    async def release_loader_lock(self, token):
        if self.loader_owner == token:
            self.loader_owner, self.loader_expires = None, 0.0

    async def get_gene_doc(self, bundle_tag, hugo_symbol):
        row = self.genes.get((bundle_tag, hugo_symbol))
        return row[1] if row else None

    async def get_alias(self, bundle_tag, alias):
        return self.aliases.get((bundle_tag, alias))


# --------------------------------------------------------------------------
# MySQL
# --------------------------------------------------------------------------

_BUNDLE_COLUMNS = (
    "bundle_tag, release_id, schema_version, method_version, pubmed_snapshot_date, "
    "published_at, release_url, gene_count, sha256, size_bytes, manifest_json, status, "
    "loaded_rows, error, load_started_at, finished_at, retired_at"
)
# The lease is judged on the database clock, never on pods' clocks.
_LOCK_HELD = "loader_owner = %s AND loader_expires_at >= UTC_TIMESTAMP(6)"


class MySQLBundleStore:
    """Bundle tables on the module's pool (owned by the results store)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def close(self) -> None:
        # The pool belongs to MySQLExpressionContextStore, which closes it.
        pass

    async def ensure_schema(self) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                for ddl in CREATE_BUNDLE_TABLES_SQL:
                    await cursor.execute(ddl)
                await cursor.execute(f"INSERT IGNORE INTO {STATE_TABLE} (id) VALUES (1)")

    async def _execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(sql, params)

    async def get_state(self) -> BundleState:
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT active_bundle_tag, previous_bundle_tag, activated_at, last_checked_at, "
                    f"next_check_after, last_error, last_error_at FROM {STATE_TABLE} WHERE id = 1"
                )
                row = await cursor.fetchone()
        if row is None:
            return BundleState()
        active, previous, activated, checked, next_after, error, error_at = row
        return BundleState(
            active_bundle_tag=active, previous_bundle_tag=previous, activated_at=_utc(activated),
            last_checked_at=_utc(checked), next_check_after=_utc(next_after),
            last_error=error, last_error_at=_utc(error_at),
        )

    async def mark_checked(self, at):
        await self._execute(
            f"UPDATE {STATE_TABLE} SET last_checked_at = %s, next_check_after = NULL, "
            "last_error = NULL, last_error_at = NULL WHERE id = 1",
            (_naive(at),),
        )

    async def mark_error(self, at, error, retry_at):
        await self._execute(
            f"UPDATE {STATE_TABLE} SET last_error = %s, last_error_at = %s, "
            "next_check_after = %s WHERE id = 1",
            (_clip(error), _naive(at), _naive(retry_at)),
        )

    @staticmethod
    def _row_to_record(row: Tuple[Any, ...]) -> BundleRecord:
        (tag, release_id, schema_version, method_version, snapshot, published_at, release_url,
         gene_count, sha256, size_bytes, manifest_json, status, loaded_rows, error,
         started_at, finished_at, retired_at) = row
        manifest = json.loads(manifest_json) if isinstance(manifest_json, (str, bytes)) else manifest_json
        return BundleRecord(
            bundle_tag=tag, release_id=release_id, schema_version=schema_version,
            method_version=method_version, pubmed_snapshot_date=snapshot,
            published_at=_utc(published_at), release_url=release_url, gene_count=int(gene_count),
            sha256=sha256, size_bytes=int(size_bytes), manifest=manifest, status=status,
            loaded_rows=int(loaded_rows), error=error, load_started_at=_utc(started_at),
            finished_at=_utc(finished_at), retired_at=_utc(retired_at),
        )

    async def get_bundle(self, bundle_tag):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_BUNDLE_COLUMNS} FROM {BUNDLES_TABLE} WHERE bundle_tag = %s",
                    (bundle_tag,),
                )
                row = await cursor.fetchone()
        return None if row is None else self._row_to_record(row)

    @staticmethod
    async def _delete_rows(cursor: Any, bundle_tag: str) -> None:
        # Chunked so dropping a ~20k-gene bundle never holds one long lock.
        for table in (STATS_TABLE, ALIASES_TABLE):
            while True:
                await cursor.execute(
                    f"DELETE FROM {table} WHERE bundle_tag = %s LIMIT {_DELETE_CHUNK}",
                    (bundle_tag,),
                )
                if cursor.rowcount < _DELETE_CHUNK:
                    break

    async def begin_load(self, record):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await self._delete_rows(cursor, record.bundle_tag)
                await cursor.execute(
                    f"REPLACE INTO {BUNDLES_TABLE} ({_BUNDLE_COLUMNS}) VALUES "
                    "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'loading', 0, NULL, %s, NULL, NULL)",
                    (
                        record.bundle_tag, record.release_id, record.schema_version,
                        record.method_version, record.pubmed_snapshot_date,
                        _naive(record.published_at), record.release_url, record.gene_count,
                        record.sha256, record.size_bytes, json.dumps(record.manifest),
                        _naive(record.load_started_at),
                    ),
                )

    async def add_genes(self, bundle_tag, rows):
        if not rows:
            return
        # The compressed document travels as hex and is decoded by UNHEX():
        # aiomysql 0.3.x cannot escape ``bytes`` parameters with PyMySQL >= 1.2
        # (it calls the removed escape_bytes_prefixed). One multi-row INSERT
        # per batch keeps it a single round trip.
        params: List[Any] = []
        for symbol, entrez, doc in rows:
            params += [bundle_tag, symbol, entrez, zlib.compress(bytes(doc), _ZLIB_LEVEL).hex()]
        placeholders = ", ".join(["(%s, %s, %s, UNHEX(%s))"] * len(rows))
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"INSERT INTO {STATS_TABLE} (bundle_tag, hugo_symbol, entrez_gene_id, doc_zlib) "
                    f"VALUES {placeholders} ON DUPLICATE KEY UPDATE "
                    "entrez_gene_id = VALUES(entrez_gene_id), doc_zlib = VALUES(doc_zlib)",
                    params,
                )

    async def add_aliases(self, bundle_tag, rows):
        if not rows:
            return
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.executemany(
                    f"INSERT INTO {ALIASES_TABLE} (bundle_tag, alias, hugo_symbol) "
                    "VALUES (%s, %s, %s) ON DUPLICATE KEY UPDATE hugo_symbol = VALUES(hugo_symbol)",
                    [(bundle_tag, alias, symbol) for alias, symbol in rows],
                )

    async def finish_load(self, bundle_tag, loaded_rows, at):
        await self._execute(
            f"UPDATE {BUNDLES_TABLE} SET status = 'ready', loaded_rows = %s, finished_at = %s, "
            "error = NULL WHERE bundle_tag = %s",
            (loaded_rows, _naive(at), bundle_tag),
        )

    async def fail_load(self, bundle_tag, error, at):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await self._delete_rows(cursor, bundle_tag)
                await cursor.execute(
                    f"UPDATE {BUNDLES_TABLE} SET status = 'failed', loaded_rows = 0, error = %s, "
                    "finished_at = %s WHERE bundle_tag = %s",
                    (_clip(error), _naive(at), bundle_tag),
                )

    async def activate(self, bundle_tag, at, lock_token=None):
        """Atomically point the cluster at a ready bundle (one single-table
        UPDATE), fenced on still holding the loader lease when a token is given."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # Single-table UPDATE assignments run left to right, so
                # previous_bundle_tag reads the old active value.
                await cursor.execute(
                    f"UPDATE {STATE_TABLE} SET "
                    "previous_bundle_tag = IF(active_bundle_tag <=> %s, previous_bundle_tag, "
                    "active_bundle_tag), active_bundle_tag = %s, activated_at = %s "
                    f"WHERE id = 1 AND EXISTS (SELECT 1 FROM {BUNDLES_TABLE} "
                    "WHERE bundle_tag = %s AND status = 'ready') "
                    f"AND (%s IS NULL OR ({_LOCK_HELD}))",
                    (bundle_tag, bundle_tag, _naive(at), bundle_tag, lock_token, lock_token),
                )
                # Report success only if the tag is active AND (when fenced) the
                # caller still holds the lease, so a stale owner never reads its
                # no-op UPDATE as success just because the tag was already active.
                await cursor.execute(
                    f"SELECT active_bundle_tag = %s AND (%s IS NULL OR ({_LOCK_HELD})) "
                    f"FROM {STATE_TABLE} WHERE id = 1",
                    (bundle_tag, lock_token, lock_token),
                )
                row = await cursor.fetchone()
        return bool(row and row[0])

    async def prune(self, keep, at, grace_seconds):
        """Mark bundles that left {active, previous} as retired; delete those
        retired for at least ``grace_seconds``. Kept bundles are un-retired."""
        at_naive = _naive(at)
        removed: List[str] = []
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(f"SELECT bundle_tag, retired_at FROM {BUNDLES_TABLE}")
                for tag, retired_at in await cursor.fetchall():
                    if tag in keep:
                        if retired_at is not None:
                            await cursor.execute(
                                f"UPDATE {BUNDLES_TABLE} SET retired_at = NULL WHERE bundle_tag = %s",
                                (tag,),
                            )
                    elif retired_at is None:
                        await cursor.execute(
                            f"UPDATE {BUNDLES_TABLE} SET retired_at = %s WHERE bundle_tag = %s",
                            (at_naive, tag),
                        )
                    elif (at_naive - retired_at).total_seconds() >= grace_seconds:
                        removed.append(tag)
                for tag in sorted(removed):
                    await self._delete_rows(cursor, tag)
                    await cursor.execute(
                        f"DELETE FROM {BUNDLES_TABLE} WHERE bundle_tag = %s", (tag,)
                    )
        return sorted(removed)

    async def _lock_holder(self, cursor: Any, token: str) -> bool:
        await cursor.execute(
            f"SELECT COUNT(*) FROM {STATE_TABLE} WHERE id = 1 AND {_LOCK_HELD}", (token,)
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def acquire_loader_lock(self, token, ttl_seconds):
        """Compare-and-set: take the lease if it's free or expired."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"UPDATE {STATE_TABLE} SET loader_owner = %s, "
                    "loader_expires_at = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND "
                    "WHERE id = 1 AND (loader_owner IS NULL OR loader_expires_at < UTC_TIMESTAMP(6))",
                    (token, int(ttl_seconds * 1_000_000)),
                )
                return await self._lock_holder(cursor, token)

    async def renew_loader_lock(self, token, ttl_seconds):
        """Extend the lease iff ``token`` still holds it (unexpired)."""
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"UPDATE {STATE_TABLE} SET "
                    "loader_expires_at = UTC_TIMESTAMP(6) + INTERVAL %s MICROSECOND "
                    f"WHERE id = 1 AND {_LOCK_HELD}",
                    (int(ttl_seconds * 1_000_000), token),
                )
                return await self._lock_holder(cursor, token)

    async def release_loader_lock(self, token):
        await self._execute(
            f"UPDATE {STATE_TABLE} SET loader_owner = NULL, loader_expires_at = NULL "
            "WHERE id = 1 AND loader_owner = %s",
            (token,),
        )

    async def get_gene_doc(self, bundle_tag, hugo_symbol):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT doc_zlib FROM {STATS_TABLE} WHERE bundle_tag = %s AND hugo_symbol = %s",
                    (bundle_tag, hugo_symbol),
                )
                row = await cursor.fetchone()
        return None if row is None else zlib.decompress(row[0])

    async def get_alias(self, bundle_tag, alias):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT hugo_symbol FROM {ALIASES_TABLE} WHERE bundle_tag = %s AND alias = %s",
                    (bundle_tag, alias),
                )
                row = await cursor.fetchone()
        return None if row is None else row[0]
