"""Joins the precomputed stats gate with the on-demand literature gate.

A (gene, cancer type) pair is *surfaced* iff:

1. ``stats_pass`` (stats repo), and
2. ``literature_precheck_pass`` (stats repo's PubMed-count pre-check), and
3. the literature gate's verdict is supported (``insufficient_evidence`` is
   false AND >=1 cited PMID).

Only pairs passing 1 and 2 reach the literature gate. Verdicts are stored per
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
from typing import Callable, Dict, List, Optional, Set, Tuple

from src.expression_context.artifact_client import ArtifactClient, ArtifactContractError
from src.expression_context.lease import VerdictLease, lease_key
from src.expression_context.literature_gate import LiteratureGate, is_supported
from src.expression_context.models import (
    CancerTypeStats,
    GeneArtifact,
    GeneResult,
    HistoryPage,
    JobPairError,
    JobRecord,
    LiteratureVerdict,
    Manifest,
    PairResult,
    StoredPair,
)
from src.expression_context.store import (
    ExpressionContextStore,
    decode_cursor,
    encode_cursor,
)

logger = logging.getLogger(__name__)

_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9._-]{0,63}\Z")


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
        artifacts: ArtifactClient,
        gate: LiteratureGate,
        store: ExpressionContextStore,
        jobs: JobStoreLike,
        literature_concurrency: int = 2,
        history_max_limit: int = 100,
        lease: Optional[VerdictLease] = None,
        lease_wait_seconds: float = 300.0,
        lease_poll_seconds: float = 2.0,
    ) -> None:
        self.artifacts = artifacts
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
        """Canonical symbol first, then aliases.json. Raises GeneNotFound."""
        symbol = normalize_symbol(gene)
        manifest = await self.artifacts.get_manifest()
        artifact, manifest = await self._gene_file(symbol, manifest)
        if artifact is None:
            aliases = await self.artifacts.get_aliases(manifest.release_id)
            target = aliases.get(symbol)
            if target and target != symbol:
                artifact, manifest = await self._gene_file(target, manifest)
        if artifact is None:
            raise GeneNotFound(symbol)
        return ResolvedGene(
            query=gene,
            hugo_symbol=artifact.gene.hugo_symbol.upper(),
            manifest=manifest,
            artifact=artifact,
        )

    async def _gene_file(
        self, symbol: str, manifest: Manifest
    ) -> Tuple[Optional[GeneArtifact], Manifest]:
        artifact = await self.artifacts.get_gene(symbol, manifest.release_id)
        if artifact is not None and artifact.release_id != manifest.release_id:
            # A new release was published after the manifest was cached:
            # refresh both once, then insist they agree.
            manifest = await self.artifacts.get_manifest(force_refresh=True)
            artifact = await self.artifacts.get_gene(
                symbol, manifest.release_id, force_refresh=True
            )
            if artifact is not None and artifact.release_id != manifest.release_id:
                raise ArtifactContractError(
                    f"genes/{symbol}.json release_id {artifact.release_id!r} does not match "
                    f"manifest release_id {manifest.release_id!r}"
                )
        return artifact, manifest

    # ------------------------------------------------------------------ jobs

    async def submit(self, gene: str, owner: Optional[str]) -> JobRecord:
        resolved = await self.resolve(gene)
        job = JobRecord(
            job_id=str(uuid.uuid4()),
            status="queued",
            gene=gene,
            hugo_symbol=resolved.hugo_symbol,
            release_id=resolved.manifest.release_id,
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
        now = _utcnow()

        rows: List[StoredPair] = []
        to_check: List[Tuple[CancerTypeStats, str]] = []
        errors: List[JobPairError] = []
        reused = 0

        def row(stats: CancerTypeStats, surfaced: bool, reason, evidence=None) -> StoredPair:
            cancer_type = manifest.cancer_type(stats.oncotree_code)
            stored_stats = stats.model_dump()
            # Denormalized display fields, so reads never need the artifact.
            stored_stats["cancer_type_name"] = cancer_type.name if cancer_type else None
            stored_stats["study_id"] = cancer_type.study_id if cancer_type else None
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
            if not stats.stats_pass:
                rows.append(row(stats, False, "stats"))
                continue
            if not stats.literature_precheck_pass:
                rows.append(row(stats, False, "precheck"))
                continue
            prior = existing.get(stats.oncotree_code)
            if prior is not None and prior.evidence is not None:
                verdict = LiteratureVerdict.model_validate(prior.evidence)
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
            to_check.append((stats, cancer_type.tumor_type_query))

        async def check(stats: CancerTypeStats, query: str):
            try:
                verdict, was_reused = await self._single_flight_verdict(
                    symbol, stats.oncotree_code, manifest.release_id, query,
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
        outcomes = await asyncio.gather(*(check(s, q) for s, q in to_check))
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
        tumor_type_query: str,
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
            outcome = await self._resolve_verdict(key, tumor_type_query, make_row)
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
        return LiteratureVerdict.model_validate(prior.evidence)

    async def _resolve_verdict(
        self,
        key: Tuple[str, str, str, str],
        tumor_type_query: str,
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
                    return await self._evaluate_and_store(key, tumor_type_query, make_row), False
                await asyncio.sleep(self._lease_poll_seconds)
                continue
            try:
                # Re-check: the previous holder may have stored it just now.
                stored = await self._stored_verdict(key)
                if stored is not None:
                    return stored, True
                verdict = await self._evaluate_and_store(
                    key, tumor_type_query, make_row, lease=(lkey, claim.token)
                )
                return verdict, False
            finally:
                if claim.token:
                    await self._lease.release(lkey, claim.token)

    async def _evaluate_and_store(
        self,
        key: Tuple[str, str, str, str],
        tumor_type_query: str,
        make_row: Callable[[LiteratureVerdict], StoredPair],
        lease: Optional[Tuple[str, Optional[str]]] = None,
    ) -> LiteratureVerdict:
        heartbeat = None
        if lease is not None and lease[1]:
            heartbeat = asyncio.create_task(self._heartbeat(*lease))
        try:
            async with self._gate_slots:
                verdict = await self.gate.evaluate(key[0], tumor_type_query)
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
        # Artifact errors propagate (-> 502): an outage must not read as 404.
        manifest = await self.artifacts.get_manifest()
        return (await self.artifacts.get_aliases(manifest.release_id)).get(symbol)

    async def history(self, limit: int, cursor: Optional[str]) -> HistoryPage:
        decoded = decode_cursor(cursor) if cursor else None
        limit = max(1, min(limit, self.history_max_limit))
        entries = await self.store.list_history(self.gate.name, limit + 1, decoded)
        page = entries[:limit]
        next_cursor = encode_cursor(page[-1]) if len(entries) > limit and page else None
        return HistoryPage(items=page, next_cursor=next_cursor)

    # ------------------------------------------------------------ lifecycle

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.artifacts.aclose()
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
            key=lambda pair: (pair.stats.q_value, pair.oncotree_code),
        )
        excluded = sorted(
            (pair for pair in pairs if not pair.surfaced), key=lambda pair: pair.oncotree_code
        )
        return GeneResult(
            hugo_symbol=symbol,
            release_id=rows[0].release_id if rows else "",
            literature_gate=rows[0].literature_gate if rows else "",
            computed_at=max((row.computed_at for row in rows), default=_utcnow()),
            surfaced=surfaced,
            excluded=excluded if include_excluded else None,
        )


def _verdict_row(make_row, stats: CancerTypeStats, verdict: LiteratureVerdict) -> StoredPair:
    # Recompute "supported" from the stored fields so a gate can't surface a
    # pair that violates the v1 rule.
    supported = is_supported(verdict.insufficient_evidence, verdict.pmids)
    verdict = verdict.model_copy(update={"supported": supported})
    return make_row(
        stats,
        supported,
        None if supported else "literature",
        verdict.model_dump(mode="json"),
    )


def _pair_result(row: StoredPair) -> PairResult:
    return PairResult(
        oncotree_code=row.oncotree_code,
        cancer_type_name=row.stats.get("cancer_type_name"),
        study_id=row.stats.get("study_id"),
        surfaced=row.surfaced,
        exclusion_reason=row.exclusion_reason,
        stats=CancerTypeStats.model_validate(row.stats),
        literature=LiteratureVerdict.model_validate(row.evidence) if row.evidence else None,
        computed_at=row.computed_at,
    )

