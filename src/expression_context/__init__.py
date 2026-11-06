"""Optional, removable expression-context module.

Surfaces (gene, TCGA cancer type) pairs where RNA-seq expression is
significantly enriched in that tumor type (precomputed by ``expression_stats/``
and published as GitHub release bundles) AND the literature characterizes the
gene there (literature gate). Also serves the "Expression stats" page.

ACGC uses exactly one entry point, from ``src/main.py``::

    register_expression_context(app)

With ``EXPRESSION_CONTEXT_ENABLED`` unset/false this is a no-op: no routes,
no page or nav entry, no tables, no outbound calls (GitHub included), no
startup work, and the rest of the module is not even imported. See
``REMOVAL.md`` to delete the feature and ``docs/expression-context.md`` for the
design.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable, Optional

from src.expression_context.settings import ExpressionContextSettings

logger = logging.getLogger(__name__)

ServiceFactory = Callable[[Any, ExpressionContextSettings], Awaitable[Any]]

__all__ = ["ExpressionContextSettings", "register_expression_context"]


async def _default_service_factory(app: Any, settings: ExpressionContextSettings) -> Any:
    from src.expression_context.bundle_loader import BundleRefresher
    from src.expression_context.bundle_store import MySQLBundleStore
    from src.expression_context.catalog import StatsCatalog
    from src.expression_context.github_releases import ReleasesClient
    from src.expression_context.lease import VerdictLease
    from src.expression_context.literature_gate import AcgcPipelineGate
    from src.expression_context.models import JobRecord
    from src.expression_context.redis_client import ModuleRedis
    from src.expression_context.service import ExpressionContextService
    from src.expression_context.store import MySQLExpressionContextStore
    from src.job_store import JobStore

    store = await MySQLExpressionContextStore.create(maxsize=settings.mysql_pool_maxsize)
    bundles = MySQLBundleStore(store.pool)
    try:
        await store.ensure_schema()
        await bundles.ensure_schema()
    except BaseException:
        await store.close()
        raise
    redis = ModuleRedis(redis_url=settings.redis_url)  # one lazily-built client, shared
    catalog = StatsCatalog(bundles, cache_seconds=settings.active_bundle_cache_seconds)
    releases = ReleasesClient(settings)
    refresher = BundleRefresher(
        settings, bundles=bundles, releases=releases, on_activated=catalog.invalidate
    )
    return ExpressionContextService(
        catalog=catalog,
        gate=AcgcPipelineGate(
            lambda: getattr(app.state, "run_store", None), mode=settings.pipeline_mode
        ),
        store=store,
        jobs=JobStore("expression_context", JobRecord),
        literature_concurrency=settings.literature_concurrency,
        history_max_limit=settings.history_max_limit,
        lease=VerdictLease(ttl_seconds=settings.lease_ttl_seconds, redis=redis),
        lease_wait_seconds=settings.lease_wait_seconds,
        lease_poll_seconds=settings.lease_poll_seconds,
        refresher=refresher,
        closers=[releases.aclose, bundles.close],
        releases_repo=settings.releases_repo,
        pinned_bundle_tag=settings.pinned_bundle_tag,
    )


def register_expression_context(
    app: Any,
    *,
    settings: Optional[ExpressionContextSettings] = None,
    service_factory: Optional[ServiceFactory] = None,
) -> bool:
    """Mount the module on ``app`` if enabled. Returns whether it was mounted."""
    settings = settings or ExpressionContextSettings()
    if not settings.enabled:
        return False

    from src.expression_context.router import STATE_ATTR, router
    from src.expression_context.web import NavLinkMiddleware, page_router

    factory = service_factory or _default_service_factory
    app.include_router(router)
    app.include_router(page_router)
    app.add_middleware(NavLinkMiddleware)

    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_: Any):
        async with inner_lifespan(app_) as state:
            service = await factory(app_, settings)
            setattr(app_.state, STATE_ATTR, service)
            start = getattr(service, "start", None)
            if callable(start):
                start()  # background bundle refresher; first check runs now
            logger.info(
                "Expression context enabled (bundles from %s%s)",
                settings.releases_repo,
                f", pinned to {settings.pinned_bundle_tag}" if settings.pinned_bundle_tag else "",
            )
            try:
                yield state
            finally:
                setattr(app_.state, STATE_ATTR, None)
                await service.aclose()

    app.router.lifespan_context = lifespan
    return True
