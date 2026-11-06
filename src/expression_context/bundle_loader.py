"""Discover, download, verify and import statistics bundles; switch the active one.

``BundleLoader.load`` imports one release into the module's tables:

1. ``manifest.json`` is validated (schema_version, required bundle fields, tag
   consistency: ``expression-stats-<release_id>-pubmed-<YYYYMMDD>``).
2. ``genes.jsonl.gz`` is streamed once: every chunk updates the SHA-256 and
   byte count and is inflated incrementally (bounded output per step, so a
   decompression bomb can't exhaust memory); each complete line is validated
   as a gene document of the same release and inserted in batches.
3. Only after the declared ``size_bytes``/``sha256``/``gene_count`` all check
   out is the bundle marked ``ready``. On any failure its rows are deleted.

``BundleRefresher.refresh_once`` decides whether a GitHub check is due (shared
bookkeeping in ``expression_context_state``, so the whole cluster makes about
one discovery call per ``refresh_interval``), takes the ONE single-loader
lock (a lease row in MySQL, renewed while loading; losing it aborts the load
and fences activation), loads a newer bundle if there is one, then switches
the active pointer atomically. Bundles that leave {active, previous} are
retired and deleted after ``bundle_prune_grace``. A failed refresh never
touches the active bundle.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import random
import re
import time
import uuid
import zlib
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Set

from pydantic import ValidationError

from src.expression_context.bundle_store import (
    BundleRecord,
    BundleStore,
    GeneRow,
    LoaderLockNotHeld,
)
from src.expression_context.github_releases import (
    BundleRelease,
    DiscoveryIncomplete,
    RateLimited,
    ReleaseAsset,
    ReleasesClient,
    ReleasesError,
)
from src.expression_context.models import (
    SUPPORTED_SCHEMA_VERSION,
    SYMBOL_PATTERN,
    BundleManifest,
    GeneArtifact,
)
from src.expression_context.settings import BUNDLE_TAG_PREFIX, ExpressionContextSettings

logger = logging.getLogger(__name__)

MANIFEST_ASSET = "manifest.json"
GENES_ASSET = "genes.jsonl.gz"
ALIASES_ASSET = "aliases.json"

_SYMBOL = re.compile(SYMBOL_PATTERN)
_RELEASE_ID = re.compile(r"[A-Za-z0-9._-]{1,128}\Z")
_INFLATE_STEP = 1 << 20  # max bytes inflated per zlib call


class BundleError(RuntimeError):
    """A bundle could not be loaded."""


class BundleContractError(BundleError):
    """The bundle violates the release bundle contract (retry only after a full interval)."""


class LoaderLockLost(BundleError):
    """This pod's single-loader lock expired or couldn't be renewed mid-import;
    another pod may be loading."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def check_schema_version(payload: Any, what: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise BundleContractError(f"{what}: expected a JSON object")
    version = payload.get("schema_version")
    if version is None or str(version) != SUPPORTED_SCHEMA_VERSION:
        raise BundleContractError(
            f"{what}: unsupported schema_version {version!r} "
            f"(supported: {SUPPORTED_SCHEMA_VERSION!r})"
        )
    return payload


def expected_bundle_tag(manifest: BundleManifest) -> str:
    return (
        f"{BUNDLE_TAG_PREFIX}{manifest.release_id}-pubmed-"
        f"{manifest.pubmed_snapshot_date.strftime('%Y%m%d')}"
    )


def parse_bundle_manifest(payload: Any, release_tag: str, max_bundle_bytes: int) -> BundleManifest:
    check_schema_version(payload, MANIFEST_ASSET)
    try:
        manifest = BundleManifest.model_validate(payload)
    except ValidationError as exc:
        fields = ", ".join(sorted({".".join(map(str, e["loc"])) for e in exc.errors()}))
        raise BundleContractError(f"{MANIFEST_ASSET}: invalid field(s): {fields}") from exc
    if not _RELEASE_ID.fullmatch(manifest.release_id):
        raise BundleContractError(f"{MANIFEST_ASSET}: malformed release_id {manifest.release_id!r}")
    if manifest.bundle_tag != release_tag:
        raise BundleContractError(
            f"{MANIFEST_ASSET}: bundle_tag {manifest.bundle_tag!r} != release tag {release_tag!r}"
        )
    if manifest.bundle_tag != expected_bundle_tag(manifest):
        raise BundleContractError(
            f"{MANIFEST_ASSET}: bundle_tag {manifest.bundle_tag!r} does not match release_id "
            f"and pubmed_snapshot_date (expected {expected_bundle_tag(manifest)!r})"
        )
    if manifest.size_bytes > max_bundle_bytes:
        raise BundleContractError(
            f"{GENES_ASSET} is {manifest.size_bytes} bytes (limit {max_bundle_bytes})"
        )
    return manifest


def parse_aliases(payload: Any) -> Dict[str, str]:
    """``aliases.json`` (required): a JSON object of alias -> canonical symbol
    strings. Structural problems reject the bundle; entries that aren't valid
    gene symbols, or map a symbol to itself, are skipped."""
    if not isinstance(payload, dict):
        raise BundleContractError(f"{ALIASES_ASSET}: expected a JSON object of alias -> symbol")
    aliases: Dict[str, str] = {}
    skipped = 0
    for alias, symbol in payload.items():
        if not isinstance(alias, str) or not isinstance(symbol, str):
            raise BundleContractError(
                f"{ALIASES_ASSET}: entry {alias!r} must map a string alias to a string symbol"
            )
        alias, symbol = alias.strip().upper(), symbol.strip().upper()
        if alias != symbol and _SYMBOL.fullmatch(alias) and _SYMBOL.fullmatch(symbol):
            aliases[alias] = symbol
        else:
            skipped += 1
    if skipped:
        logger.info("%s: skipped %d alias entries that aren't gene symbols", ALIASES_ASSET, skipped)
    return aliases


class GzipLineReader:
    """Incremental gzip (multi-member) → lines, with bounded memory."""

    def __init__(self, max_line_bytes: int) -> None:
        self._inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
        self._buffer = bytearray()
        self._max_line_bytes = max_line_bytes
        self._started = False

    def _take_lines(self) -> List[bytes]:
        lines = []
        while True:
            end = self._buffer.find(b"\n")
            if end < 0:
                break
            lines.append(bytes(self._buffer[:end]))
            del self._buffer[: end + 1]
        if len(self._buffer) > self._max_line_bytes:
            raise BundleContractError(f"{GENES_ASSET}: a line exceeds {self._max_line_bytes} bytes")
        return lines

    def feed(self, chunk: bytes) -> List[bytes]:
        lines: List[bytes] = []
        data = chunk
        try:
            while data:
                self._started = True
                if self._inflater.eof:
                    # Next member of a multi-member gzip stream.
                    self._inflater = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
                self._buffer += self._inflater.decompress(data, _INFLATE_STEP)
                lines.extend(self._take_lines())
                data = self._inflater.unconsumed_tail or (
                    self._inflater.unused_data if self._inflater.eof else b""
                )
        except zlib.error as exc:
            raise BundleContractError(f"{GENES_ASSET}: corrupt gzip data ({exc})") from exc
        return lines

    def finish(self) -> List[bytes]:
        if not self._started or not self._inflater.eof:
            raise BundleContractError(f"{GENES_ASSET}: truncated gzip stream")
        tail = bytes(self._buffer)
        self._buffer.clear()
        return [tail] if tail.strip() else []


class BundleLoader:
    def __init__(
        self,
        settings: ExpressionContextSettings,
        releases: ReleasesClient,
        bundles: BundleStore,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._releases = releases
        self._bundles = bundles
        self._clock = clock
        self._token = ""

    async def load(
        self,
        release: BundleRelease,
        *,
        lock_token: str,
        should_abort: Callable[[], bool] = lambda: False,
    ) -> BundleRecord:
        """Import ``release``. Every write carries ``lock_token`` and is refused
        by the store unless that token still holds the single-loader lease."""
        for name in (MANIFEST_ASSET, GENES_ASSET, ALIASES_ASSET):
            if name not in release.assets:
                raise BundleContractError(f"release {release.tag} has no {name} asset")
        manifest = parse_bundle_manifest(
            await self._releases.fetch_json_asset(
                release.assets[MANIFEST_ASSET], max_bytes=self._settings.max_manifest_bytes
            ),
            release.tag,
            self._settings.max_bundle_bytes,
        )
        genes_asset = release.assets[GENES_ASSET]
        if genes_asset.size >= 0 and genes_asset.size != manifest.size_bytes:
            raise BundleContractError(
                f"{GENES_ASSET} asset is {genes_asset.size} bytes but the manifest declares "
                f"{manifest.size_bytes}"
            )
        aliases = parse_aliases(
            await self._releases.fetch_json_asset(
                release.assets[ALIASES_ASSET], max_bytes=self._settings.max_aliases_bytes
            )
        )

        existing = await self._bundles.get_bundle(release.tag)
        if existing is not None and existing.status == "ready" and existing.sha256 == manifest.sha256:
            return existing  # already imported intact (e.g. it was the previous bundle)

        tag = release.tag
        if should_abort():
            raise LoaderLockLost("single-loader lock lost before the import started")
        self._token = lock_token
        await self._bundles.begin_load(
            BundleRecord(
                bundle_tag=tag,
                release_id=manifest.release_id,
                schema_version=manifest.schema_version,
                method_version=manifest.method_version,
                pubmed_snapshot_date=manifest.pubmed_snapshot_date,
                gene_count=manifest.gene_count,
                sha256=manifest.sha256,
                size_bytes=manifest.size_bytes,
                manifest=manifest.model_dump(mode="json"),
                status="loading",
                load_started_at=self._clock(),
                published_at=release.published_at,
                release_url=release.html_url,
            ),
            lock_token,
        )
        try:
            async with asyncio.timeout(self._settings.download_timeout_seconds):
                symbols = await self._import_genes(tag, manifest, genes_asset, should_abort)
            await self._import_aliases(tag, aliases, symbols)
            if should_abort():
                raise LoaderLockLost("single-loader lock lost before finishing")
            await self._bundles.finish_load(tag, len(symbols), self._clock(), lock_token)
        except LoaderLockNotHeld as exc:
            # The store refused a write: our lease is gone, and the rows for
            # this tag may now be another loader's. Touch nothing.
            raise LoaderLockLost("single-loader lock lost: the store refused a write") from exc
        except BaseException as exc:
            reason = "timed out" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
            # After losing the lock another pod may be importing this tag:
            # deleting "our" rows would delete theirs. Leave them; the next
            # owner's begin_load() resets the tag.
            if not should_abort():
                try:
                    await self._bundles.fail_load(tag, reason, self._clock(), lock_token)
                except LoaderLockNotHeld:
                    logger.warning("Lost the loader lock; leaving bundle %s rows to its new owner", tag)
                except Exception:
                    logger.exception("Could not clean up failed expression-context bundle %s", tag)
            if isinstance(exc, TimeoutError):
                raise BundleError(f"{GENES_ASSET} import timed out") from exc
            raise
        record = await self._bundles.get_bundle(tag)
        assert record is not None
        return record

    async def _import_genes(
        self,
        tag: str,
        manifest: BundleManifest,
        asset: ReleaseAsset,
        should_abort: Callable[[], bool],
    ) -> Set[str]:
        reader = GzipLineReader(self._settings.max_line_bytes)
        digest = hashlib.sha256()
        received = 0
        line_no = 0
        symbols: Set[str] = set()
        batch: List[GeneRow] = []

        async def accept(lines: List[bytes]) -> None:
            nonlocal line_no, batch
            for line in lines:
                line_no += 1
                if not line.strip():
                    continue
                batch.append(self._gene_row(line.rstrip(b"\r"), manifest, symbols, line_no))
                if len(batch) >= self._settings.load_batch_size:
                    await self._bundles.add_genes(tag, batch, self._token)
                    batch = []
                    if should_abort():
                        raise LoaderLockLost("single-loader lock lost during import")

        async with self._releases.stream_asset(asset) as chunks:
            async for chunk in chunks:
                received += len(chunk)
                if received > manifest.size_bytes:
                    raise BundleContractError(
                        f"{GENES_ASSET} is larger than the declared {manifest.size_bytes} bytes"
                    )
                digest.update(chunk)
                await accept(reader.feed(chunk))
        if received != manifest.size_bytes:
            raise BundleContractError(
                f"{GENES_ASSET} is {received} bytes but the manifest declares {manifest.size_bytes}"
            )
        if digest.hexdigest() != manifest.sha256:
            raise BundleContractError(f"{GENES_ASSET} SHA-256 does not match the manifest")
        await accept(reader.finish())
        if batch:
            await self._bundles.add_genes(tag, batch, self._token)
        if len(symbols) != manifest.gene_count:
            raise BundleContractError(
                f"{GENES_ASSET} has {len(symbols)} genes but the manifest declares "
                f"{manifest.gene_count}"
            )
        return symbols

    @staticmethod
    def _gene_row(
        line: bytes, manifest: BundleManifest, symbols: Set[str], line_no: int
    ) -> GeneRow:
        where = f"{GENES_ASSET} line {line_no}"
        try:
            payload = json.loads(line)
        except ValueError as exc:
            raise BundleContractError(f"{where}: not valid JSON") from exc
        check_schema_version(payload, where)
        try:
            artifact = GeneArtifact.model_validate(payload)
        except ValidationError as exc:
            raise BundleContractError(f"{where}: {exc.error_count()} invalid field(s)") from exc
        if artifact.release_id != manifest.release_id:
            raise BundleContractError(
                f"{where}: release_id {artifact.release_id!r} != manifest {manifest.release_id!r}"
            )
        symbol = artifact.gene.hugo_symbol.strip().upper()
        if not _SYMBOL.fullmatch(symbol):
            raise BundleContractError(f"{where}: invalid hugo_symbol {artifact.gene.hugo_symbol!r}")
        if symbol in symbols:
            raise BundleContractError(f"{where}: duplicate gene {symbol}")
        symbols.add(symbol)
        return symbol, artifact.gene.entrez_gene_id, line

    async def _import_aliases(self, tag: str, aliases: Dict[str, str], symbols: Set[str]) -> None:
        rows = [
            (alias, symbol)
            for alias, symbol in sorted(aliases.items())
            if symbol in symbols and alias not in symbols
        ]
        size = self._settings.load_batch_size * 4
        for start in range(0, len(rows), size):
            await self._bundles.add_aliases(tag, rows[start : start + size], self._token)


class BundleRefresher:
    """Keeps the cluster's active bundle current (see module docstring)."""

    def __init__(
        self,
        settings: ExpressionContextSettings,
        *,
        bundles: BundleStore,
        releases: ReleasesClient,
        on_activated: Optional[Callable[[], None]] = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self._settings = settings
        self._bundles = bundles
        self._releases = releases
        self._loader = BundleLoader(settings, releases, bundles, clock)
        self._on_activated = on_activated
        self._clock = clock
        self._lock_lost = False

    @property
    def wake_seconds(self) -> float:
        """How often the background loop asks whether a check is due."""
        return max(
            60.0,
            min(self._settings.refresh_error_retry_seconds, self._settings.refresh_interval_seconds / 12),
        )

    def _due(self, state, now: datetime) -> bool:
        if state.next_check_after is not None and now < state.next_check_after:
            return False
        if state.last_checked_at is None:
            return True
        # With nothing loaded yet, look again sooner than the full interval.
        # No early-check slack: the API budget assumes exactly these cadences.
        interval = self._settings.refresh_interval_seconds
        if state.active_bundle_tag is None:
            interval = self._settings.min_check_period_seconds
        return (now - state.last_checked_at).total_seconds() >= interval

    @asynccontextmanager
    async def _single_loader(self) -> AsyncIterator[Optional[str]]:
        """The ONE loader lock: a lease row in MySQL (the store the loader writes
        anyway), held and renewed for the whole refresh. Yields the owner token,
        or None if another pod holds it or the store can't be reached."""
        token = uuid.uuid4().hex
        ttl = self._settings.bundle_lock_ttl_seconds
        try:
            acquired = await self._bundles.acquire_loader_lock(token, ttl)
        except Exception as exc:
            logger.warning("Expression-context loader lock unavailable: %s", exc)
            acquired = False
        if not acquired:
            yield None
            return
        self._lock_lost = False
        heartbeat = asyncio.create_task(self._heartbeat(token, ttl))
        try:
            yield token
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            try:
                await self._bundles.release_loader_lock(token)
            except Exception as exc:  # it expires on its own
                logger.warning("Expression-context loader lock release failed: %s", exc)

    async def _heartbeat(self, token: str, ttl: float) -> None:
        while True:
            await asyncio.sleep(max(0.01, ttl / 3))
            try:
                held = await self._bundles.renew_loader_lock(token, ttl)
            except Exception as exc:
                logger.warning("Expression-context loader lock renewal failed (%s); aborting", exc)
                held = False
            if not held:
                self._lock_lost = True
                return

    async def _record_error(self, now: datetime, message: str, retry_at: datetime) -> None:
        logger.warning("Expression-context bundle refresh failed: %s", message)
        await self._bundles.mark_error(now, message, retry_at)

    async def _prune(self, token: str) -> List[str]:
        """Fenced, and computed by the store from the current pointers and the
        database clock (no snapshot, no pod clock)."""
        if self._lock_lost:
            return []
        try:
            return await self._bundles.prune(token, self._settings.bundle_prune_grace_seconds)
        except LoaderLockNotHeld:
            self._lock_lost = True
            return []

    async def refresh_once(self) -> str:
        """One refresh attempt; returns an outcome label (also sent as a metric tag)."""
        outcome = await self._refresh_once()
        _metric("expression_context.bundle.refresh", tags=[f"outcome:{outcome}"])
        return outcome

    async def _refresh_once(self) -> str:
        now = self._clock()
        pinned = self._settings.pinned_bundle_tag
        state = await self._bundles.get_state()
        if pinned and state.active_bundle_tag == pinned:
            return "pinned_active"  # nothing to do, and no GitHub call
        if not self._due(state, now):
            return "not_due"
        async with self._single_loader() as token:
            if token is None:
                return "locked"
            state = await self._bundles.get_state()  # another pod may have just finished
            if pinned and state.active_bundle_tag == pinned:
                return "pinned_active"
            if not self._due(state, now):
                return "not_due"
            interval_retry = now + timedelta(seconds=self._settings.refresh_interval_seconds)
            error_retry = now + timedelta(seconds=self._settings.refresh_error_retry_seconds)
            try:
                release = await (
                    self._releases.bundle_by_tag(pinned) if pinned else self._releases.latest_bundle()
                )
            except RateLimited as exc:
                await self._record_error(now, f"GitHub rate limit: {exc}", max(exc.retry_at, now))
                return "rate_limited"
            except DiscoveryIncomplete as exc:
                # Keep the active bundle; activating an older one would be a guess.
                await self._record_error(now, f"GitHub release discovery incomplete: {exc}", interval_retry)
                return "discovery_incomplete"
            except ReleasesError as exc:
                await self._record_error(now, f"GitHub release lookup failed: {exc}", error_retry)
                return "error"
            if release is None:
                if pinned:
                    await self._record_error(
                        now, f"pinned bundle tag {pinned!r} not found", error_retry
                    )
                    return "error"
                await self._bundles.mark_checked(now)
                return "no_bundle"
            if release.tag == state.active_bundle_tag:
                await self._prune(token)
                await self._bundles.mark_checked(now)
                return "up_to_date"
            return await self._load_and_activate(release, token, now, error_retry)

    async def _load_and_activate(
        self, release: BundleRelease, token: str, now: datetime, error_retry: datetime
    ) -> str:
        started = time.monotonic()
        try:
            record = await self._loader.load(
                release, lock_token=token, should_abort=lambda: self._lock_lost
            )
        except RateLimited as exc:
            await self._record_error(now, f"bundle {release.tag}: {exc}", max(exc.retry_at, now))
            return "rate_limited"
        except BundleContractError as exc:
            # A bad bundle won't fix itself: don't re-download before a full interval.
            retry = now + timedelta(seconds=self._settings.refresh_interval_seconds)
            await self._record_error(now, f"bundle {release.tag} rejected: {exc}", retry)
            return "load_failed"
        except Exception as exc:  # transient: network, MySQL, lost lock, timeout
            await self._record_error(now, f"loading bundle {release.tag} failed: {exc}", error_retry)
            return "load_failed"
        if self._lock_lost or not await self._bundles.activate(
            record.bundle_tag, self._clock(), lock_token=token
        ):
            await self._record_error(
                now, f"could not activate bundle {release.tag}: single-loader lock lost", error_retry
            )
            return "load_failed"
        pruned = await self._prune(token)
        await self._bundles.mark_checked(now)
        if self._on_activated is not None:
            self._on_activated()
        elapsed = time.monotonic() - started
        _metric("expression_context.bundle.load_seconds", value=elapsed, kind="distribution")
        logger.info(
            "Expression-context bundle %s active (release %s, %d genes, %.1fs; pruned %s)",
            record.bundle_tag, record.release_id, record.loaded_rows, elapsed, pruned or "none",
        )
        return "loaded"

    async def run_forever(self) -> None:
        """Background loop: check now, then every ``wake_seconds`` (jittered)."""
        while True:
            try:
                await self.refresh_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Expression-context bundle refresh crashed")
                _metric("expression_context.bundle.refresh", tags=["outcome:crashed"])
            await asyncio.sleep(self.wake_seconds * random.uniform(0.9, 1.1))


def _metric(name: str, *, value: float = 1, tags: Optional[List[str]] = None, kind: str = "increment") -> None:
    """Best-effort DogStatsD metric through ACGC's public observability helpers."""
    try:
        from src import observability

        if kind == "distribution":
            observability.distribution(name, value, tags=tags)
        else:
            observability.increment(name, int(value), tags=tags)
    except Exception:  # metrics must never break a refresh
        logger.debug("Expression-context metric %s not sent", name, exc_info=True)

