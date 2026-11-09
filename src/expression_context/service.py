"""Joins the precomputed stats gate with the on-demand literature gate.

Statistics come from the active release bundle (``catalog.py``); nothing on
the request path calls out to GitHub.

A (gene, cancer type) pair is *surfaced* iff:

1. it passes the statistics: ``stats_pass``, and not a method-m3 pair whose
   lineage enrichment was blocked by broad expression without a subgroup
   pass (``CancerTypeStats.passes_stats``), and
2. ``literature_precheck_pass`` is not false (stats repo's PubMed-count
   pre-check; null means it was not run, and the literature gate itself is
   authoritative), and
3. the literature gate confirms the SPECIFIC tumor type
   (``literature_gate.verdict_supported``: verdict "supported" AND >=1 PMID
   judged to describe the gene in that tumor type; only those are cited).

Only pairs passing 1 and 2 reach the literature gate; exclusion reason
``precheck`` is recorded only for an explicit ``literature_precheck_pass:
false``. Verdicts are stored per
(hugo_symbol, oncotree_code, release_id, literature_gate) and reused.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Set, Tuple

from src.expression_context.catalog import ActiveBundle, BundleGone, BundleNotLoaded, StatsCatalog
from src.expression_context.lease import VerdictLease, lease_key
from src.expression_context.literature_gate import (
    TUMOR_SPECIFIC_GATE,
    LiteratureGate,
    stored_v2_verdict,
    verdict_supported,
)
from src.expression_context.models import (
    SYMBOL_PATTERN,
    BundleStatus,
    CancerTypeStats,
    GeneArtifact,
    GeneResult,
    GeneStatsSummary,
    GeneStatsView,
    HistoryPage,
    JobPairError,
    JobRecord,
    LiteratureVerdict,
    Manifest,
    PairResult,
    PairStatsView,
    StoredPair,
    TumorContext,
)
from src.expression_context.store import (
    ExpressionContextStore,
    decode_cursor,
    encode_cursor,
)

logger = logging.getLogger(__name__)

_SYMBOL = re.compile(SYMBOL_PATTERN)


class InvalidGene(ValueError):
    pass


class GeneNotFound(LookupError):
    pass


@dataclass
class ResolvedGene:
    query: str
    hugo_symbol: str
    manifest: Manifest
    artifact: GeneArtifact
    bundle: Optional[ActiveBundle] = None

    @property
    def bundle_tag(self) -> Optional[str]:
        return self.bundle.tag if self.bundle else None


def precheck_expires_at(manifest: Manifest) -> Optional[datetime]:
    """``source.precheck_expires_at`` (contract freshness marker), if parseable."""
    raw = manifest.source.get("precheck_expires_at") if manifest.source else None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable source.precheck_expires_at %r; treating as fresh", raw)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def precheck_expired(manifest: Manifest, now: datetime) -> bool:
    expires = precheck_expires_at(manifest)
    return expires is not None and now >= expires


def effective_precheck(stats: CancerTypeStats, expired: bool) -> Optional[bool]:
    """An expired PubMed pre-check is unavailable (None): it never excludes a
    pair; like "not run", the literature gate decides."""
    return None if expired else stats.literature_precheck_pass


def normalize_symbol(gene: str) -> str:
    symbol = (gene or "").strip().upper()
    if not _SYMBOL.fullmatch(symbol):
        raise InvalidGene("gene must be a gene symbol (letters, digits, '.', '_' or '-')")
    return symbol


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStoreLike:  # pragma: no cover - typing aid only
    async def put(self, job: JobRecord) -> None: ...

    async def get(self, job_id: str) -> Optional[JobRecord]: ...


class ExpressionContextService:
    def __init__(
        self,
        *,
        catalog: StatsCatalog,
        gate: LiteratureGate,
        store: ExpressionContextStore,
        jobs: JobStoreLike,
        literature_concurrency: int = 2,
        history_max_limit: int = 100,
        lease: Optional[VerdictLease] = None,
        lease_wait_seconds: float = 300.0,
        lease_poll_seconds: float = 2.0,
        refresher: Optional[Any] = None,
        closers: Sequence[Callable[[], Awaitable[None]]] = (),
        releases_repo: str = "",
        pinned_bundle_tag: Optional[str] = None,
        clock: Callable[[], datetime] = None,
    ) -> None:
        self._clock = clock or _utcnow
        self.releases_repo = releases_repo
        self.pinned_bundle_tag = pinned_bundle_tag
        self.catalog = catalog
        # BundleRefresher (or None in tests that seed bundles directly).
        self.refresher = refresher
        self._closers = list(closers)
        self._refresh_task: Optional[asyncio.Task] = None
        self.gate = gate
        self.store = store
        self.jobs = jobs
        # One bound for the whole process (the service is an app singleton),
        # shared by every job, on concurrent literature-gate evaluations.
        self._gate_slots = asyncio.Semaphore(literature_concurrency)
        self.history_max_limit = history_max_limit
        self._lease = lease or VerdictLease(ttl_seconds=120.0)
        self._lease_wait_seconds = lease_wait_seconds
        self._lease_poll_seconds = lease_poll_seconds
        self._inflight: Dict[Tuple[str, str, str, str], asyncio.Future] = {}
        self._tasks: Set[asyncio.Task] = set()

    # ------------------------------------------------------------------ gene

    async def resolve(self, gene: str) -> ResolvedGene:
        """Canonical symbol first, then the bundle's aliases. Raises GeneNotFound,
        or BundleNotLoaded when no bundle is active yet. If the bundle this pod
        had cached disappears meanwhile, the active one is re-read (never a 404)."""
        symbol = normalize_symbol(gene)
        for attempt in range(2):
            bundle = await self.catalog.active()
            try:
                artifact = await self.catalog.gene(bundle, symbol)
                if artifact is None:
                    target = await self.catalog.alias(bundle, symbol)
                    if target and target != symbol:
                        artifact = await self.catalog.gene(bundle, target)
                break
            except BundleGone:
                if attempt:
                    raise BundleNotLoaded("the active bundle changed while reading it")
        if artifact is None:
            raise GeneNotFound(symbol)
        return ResolvedGene(
            query=gene,
            hugo_symbol=artifact.gene.hugo_symbol.upper(),
            manifest=bundle.manifest,
            artifact=artifact,
            bundle=bundle,
        )

    # ------------------------------------------------------------------ jobs

    async def submit(self, gene: str, owner: Optional[str]) -> JobRecord:
        resolved = await self.resolve(gene)
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            status="queued",
            gene=gene,
            hugo_symbol=resolved.hugo_symbol,
            release_id=resolved.manifest.release_id,
            bundle_tag=resolved.bundle_tag,
            created_at=_utcnow(),
            owner=owner,
        )
        await self.jobs.put(job)
        task = asyncio.create_task(self._run_job(job, resolved))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def _run_job(self, job: JobRecord, resolved: ResolvedGene) -> None:
        try:
            job.status = "running"
            await self.jobs.put(job)
            result, evaluated, reused, errors = await self.evaluate(resolved, job.owner)
        except Exception as exc:
            logger.exception("Expression-context job %s failed", job.job_id)
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:500]
        else:
            job.status = "succeeded"
            job.result = result
            job.literature_evaluated = evaluated
            job.literature_reused = reused
            job.pair_errors = errors
        job.finished_at = _utcnow()
        await self.jobs.put(job)

    async def evaluate(
        self, resolved: ResolvedGene, requested_by: Optional[str]
    ) -> Tuple[GeneResult, int, int, List[JobPairError]]:
        manifest = resolved.manifest
        symbol = resolved.hugo_symbol
        gate_name = self.gate.name
        existing = await self.store.get_pairs(symbol, manifest.release_id, gate_name)
        now = self._clock()
        expired = precheck_expired(manifest, now)

        rows: List[StoredPair] = []
        to_check: List[Tuple[CancerTypeStats, TumorContext]] = []
        errors: List[JobPairError] = []
        reused = 0

        def row(stats: CancerTypeStats, surfaced: bool, reason, evidence=None) -> StoredPair:
            cancer_type = manifest.cancer_type(stats.oncotree_code)
            stored_stats = stats.model_dump()
            # Denormalized display fields, so reads never need the artifact.
            stored_stats["cancer_type_name"] = cancer_type.name if cancer_type else None
            stored_stats["study_id"] = cancer_type.study_id if cancer_type else None
            stored_stats["bundle_tag"] = resolved.bundle_tag
            if resolved.artifact.broadly_expressed is not None:
                stored_stats["gene_broadly_expressed"] = resolved.artifact.broadly_expressed
            if expired:
                stored_stats["precheck_expired"] = True
            return StoredPair(
                hugo_symbol=symbol,
                oncotree_code=stats.oncotree_code,
                release_id=manifest.release_id,
                literature_gate=gate_name,
                surfaced=surfaced,
                exclusion_reason=reason,
                stats=stored_stats,
                evidence=evidence,
                computed_at=now,
                requested_by=requested_by,
            )

        for stats in resolved.artifact.cancer_types:
            if not stats.passes_stats:
                # Includes broad-blocked m3 pairs: visible, never sent on.
                rows.append(row(stats, False, "stats"))
                continue
            if effective_precheck(stats, expired) is False:
                rows.append(row(stats, False, "precheck"))
                continue
            prior = existing.get(stats.oncotree_code)
            if prior is not None and prior.evidence is not None:
                verdict = LiteratureVerdict.model_validate(prior.evidence)
                # Never reuse evidence from another gate, even if it is stored
                # under this gate's key (a legacy row written before the
                # version bump): recompute it instead.
                if verdict.literature_gate == gate_name:
                    rows.append(_verdict_row(row, stats, verdict))
                    reused += 1
                    continue
            cancer_type = manifest.cancer_type(stats.oncotree_code)
            if cancer_type is None or not cancer_type.tumor_type_query.strip():
                errors.append(JobPairError(
                    oncotree_code=stats.oncotree_code,
                    error="cancer type missing from manifest (no tumor_type_query)",
                ))
                continue
            to_check.append((stats, TumorContext(
                oncotree_code=stats.oncotree_code,
                name=cancer_type.name,
                tumor_type_query=cancer_type.tumor_type_query,
            )))

        async def check(stats: CancerTypeStats, tumor: TumorContext):
            try:
                verdict, was_reused = await self._single_flight_verdict(
                    symbol, stats.oncotree_code, manifest.release_id, tumor,
                    lambda verdict: _verdict_row(row, stats, verdict),
                )
                return stats, verdict, was_reused, None
            except Exception as exc:
                logger.warning(
                    "Literature gate %s failed for %s/%s: %s",
                    gate_name, symbol, stats.oncotree_code, exc,
                )
                return stats, None, False, f"{type(exc).__name__}: {exc}"[:300]

        evaluated = 0
        outcomes = await asyncio.gather(*(check(s, t) for s, t in to_check))
        for stats, verdict, was_reused, error in outcomes:
            if verdict is None:
                errors.append(JobPairError(oncotree_code=stats.oncotree_code, error=error or ""))
                continue
            if was_reused:
                reused += 1
            else:
                evaluated += 1
            rows.append(_verdict_row(row, stats, verdict))

        await self.store.upsert_pairs(rows)
        result = self._gene_result(symbol, rows, include_excluded=True)
        return result, evaluated, reused, errors

    # ------------------------------------------------- literature single-flight

    async def _single_flight_verdict(
        self,
        symbol: str,
        oncotree_code: str,
        release_id: str,
        tumor: TumorContext,
        make_row: Callable[[LiteratureVerdict], StoredPair],
    ) -> Tuple[LiteratureVerdict, bool]:
        """(verdict, reused) for one verdict key, computing it at most once.

        In-process: concurrent jobs share one future per key. Cross-pod: a
        Redis lease on the key; non-holders poll the store for the holder's
        verdict (and take over if the holder gives up), falling back to their
        own evaluation only after ``lease_wait_seconds``.
        """
        key = (symbol, oncotree_code, release_id, self.gate.name)
        while (pending := self._inflight.get(key)) is not None:
            try:
                verdict, _ = await asyncio.shield(pending)
                return verdict, True
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if not pending.cancelled() or (current is not None and current.cancelling()):
                    raise  # this waiter itself was cancelled
                # The leader was cancelled (e.g. its job was torn down): the
                # leader has already dropped the key, so the first waiter to
                # get here becomes the new leader and the rest wait on it.
                logger.info("Literature single-flight leader cancelled for %s; taking over", key)
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        # Waiters retrieve the exception; don't warn if there were none.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self._inflight[key] = future
        try:
            outcome = await self._resolve_verdict(key, tumor, make_row)
        except asyncio.CancelledError:
            future.cancel()
            raise
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(outcome)
            return outcome
        finally:
            if self._inflight.get(key) is future:
                del self._inflight[key]

    async def _stored_verdict(self, key: Tuple[str, str, str, str]) -> Optional[LiteratureVerdict]:
        symbol, oncotree_code, release_id, gate_name = key
        prior = (await self.store.get_pairs(symbol, release_id, gate_name)).get(oncotree_code)
        if prior is None or prior.evidence is None:
            return None
        verdict = LiteratureVerdict.model_validate(prior.evidence)
        # Evidence from a different gate is not this gate's verdict, even when
        # it is stored under this gate's key: treat it as absent and evaluate.
        return verdict if verdict.literature_gate == gate_name else None

    async def _resolve_verdict(
        self,
        key: Tuple[str, str, str, str],
        tumor: TumorContext,
        make_row: Callable[[LiteratureVerdict], StoredPair],
    ) -> Tuple[LiteratureVerdict, bool]:
        lkey = lease_key(*key)
        deadline = time.monotonic() + self._lease_wait_seconds
        while True:
            stored = await self._stored_verdict(key)
            if stored is not None:
                return stored, True
            claim = await self._lease.claim(lkey)
            if claim.outcome == "held":
                if time.monotonic() >= deadline:
                    logger.warning(
                        "Gave up waiting for another pod's literature verdict for %s; "
                        "evaluating it here", lkey,
                    )
                    return await self._evaluate_and_store(key, tumor, make_row), False
                await asyncio.sleep(self._lease_poll_seconds)
                continue
            try:
                # Re-check: the previous holder may have stored it just now.
                stored = await self._stored_verdict(key)
                if stored is not None:
                    return stored, True
                verdict = await self._evaluate_and_store(
                    key, tumor, make_row, lease=(lkey, claim.token)
                )
                return verdict, False
            finally:
                if claim.token:
                    await self._lease.release(lkey, claim.token)

    async def _evaluate_and_store(
        self,
        key: Tuple[str, str, str, str],
        tumor: TumorContext,
        make_row: Callable[[LiteratureVerdict], StoredPair],
        lease: Optional[Tuple[str, Optional[str]]] = None,
    ) -> LiteratureVerdict:
        heartbeat = None
        if lease is not None and lease[1]:
            heartbeat = asyncio.create_task(self._heartbeat(*lease))
        try:
            async with self._gate_slots:
                verdict = await self.gate.evaluate(key[0], tumor)
            # Store before the lease is released so waiters find it.
            await self.store.upsert_pairs([make_row(verdict)])
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
        return verdict

    async def _heartbeat(self, lkey: str, token: str) -> None:
        interval = max(self._lease.ttl_seconds / 3, 0.05)
        while True:
            await asyncio.sleep(interval)
            if not await self._lease.renew(lkey, token):
                logger.warning("Lost the literature lease %s while evaluating", lkey)
                return

    # ---------------------------------------------------------------- reads

    async def latest_result(
        self, gene: str, include_excluded: bool
    ) -> GeneResult:
        symbol = normalize_symbol(gene)
        rows = await self.store.latest_pairs(symbol, self.gate.name)
        if not rows:
            canonical = await self._alias_target(symbol)
            if canonical:
                rows = await self.store.latest_pairs(canonical, self.gate.name)
        if not rows:
            raise GeneNotFound(symbol)
        return self._gene_result(rows[0].hugo_symbol, rows, include_excluded)

    async def _alias_target(self, symbol: str) -> Optional[str]:
        # BundleNotLoaded propagates (-> 503): "can't resolve yet" is not a 404.
        for attempt in range(2):
            bundle = await self.catalog.active()
            try:
                return await self.catalog.alias(bundle, symbol)
            except BundleGone:
                if attempt:
                    raise BundleNotLoaded("the active bundle changed while reading it")
        return None  # pragma: no cover

    async def bundle_status(self) -> BundleStatus:
        return await self.catalog.status(
            releases_repo=self.releases_repo, pinned_bundle_tag=self.pinned_bundle_tag
        )

    async def stats_view(self, gene: str) -> GeneStatsView:
        """The active bundle's statistics for every cancer type of a gene, joined
        with any stored literature verdicts for this release (the stats page)."""
        resolved = await self.resolve(gene)
        bundle = resolved.bundle
        assert bundle is not None
        stored = await self.store.get_pairs(
            resolved.hugo_symbol, resolved.manifest.release_id, self.gate.name
        )
        pairs: List[PairStatsView] = []
        summary = GeneStatsSummary()
        expired = precheck_expired(resolved.manifest, self._clock())
        for stats in resolved.artifact.cancer_types:
            cancer_type = resolved.manifest.cancer_type(stats.oncotree_code)
            precheck = {True: "pass", False: "fail", None: "not_run"}[stats.literature_precheck_pass]
            if expired and stats.literature_precheck_pass is not None:
                precheck = "expired"
            passes = stats.passes_stats
            eligible = passes and effective_precheck(stats, expired) is not False
            verdict = None
            prior = stored.get(stats.oncotree_code)
            if (
                eligible
                and self.gate.name == TUMOR_SPECIFIC_GATE
                and prior is not None
                and prior.evidence is not None
            ):
                stored_verdict = LiteratureVerdict.model_validate(prior.evidence)
                if stored_verdict.literature_gate == TUMOR_SPECIFIC_GATE:
                    verdict = stored_verdict.model_copy(
                        update={"supported": verdict_supported(stored_verdict)}
                    )
            if not passes:
                status, reason = "excluded", "stats"
            elif not eligible:
                status, reason = "excluded", "precheck"
            elif verdict is None:
                status, reason = "literature_pending", None
            elif verdict.supported:
                status, reason = "surfaced", None
            else:
                status, reason = "excluded", "literature"
            setattr(summary, status, getattr(summary, status) + 1)
            pairs.append(PairStatsView(
                oncotree_code=stats.oncotree_code,
                cancer_type_name=cancer_type.name if cancer_type else None,
                study_id=cancer_type.study_id if cancer_type else None,
                tumor_type_query=cancer_type.tumor_type_query if cancer_type else None,
                stats=stats,
                stats_pass=passes,
                enrichment=stats.enrichment_label,
                broad_blocked=stats.broad_blocked,
                precheck=precheck,
                literature_eligible=eligible,
                status=status,
                exclusion_reason=reason,
                literature=verdict,
            ))
        pairs.sort(key=lambda pair: (
            not pair.stats_pass,
            pair.stats.q_value is None,
            pair.stats.q_value if pair.stats.q_value is not None else 0.0,
            pair.oncotree_code,
        ))
        return GeneStatsView(
            query=gene,
            hugo_symbol=resolved.hugo_symbol,
            entrez_gene_id=resolved.artifact.gene.entrez_gene_id,
            broadly_expressed=resolved.artifact.broadly_expressed,
            literature_gate=self.gate.name,
            bundle=bundle.provenance(),
            summary=summary,
            pairs=pairs,
        )

    async def history(self, limit: int, cursor: Optional[str]) -> HistoryPage:
        decoded = decode_cursor(cursor) if cursor else None
        limit = max(1, min(limit, self.history_max_limit))
        entries = await self.store.list_history(self.gate.name, limit + 1, decoded)
        page = entries[:limit]
        next_cursor = encode_cursor(page[-1]) if len(entries) > limit and page else None
        return HistoryPage(items=page, next_cursor=next_cursor)

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Start the background bundle refresher (first check runs immediately)."""
        if self.refresher is not None and self._refresh_task is None:
            self._refresh_task = asyncio.create_task(self.refresher.run_forever())

    async def aclose(self) -> None:
        tasks = list(self._tasks)
        if self._refresh_task is not None:
            tasks.append(self._refresh_task)
            self._refresh_task = None
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for close in self._closers:
            try:
                await close()
            except Exception:
                logger.exception("Expression-context shutdown step failed")
        await self._lease.aclose()
        await self.store.close()

    # -------------------------------------------------------------- helpers

    @staticmethod
    def _gene_result(
        symbol: str,
        rows: List[StoredPair],
        include_excluded: bool,
    ) -> GeneResult:
        pairs = [_pair_result(row) for row in rows]
        surfaced = sorted(
            (pair for pair in pairs if pair.surfaced),
            # Undefined q-values (null) sort last.
            key=lambda pair: (
                pair.stats.q_value is None,
                pair.stats.q_value if pair.stats.q_value is not None else 0.0,
                pair.oncotree_code,
            ),
        )
        excluded = sorted(
            (pair for pair in pairs if not pair.surfaced), key=lambda pair: pair.oncotree_code
        )
        broad = [row.stats.get("gene_broadly_expressed") for row in rows]
        return GeneResult(
            hugo_symbol=symbol,
            broadly_expressed=next((value for value in broad if value is not None), None),
            release_id=rows[0].release_id if rows else "",
            literature_gate=rows[0].literature_gate if rows else "",
            computed_at=max((row.computed_at for row in rows), default=_utcnow()),
            surfaced=surfaced,
            excluded=excluded if include_excluded else None,
        )


def _verdict_row(make_row, stats: CancerTypeStats, verdict: LiteratureVerdict) -> StoredPair:
    # Recompute "supported" from the stored fields so a gate can't surface a
    # pair that violates the rule (tumor-specific PMIDs only).
    supported = verdict_supported(verdict)
    verdict = verdict.model_copy(update={"supported": supported})
    return make_row(
        stats,
        supported,
        None if supported else "literature",
        verdict.model_dump(mode="json"),
    )


def _pair_result(row: StoredPair) -> PairResult:
    # Fail closed: only a v2 row whose stored evidence is a valid v2 verdict is
    # returned as literature. A row without evidence, a legacy v1 row, or
    # evidence from another gate is hidden, even if the stored boolean says
    # otherwise.
    verdict = stored_v2_verdict(row.literature_gate, row.evidence)
    surfaced = verdict is not None and verdict_supported(verdict)
    if verdict is not None:
        verdict = verdict.model_copy(update={"supported": surfaced})
    reason = None if surfaced else (row.exclusion_reason or "literature")
    return PairResult(
        oncotree_code=row.oncotree_code,
        cancer_type_name=row.stats.get("cancer_type_name"),
        study_id=row.stats.get("study_id"),
        surfaced=surfaced,
        exclusion_reason=reason,
        stats=CancerTypeStats.model_validate(row.stats),
        literature=verdict,
        computed_at=row.computed_at,
        bundle_tag=row.stats.get("bundle_tag"),
    )

