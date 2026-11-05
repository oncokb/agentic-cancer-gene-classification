"""Best-effort cross-pod lease on a literature-verdict key (Redis SET NX PX).

Only one holder should run the (expensive) literature gate for a given
(hugo_symbol, oncotree_code, release_id, literature_gate) at a time. Other
pods wait for the holder's stored verdict instead of duplicating the work.

Fails open like the rest of ACGC's Redis use: if Redis is unreachable the
claim reports ``unavailable`` and the caller proceeds with only in-process
de-duplication. Release and renewal are compare-then-act (GET, then DEL/SET)
rather than Lua, so they also work against the test FakeRedis; the tiny race
(the lease expiring between the GET and the DEL) can at worst cause one
duplicate gate call, and the store re-check after every claim bounds that.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

logger = logging.getLogger(__name__)

LEASE_PREFIX = "expression_context:lease:"

ClaimOutcome = Literal["claimed", "held", "unavailable"]


def _default_client() -> Any:
    # Same shared client (and Sentinel handling) as the reference-data cache
    # and JobStore use.
    from src.pipeline import cache

    return cache._get_client()


def lease_key(hugo_symbol: str, oncotree_code: str, release_id: str, literature_gate: str) -> str:
    return f"{LEASE_PREFIX}{literature_gate}:{release_id}:{hugo_symbol}:{oncotree_code}"


@dataclass
class Claim:
    outcome: ClaimOutcome
    token: Optional[str] = None


class VerdictLease:
    def __init__(self, ttl_seconds: float, client_factory: Callable[[], Any] = _default_client):
        self.ttl_seconds = ttl_seconds
        self._ttl_ms = max(1, int(ttl_seconds * 1000))
        self._client_factory = client_factory

    async def claim(self, key: str) -> Claim:
        token = uuid.uuid4().hex
        try:
            acquired = await self._client_factory().set(key, token, px=self._ttl_ms, nx=True)
        except Exception as exc:
            logger.warning(
                "Expression-context lease claim failed for %r (%s); proceeding without a "
                "cross-pod lease, so another pod may duplicate this literature check",
                key, exc,
            )
            return Claim("unavailable")
        return Claim("claimed", token) if acquired else Claim("held")

    async def is_held(self, key: str) -> bool:
        """Whether anyone holds the lease. Unknown (Redis error) counts as not held."""
        try:
            return await self._client_factory().get(key) is not None
        except Exception:
            return False

    async def _owned(self, client: Any, key: str, token: str) -> bool:
        current = await client.get(key)
        if isinstance(current, bytes):
            current = current.decode()
        return current == token

    async def renew(self, key: str, token: str) -> bool:
        try:
            client = self._client_factory()
            if not await self._owned(client, key, token):
                return False
            await client.set(key, token, px=self._ttl_ms)
            return True
        except Exception as exc:
            logger.warning("Expression-context lease renewal failed for %r: %s", key, exc)
            return True

    async def release(self, key: str, token: str) -> None:
        try:
            client = self._client_factory()
            if await self._owned(client, key, token):
                await client.delete(key)
        except Exception as exc:
            logger.warning("Expression-context lease release failed for %r: %s", key, exc)
