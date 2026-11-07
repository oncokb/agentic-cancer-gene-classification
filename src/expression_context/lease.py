"""Best-effort cross-pod lease on a literature-verdict key.

Only one holder should run the (expensive) literature gate for a given
(hugo_symbol, oncotree_code, release_id, literature_gate) at a time. Other
pods wait for the holder's stored verdict instead of duplicating the work.

- claim: ``SET key token NX PX ttl``.
- renew / release: owner-safe Lua (compare-and-PEXPIRE / compare-and-DEL), so
  a holder whose lease expired can never extend or delete a newer holder's
  lease.

Fails open like the rest of ACGC's Redis use: if Redis is unreachable the
claim reports ``unavailable`` and the caller proceeds with only in-process
de-duplication.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Literal, Optional

from src.expression_context.redis_client import ModuleRedis, RedisUnavailable

logger = logging.getLogger(__name__)

LEASE_PREFIX = "expression_context:lease:"

# KEYS[1]=lease key, ARGV[1]=owner token, ARGV[2]=ttl ms
RENEW_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("PEXPIRE", KEYS[1], ARGV[2])
end
return 0
"""

# KEYS[1]=lease key, ARGV[1]=owner token
RELEASE_SCRIPT = """
if redis.call("GET", KEYS[1]) == ARGV[1] then
  return redis.call("DEL", KEYS[1])
end
return 0
"""

ClaimOutcome = Literal["claimed", "held", "unavailable"]


def lease_key(hugo_symbol: str, oncotree_code: str, release_id: str, literature_gate: str) -> str:
    return f"{LEASE_PREFIX}{literature_gate}:{release_id}:{hugo_symbol}:{oncotree_code}"


@dataclass
class Claim:
    outcome: ClaimOutcome
    token: Optional[str] = None


class VerdictLease:
    def __init__(self, ttl_seconds: float, redis: Optional[ModuleRedis] = None):
        self.ttl_seconds = ttl_seconds
        self._ttl_ms = max(1, int(ttl_seconds * 1000))
        self.redis = redis or ModuleRedis()

    async def claim(self, key: str) -> Claim:
        token = uuid.uuid4().hex
        try:
            acquired = await self.redis.client().set(key, token, px=self._ttl_ms, nx=True)
        except Exception as exc:
            if not isinstance(exc, RedisUnavailable):
                self.redis.mark_down(exc)
            logger.warning(
                "Expression-context lease claim unavailable for %r (%s); proceeding without a "
                "cross-pod lease, so another pod may duplicate this literature check",
                key, exc,
            )
            return Claim("unavailable")
        return Claim("claimed", token) if acquired else Claim("held")

    async def renew(self, key: str, token: str) -> bool:
        """Extend the lease iff ``token`` still owns it. False if it doesn't;
        True (fail open) if Redis is unreachable."""
        try:
            return bool(await self.redis.client().eval(RENEW_SCRIPT, 1, key, token, self._ttl_ms))
        except Exception as exc:
            if not isinstance(exc, RedisUnavailable):
                self.redis.mark_down(exc)
            logger.warning("Expression-context lease renewal unavailable for %r: %s", key, exc)
            return True

    async def release(self, key: str, token: str) -> bool:
        """Delete the lease iff ``token`` still owns it."""
        try:
            return bool(await self.redis.client().eval(RELEASE_SCRIPT, 1, key, token))
        except Exception as exc:
            if not isinstance(exc, RedisUnavailable):
                self.redis.mark_down(exc)
            logger.warning("Expression-context lease release unavailable for %r: %s", key, exc)
            return False

    async def aclose(self) -> None:
        await self.redis.aclose()
