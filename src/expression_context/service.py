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
from typing import (
    Any,
    AsyncIterator,
    Awaitable,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
)

from src.expression_context.catalog import ActiveBundle, BundleGone, BundleNotLoaded, StatsCatalog
from src.expression_context.csv_export import (
    check_sheet_cancer_names,
    csv_filename,
    format_csv_rows,
    format_golden_set_rows,
    gene_view_csv_rows,
    golden_set_filename,
    golden_set_row_from_view,
    load_sheet_cancer_names,
)
from src.expression_context.lease import VerdictLease, lease_key
from src.expression_context.literature_gate import (
    TUMOR_SPECIFIC_GATE,
    LiteratureGate,
    stored_verdict_surfaced,
    verdict_supported,
)
from src.expression_context.models import (
    SYMBOL_PATTERN,
    BundleProvenance,
    BundleStatus,
    CancerTypeStats,
    GeneArtifact,
    GeneResult,
    GeneStatsSummary,
    GeneStatsView,
    HistoryEntry,
    HistoryPage,
    JobPairError,
    JobRecord,
    JobStatus,
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


class GenesNotChecked(Exception):
    def __init__(self, genes: List[str]):
        super().__init__(f"Genes not yet checked: {', '.join(genes)}")
        self.genes = genes


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

    async def get_job_status(self, job: JobRecord) -> JobStatus:
        if job.status == "succeeded" and job.result is not None:
            try:
                view, stored = await self.gene_view(job.hugo_symbol)
                revalidated = self._gene_result_from_view(
                    view, stored, include_excluded=True
                )
                job = job.model_copy(update={"result": revalidated})
            except GeneNotFound:
                revalidated = job.result.model_copy(update={"surfaced": []})
                job = job.model_copy(update={"result": revalidated})
        return job.public()

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
            stored_stats["method_version"] = manifest.method_version
            if resolved.artifact.gene.entrez_gene_id is not None:
                stored_stats["entrez_gene_id"] = resolved.artifact.gene.entrez_gene_id
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
        view, stored = await self.gene_view(symbol, resolved=resolved)
        result = self._gene_result_from_view(view, stored, include_excluded=True)
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

    def _evaluate_gene_view(
        self,
        resolved: ResolvedGene,
        stored: Mapping[str, StoredPair],
    ) -> GeneStatsView:
        bundle = resolved.bundle
        if bundle is not None:
            if resolved.manifest.release_id != bundle.release_id:
                raise ValueError(
                    f"ResolvedGene manifest release_id ({resolved.manifest.release_id}) "
                    f"does not match bundle release_id ({bundle.release_id})"
                )
            bundle_provenance = bundle.provenance()
        else:
            bundle_provenance = BundleProvenance(
                bundle_tag=resolved.manifest.release_id,
                release_id=resolved.manifest.release_id,
                schema_version=resolved.manifest.schema_version,
                method_version=resolved.manifest.method_version,
                pubmed_snapshot_date=resolved.manifest.pubmed_snapshot_date,
                gene_count=0,
                source=resolved.manifest.source,
                params=resolved.manifest.params,
            )
        active_release_id = resolved.manifest.release_id
        pairs: List[PairStatsView] = []
        summary = GeneStatsSummary()
        expired = precheck_expired(resolved.manifest, self._clock())
        has_stored_result = bool(stored)
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
                and prior.release_id == active_release_id
            ):
                stored_verdict = LiteratureVerdict.model_validate(prior.evidence)
                if stored_verdict.literature_gate == TUMOR_SPECIFIC_GATE:
                    is_surfaced = stored_verdict_surfaced(
                        row_release_id=prior.release_id,
                        active_release_id=active_release_id,
                        literature_gate=prior.literature_gate,
                        evidence=prior.evidence,
                        stats=stats,
                        precheck_is_expired=expired,
                    )
                    verdict = stored_verdict.model_copy(
                        update={"supported": is_surfaced}
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
            pairs.append(
                PairStatsView(
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
                )
            )
        pairs.sort(
            key=lambda pair: (
                not pair.stats_pass,
                pair.stats.q_value is None,
                pair.stats.q_value if pair.stats.q_value is not None else 0.0,
                pair.oncotree_code,
            )
        )
        return GeneStatsView(
            query=resolved.query,
            hugo_symbol=resolved.hugo_symbol,
            entrez_gene_id=resolved.artifact.gene.entrez_gene_id,
            broadly_expressed=resolved.artifact.broadly_expressed,
            literature_gate=self.gate.name,
            bundle=bundle_provenance,
            summary=summary,
            pairs=pairs,
            has_stored_result=has_stored_result,
        )

    async def gene_view(
        self,
        gene: str,
        *,
        resolved: Optional[ResolvedGene] = None,
    ) -> Tuple[GeneStatsView, Dict[str, StoredPair]]:
        """The active bundle's statistics for every cancer type of a gene, joined
        with any stored literature verdicts for this release only (the reference view)."""
        if resolved is None or resolved.bundle is None:
            resolved = await self.resolve(gene)
        else:
            try:
                await self.catalog.verify_active(resolved.bundle)
            except BundleGone:
                resolved = await self.resolve(gene)
        stored = await self.store.get_pairs(
            resolved.hugo_symbol, resolved.manifest.release_id, self.gate.name
        )
        return self._evaluate_gene_view(resolved, stored), stored

    async def latest_result(
        self, gene: str, include_excluded: bool
    ) -> GeneResult:
        view, stored = await self.gene_view(gene)
        historical: List[StoredPair] = []
        if not stored:
            historical = await self.store.latest_pairs(view.hugo_symbol, self.gate.name)
            if not historical:
                raise GeneNotFound(view.hugo_symbol)
        return self._gene_result_from_view(
            view, stored, historical, include_excluded=include_excluded
        )

    async def export_gene_csv(
        self, gene: str, *, include_excluded: bool = False
    ) -> Tuple[str, str]:
        view, stored = await self.gene_view(gene)
        if not stored:
            historical = await self.store.latest_pairs(view.hugo_symbol, self.gate.name)
            if not historical:
                raise GeneNotFound(view.hugo_symbol)
        csv_rows = gene_view_csv_rows(view, include_excluded=include_excluded)
        return (
            csv_filename(view.hugo_symbol, view.bundle.release_id),
            format_csv_rows(csv_rows, include_header=True, include_bom=True),
        )

    async def _stream_bundle_views_to_csv(
        self,
        bundle: ActiveBundle,
        *,
        view_to_rows: Callable[[GeneStatsView], List[List[str]]],
        format_rows: Callable[..., str],
        include_excluded: bool = False,
        page_size: int = 50,
    ) -> Tuple[ActiveBundle, AsyncIterator[str]]:
        release_id = bundle.release_id
        gate_name = self.gate.name
        batch_size = max(1, min(page_size, self.history_max_limit))

        async def fetch_page(after: Optional[str]) -> Tuple[List[str], List[List[str]]]:
            await self.catalog.verify_active(bundle)
            if include_excluded:
                symbols = await self.catalog.list_genes(bundle, batch_size, after_symbol=after)
            else:
                symbols = await self.store.list_release_genes(
                    release_id, gate_name, batch_size, after_symbol=after
                )
            if not symbols:
                return symbols, []

            page_rows: List[List[str]] = []
            for symbol in symbols:
                await self.catalog.verify_active(bundle)
                artifact = await self.catalog.gene(bundle, symbol)
                if artifact is None:
                    continue
                resolved = ResolvedGene(
                    query=symbol,
                    hugo_symbol=artifact.gene.hugo_symbol.upper(),
                    manifest=bundle.manifest,
                    artifact=artifact,
                    bundle=bundle,
                )
                stored = await self.store.get_pairs(
                    resolved.hugo_symbol, release_id, gate_name
                )
                view = self._evaluate_gene_view(resolved, stored)
                rows = view_to_rows(view)
                page_rows.extend(rows)
            return symbols, page_rows

        first_symbols, first_page_rows = await fetch_page(None)

        async def stream() -> AsyncIterator[str]:
            yield format_rows((), include_header=True, include_bom=True)
            try:
                await self.catalog.verify_active(bundle)
            except BundleGone as exc:
                logger.error("Active bundle superseded during bulk export stream: %s", exc)
                raise

            if first_page_rows:
                yield format_rows(first_page_rows, include_header=False, include_bom=False)
            if len(first_symbols) < batch_size:
                try:
                    await self.catalog.verify_active(bundle)
                except BundleGone as exc:
                    logger.error("Active bundle superseded before ending bulk export stream: %s", exc)
                    raise
                return
            after_symbol = first_symbols[-1]
            while True:
                symbols, page_rows = await fetch_page(after_symbol)
                if not symbols:
                    break
                try:
                    await self.catalog.verify_active(bundle)
                except BundleGone as exc:
                    logger.error("Active bundle superseded during bulk export stream: %s", exc)
                    raise
                if page_rows:
                    yield format_rows(page_rows, include_header=False, include_bom=False)
                if len(symbols) < batch_size:
                    break
                after_symbol = symbols[-1]

            try:
                await self.catalog.verify_active(bundle)
            except BundleGone as exc:
                logger.error("Active bundle superseded before ending bulk export stream: %s", exc)
                raise

        return bundle, stream()

    async def export_bundle_csv(
        self, *, include_excluded: bool = False, page_size: int = 50
    ) -> Tuple[str, AsyncIterator[str]]:
        bundle = await self.catalog.active()
        manifest_cts = (
            bundle.manifest.cancer_types
            if hasattr(bundle.manifest, "cancer_types")
            else (bundle.manifest.get("cancer_types", []) if isinstance(bundle.manifest, dict) else [])
        )
        all_study_ids = [
            getattr(ct, "study_id", None) if not isinstance(ct, dict) else ct.get("study_id")
            for ct in manifest_cts
        ]
        all_study_ids = [sid for sid in all_study_ids if sid]

        def view_to_rows(view: GeneStatsView) -> List[List[str]]:
            return gene_view_csv_rows(
                view, include_excluded=include_excluded, all_study_ids=all_study_ids
            )

        active_bundle, stream = await self._stream_bundle_views_to_csv(
            bundle,
            view_to_rows=view_to_rows,
            format_rows=format_csv_rows,
            include_excluded=include_excluded,
            page_size=page_size,
        )
        return csv_filename("all", active_bundle.release_id), stream

    async def export_golden_set_bundle_csv(
        self, *, page_size: int = 50
    ) -> Tuple[str, AsyncIterator[str]]:
        bundle = await self.catalog.active()
        name_map = load_sheet_cancer_names()
        check_sheet_cancer_names(bundle, name_map)

        def view_to_rows(view: GeneStatsView) -> List[List[str]]:
            return [golden_set_row_from_view(view, name_map=name_map)]

        active_bundle, stream = await self._stream_bundle_views_to_csv(
            bundle,
            view_to_rows=view_to_rows,
            format_rows=format_golden_set_rows,
            include_excluded=False,
            page_size=page_size,
        )
        return golden_set_filename(active_bundle.release_id), stream

    async def export_golden_set_genes_csv(
        self, genes_query: str
    ) -> Tuple[str, str]:
        raw_genes = [g.strip() for g in genes_query.split(",") if g.strip()]
        if not raw_genes:
            raise InvalidGene("No genes specified.")
        if len(raw_genes) > 500:
            raise InvalidGene(f"Maximum 500 raw genes allowed per request; received {len(raw_genes)}.")

        bundle = await self.catalog.active()
        await self.catalog.verify_active(bundle)

        name_map = load_sheet_cancer_names()
        check_sheet_cancer_names(bundle, name_map)

        seen_symbols: set[str] = set()
        resolved_genes: List[ResolvedGene] = []
        bad_names: List[str] = []

        for raw_gene in raw_genes:
            try:
                symbol = normalize_symbol(raw_gene)
            except InvalidGene:
                bad_names.append(raw_gene)
                continue

            artifact = await self.catalog.gene(bundle, symbol)
            if artifact is None:
                target = await self.catalog.alias(bundle, symbol)
                if target and target != symbol:
                    artifact = await self.catalog.gene(bundle, target)

            if artifact is None:
                bad_names.append(raw_gene)
            else:
                hugo = artifact.gene.hugo_symbol.upper()
                if hugo not in seen_symbols:
                    seen_symbols.add(hugo)
                    resolved_genes.append(
                        ResolvedGene(
                            query=raw_gene,
                            hugo_symbol=hugo,
                            manifest=bundle.manifest,
                            artifact=artifact,
                            bundle=bundle,
                        )
                    )

        if bad_names:
            raise InvalidGene(f"Unknown genes: {', '.join(bad_names)}")

        rows: List[List[str]] = []
        not_yet_checked: List[str] = []
        for resolved in resolved_genes:
            view, stored = await self.gene_view(resolved.hugo_symbol, resolved=resolved)
            if not stored:
                not_yet_checked.append(resolved.hugo_symbol)
            else:
                rows.append(golden_set_row_from_view(view, name_map=name_map))

        await self.catalog.verify_active(bundle)

        if not_yet_checked:
            raise GenesNotChecked(not_yet_checked)

        filename = golden_set_filename(bundle.release_id)
        content = format_golden_set_rows(rows, include_header=True, include_bom=True)
        return filename, content

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
        view, _ = await self.gene_view(gene)
        return view

    async def history(self, limit: int, cursor: Optional[str]) -> HistoryPage:
        decoded = decode_cursor(cursor) if cursor else None
        limit = max(1, min(limit, self.history_max_limit))
        for attempt in range(2):
            bundle = await self.catalog.active()
            raw_entries = await self.store.list_history(self.gate.name, limit + 1, decoded)
            page = raw_entries[:limit]
            try:
                evaluated_page: List[HistoryEntry] = []
                for entry in page:
                    if entry.release_id != bundle.release_id:
                        evaluated_page.append(entry.model_copy(update={"surfaced_count": 0}))
                    else:
                        try:
                            view, _ = await self.gene_view(entry.hugo_symbol)
                            await self.catalog.verify_active(bundle)
                            surfaced_count = view.summary.surfaced
                        except GeneNotFound:
                            surfaced_count = 0
                        evaluated_page.append(
                            entry.model_copy(update={"surfaced_count": surfaced_count})
                        )
                break
            except BundleGone:
                if attempt:
                    raise BundleNotLoaded("the active bundle changed while reading history")
        next_cursor = encode_cursor(page[-1]) if len(raw_entries) > limit and page else None
        return HistoryPage(items=evaluated_page, next_cursor=next_cursor)

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
    def _gene_result_from_view(
        view: GeneStatsView,
        stored: Mapping[str, StoredPair],
        historical: Sequence[StoredPair] = (),
        *,
        include_excluded: bool,
    ) -> GeneResult:
        pair_results: List[PairResult] = []
        all_rows = list(stored.values()) if stored else list(historical)
        default_computed_at = max((r.computed_at for r in all_rows), default=_utcnow())
        for pair in view.pairs:
            is_surfaced = (pair.status == "surfaced")
            if is_surfaced:
                reason = None
            elif pair.status == "literature_pending":
                reason = "pending"
            else:
                reason = pair.exclusion_reason or "literature"

            stored_row = stored.get(pair.oncotree_code)
            computed_at = (
                stored_row.computed_at
                if stored_row and stored_row.computed_at is not None
                else default_computed_at
            )
            pair_results.append(
                PairResult(
                    oncotree_code=pair.oncotree_code,
                    cancer_type_name=pair.cancer_type_name,
                    study_id=pair.study_id,
                    surfaced=is_surfaced,
                    exclusion_reason=reason,
                    stats=pair.stats,
                    literature=pair.literature,
                    computed_at=computed_at,
                    bundle_tag=view.bundle.bundle_tag,
                )
            )

        surfaced = sorted(
            (p for p in pair_results if p.surfaced),
            key=lambda p: (
                p.stats.q_value is None,
                p.stats.q_value if p.stats.q_value is not None else 0.0,
                p.oncotree_code,
            ),
        )
        excluded = (
            sorted((p for p in pair_results if not p.surfaced), key=lambda p: p.oncotree_code)
            if include_excluded
            else None
        )

        all_rows = list(stored.values()) if stored else list(historical)
        computed_at = max((r.computed_at for r in all_rows), default=_utcnow())
        return GeneResult(
            hugo_symbol=view.hugo_symbol,
            broadly_expressed=view.broadly_expressed,
            release_id=view.bundle.release_id,
            literature_gate=view.literature_gate,
            computed_at=computed_at,
            surfaced=surfaced,
            excluded=excluded,
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

