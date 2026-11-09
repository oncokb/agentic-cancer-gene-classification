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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Protocol, Tuple

from src.expression_context.literature_gate import stored_verdict_surfaced
from src.expression_context.models import HistoryEntry, StoredPair

logger = logging.getLogger(__name__)

TABLE_NAME = "expression_context_results"

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
    INDEX idx_expression_context_computed (literature_gate, computed_at)
)
"""

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

    async def get_pairs(
        self, hugo_symbol: str, release_id: str, literature_gate: str
    ) -> Dict[str, StoredPair]: ...

    async def upsert_pairs(self, pairs: List[StoredPair]) -> None: ...

    async def latest_pairs(self, hugo_symbol: str, literature_gate: str) -> List[StoredPair]: ...

    async def list_history(
        self, literature_gate: str, limit: int, cursor: Optional[Cursor]
    ) -> List[HistoryEntry]: ...


def _history_sort_key(entry: HistoryEntry) -> Cursor:
    return (entry.computed_at, entry.hugo_symbol, entry.release_id)


class InMemoryExpressionContextStore:
    """Same semantics as the MySQL store; used by tests and local runs."""

    def __init__(self) -> None:
        self.rows: Dict[Tuple[str, str, str, str], StoredPair] = {}
        self.schema_created = False

    async def ensure_schema(self) -> None:
        self.schema_created = True

    async def close(self) -> None:
        pass

    async def get_pairs(self, hugo_symbol, release_id, literature_gate):
        return {
            row.oncotree_code: row
            for (symbol, _code, release, gate), row in self.rows.items()
            if symbol == hugo_symbol and release == release_id and gate == literature_gate
        }

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

    async def list_history(self, literature_gate, limit, cursor):
        groups: Dict[Tuple[str, str], List[StoredPair]] = {}
        for row in self.rows.values():
            if row.literature_gate == literature_gate:
                groups.setdefault((row.hugo_symbol, row.release_id), []).append(row)
        entries = [
            HistoryEntry(
                hugo_symbol=symbol,
                release_id=release_id,
                literature_gate=literature_gate,
                computed_at=max(row.computed_at for row in rows),
                pair_count=len(rows),
                surfaced_count=sum(
                    1
                    for row in rows
                    if stored_verdict_surfaced(row.literature_gate, row.evidence)
                ),
            )
            for (symbol, release_id), rows in groups.items()
        ]
        entries.sort(key=_history_sort_key, reverse=True)
        if cursor is not None:
            entries = [entry for entry in entries if _history_sort_key(entry) < cursor]
        return entries[:limit]


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

    async def get_pairs(self, hugo_symbol, release_id, literature_gate):
        async with self._pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    f"SELECT {_COLUMNS} FROM {TABLE_NAME} "
                    "WHERE hugo_symbol = %s AND release_id = %s AND literature_gate = %s",
                    (hugo_symbol, release_id, literature_gate),
                )
                rows = await cursor.fetchall()
        pairs = [self._row_to_pair(row) for row in rows]
        return {pair.oncotree_code: pair for pair in pairs}

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

    async def list_history(self, literature_gate, limit, cursor):
        params: List[Any] = [literature_gate]
        having = ""
        if cursor is not None:
            when, symbol, release_id = cursor
            having = (
                "HAVING (last_computed_at < %s) "
                "OR (last_computed_at = %s AND hugo_symbol < %s) "
                "OR (last_computed_at = %s AND hugo_symbol = %s AND release_id < %s) "
            )
            naive = _naive_utc(when)
            params += [naive, naive, symbol, naive, symbol, release_id]
        params.append(limit)
        async with self._pool.acquire() as conn:
            async with conn.cursor() as db_cursor:
                await db_cursor.execute(
                    "SELECT hugo_symbol, release_id, MAX(computed_at) AS last_computed_at, "
                    "COUNT(*) AS pair_count "
                    f"FROM {TABLE_NAME} WHERE literature_gate = %s "
                    "GROUP BY hugo_symbol, release_id "
                    f"{having}"
                    "ORDER BY last_computed_at DESC, hugo_symbol DESC, release_id DESC "
                    "LIMIT %s",
                    params,
                )
                rows = await db_cursor.fetchall()
                surfaced_counts: Dict[Tuple[str, str], int] = {}
                if rows:
                    # Bounded by the page: at most `limit` (API max 100) groups,
                    # one row per cancer type within each. The v2 evidence rule
                    # is applied in Python so the stores cannot drift.
                    keys = [(symbol, release_id) for symbol, release_id, *_ in rows]
                    placeholders = ", ".join(["(%s, %s)"] * len(keys))
                    evidence_params: List[Any] = [literature_gate]
                    for key in keys:
                        evidence_params.extend(key)
                    await db_cursor.execute(
                        "SELECT hugo_symbol, release_id, evidence_json "
                        f"FROM {TABLE_NAME} WHERE literature_gate = %s "
                        f"AND (hugo_symbol, release_id) IN ({placeholders})",
                        evidence_params,
                    )
                    for symbol, release_id, evidence_json in await db_cursor.fetchall():
                        evidence = (
                            json.loads(evidence_json)
                            if isinstance(evidence_json, (str, bytes))
                            else evidence_json
                        )
                        if stored_verdict_surfaced(literature_gate, evidence):
                            key = (symbol, release_id)
                            surfaced_counts[key] = surfaced_counts.get(key, 0) + 1
        return [
            HistoryEntry(
                hugo_symbol=symbol,
                release_id=release_id,
                literature_gate=literature_gate,
                computed_at=_utc(computed_at),
                pair_count=int(pair_count),
                surfaced_count=surfaced_counts.get((symbol, release_id), 0),
            )
            for symbol, release_id, computed_at, pair_count in rows
        ]
