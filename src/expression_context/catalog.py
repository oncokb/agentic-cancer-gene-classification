"""Request-path read access to the active statistics bundle.

Every request reads the bundle the cluster-wide pointer names (cached in
process for ``active_bundle_cache_seconds``; the pod that switches bundles
invalidates its cache immediately). No bundle loaded yet → ``BundleNotLoaded``
(HTTP 503). Nothing here makes outbound calls.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from src.expression_context.bundle_store import BundleRecord, BundleStore
from src.expression_context.models import (
    BundleManifest,
    BundleProvenance,
    BundleStatus,
    GeneArtifact,
    RefreshState,
)


class BundleNotLoaded(RuntimeError):
    """No statistics bundle has been loaded into this deployment yet."""


class BundleGone(RuntimeError):
    """The bundle a reader was using is no longer available (pruned/replaced);
    re-read the active bundle and retry instead of answering 404."""


@dataclass(frozen=True)
class ActiveBundle:
    record: BundleRecord
    manifest: BundleManifest

    @property
    def tag(self) -> str:
        return self.record.bundle_tag

    @property
    def release_id(self) -> str:
        return self.manifest.release_id

    def provenance(self) -> BundleProvenance:
        return provenance_of(self.record, self.manifest)


def provenance_of(record: BundleRecord, manifest: Optional[BundleManifest] = None) -> BundleProvenance:
    manifest = manifest or BundleManifest.model_validate(record.manifest)
    return BundleProvenance(
        bundle_tag=record.bundle_tag,
        release_id=record.release_id,
        schema_version=record.schema_version,
        method_version=record.method_version,
        pubmed_snapshot_date=record.pubmed_snapshot_date,
        gene_count=record.loaded_rows or record.gene_count,
        published_at=record.published_at,
        loaded_at=record.finished_at,
        release_url=record.release_url,
        source=manifest.source,
        params=manifest.params,
    )


class StatsCatalog:
    def __init__(
        self,
        bundles: BundleStore,
        *,
        cache_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bundles = bundles
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._cached: Optional[ActiveBundle] = None
        self._cached_at = float("-inf")

    def invalidate(self) -> None:
        self._cached_at = float("-inf")

    async def active(self) -> ActiveBundle:
        now = self._clock()
        if self._cached is not None and now - self._cached_at < self._cache_seconds:
            return self._cached
        state = await self._bundles.get_state()
        tag = state.active_bundle_tag
        if not tag:
            raise BundleNotLoaded("no expression statistics bundle is loaded yet")
        if self._cached is None or self._cached.tag != tag:
            record = await self._bundles.get_bundle(tag)
            if record is None or record.status != "ready":
                raise BundleNotLoaded(f"active bundle {tag} is not available")
            self._cached = ActiveBundle(record, BundleManifest.model_validate(record.manifest))
        self._cached_at = now
        return self._cached

    async def _check_still_there(self, bundle: ActiveBundle) -> None:
        record = await self._bundles.get_bundle(bundle.tag)
        if record is None or record.status != "ready":
            self.invalidate()
            raise BundleGone(bundle.tag)

    async def gene(self, bundle: ActiveBundle, hugo_symbol: str) -> Optional[GeneArtifact]:
        """The gene's document, None if the bundle doesn't have it; BundleGone
        if the bundle itself has gone away meanwhile."""
        raw = await self._bundles.get_gene_doc(bundle.tag, hugo_symbol)
        if raw is None:
            await self._check_still_there(bundle)
            return None
        return GeneArtifact.model_validate_json(raw)

    async def alias(self, bundle: ActiveBundle, symbol: str) -> Optional[str]:
        target = await self._bundles.get_alias(bundle.tag, symbol)
        if target is None:
            await self._check_still_there(bundle)
        return target

    async def status(self, *, releases_repo: str, pinned_bundle_tag: Optional[str]) -> BundleStatus:
        state = await self._bundles.get_state()
        active = None
        if state.active_bundle_tag:
            record = await self._bundles.get_bundle(state.active_bundle_tag)
            if record is not None and record.status == "ready":
                active = provenance_of(record)
        return BundleStatus(
            active=active,
            refresh=RefreshState(
                last_checked_at=state.last_checked_at,
                next_check_after=state.next_check_after,
                last_error=state.last_error,
                last_error_at=state.last_error_at,
                pinned_bundle_tag=pinned_bundle_tag,
                releases_repo=releases_repo,
            ),
        )
