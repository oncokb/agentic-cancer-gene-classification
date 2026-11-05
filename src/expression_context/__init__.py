"""Optional, removable expression-context module.

Surfaces (gene, TCGA cancer type) pairs where RNA-seq expression is
significantly enriched in that tumor type (precomputed by the separate stats
repo) AND the literature characterizes the gene there (literature gate).

ACGC uses exactly one entry point, from ``src/main.py``::

    register_expression_context(app)

With ``EXPRESSION_CONTEXT_ENABLED`` unset/false this is a no-op: no routes,
no table, no outbound calls, no startup work, and the rest of the module is
not even imported. See ``REMOVAL.md`` to delete the feature and
``docs/expression-context.md`` for the contract.
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
    from src.expression_context.artifact_client import ArtifactClient
    from src.expression_context.lease import VerdictLease
    from src.expression_context.literature_gate import AcgcPipelineGate
    from src.expression_context.redis_client import ModuleRedis
    from src.expression_context.models import JobRecord
    from src.expression_context.service import ExpressionContextService
    from src.expression_context.store import MySQLExpressionContextStore
    from src.job_store import JobStore

    store = await MySQLExpressionContextStore.create(maxsize=settings.mysql_pool_maxsize)
    try:
        await store.ensure_schema()
    except BaseException:
        await store.close()
        raise
    redis = ModuleRedis(redis_url=settings.redis_url)  # one lazily-built client, shared
    return ExpressionContextService(
        artifacts=ArtifactClient(settings, redis=redis),
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

    factory = service_factory or _default_service_factory
    app.include_router(router)

    inner_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app_: Any):
        async with inner_lifespan(app_) as state:
            service = await factory(app_, settings)
            setattr(app_.state, STATE_ATTR, service)
            logger.info("Expression context enabled (artifact base %s)", settings.base_url)
            try:
                yield state
            finally:
                setattr(app_.state, STATE_ATTR, None)
                await service.aclose()

    app.router.lifespan_context = lifespan
    return True
