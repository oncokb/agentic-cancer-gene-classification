"""HTTP client for the stats repo's static v1 JSON artifacts.

Contract (docs/expression-context.md):

- ``GET {base}/v1/manifest.json``
- ``GET {base}/v1/genes/{HUGO_SYMBOL}.json`` (404 when the gene is absent)
- ``GET {base}/v1/aliases.json``

Requests have explicit timeouts and retry transient failures (transport
errors, 429, 5xx) with tenacity. Successful fetches go through the shared
Redis ``cached_call`` (fail-open), keyed by release_id for gene/alias files.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional

import httpx
from pydantic import ValidationError
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.expression_context.models import SUPPORTED_SCHEMA_VERSION, GeneArtifact, Manifest
from src.expression_context.settings import ExpressionContextSettings
from src.pipeline.cache import cached_call

logger = logging.getLogger(__name__)

_MISSING = {"__expression_context_missing__": True}


class ArtifactError(RuntimeError):
    """The stats artifact could not be fetched or does not honour the contract."""


class ArtifactUnavailable(ArtifactError):
    """Network failure, timeout, or unexpected HTTP status after retries."""


class ArtifactContractError(ArtifactError):
    """The artifact parsed but violates the contract (e.g. unknown schema_version)."""


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or code >= 500
    return False


def _check_schema_version(payload: Any, what: str) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ArtifactContractError(f"{what}: expected a JSON object")
    version = payload.get("schema_version")
    if version is None or str(version) != SUPPORTED_SCHEMA_VERSION:
        raise ArtifactContractError(
            f"{what}: unsupported schema_version {version!r} "
            f"(supported: {SUPPORTED_SCHEMA_VERSION!r})"
        )
    return payload


class ArtifactClient:
    def __init__(
        self,
        settings: ExpressionContextSettings,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._settings = settings
        self._base = settings.base_url
        # Short, stable namespace so two deployments pointing at different
        # artifact hosts never share cache entries.
        self._ns = hashlib.sha256(self._base.encode()).hexdigest()[:12]
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.http_timeout_seconds, connect=settings.http_connect_timeout_seconds
            ),
            follow_redirects=True,
            headers={"Accept": "application/json"},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    def _key(self, *parts: str) -> str:
        return ":".join(("expression_context", self._ns, *parts))

    async def _get_json(self, path: str, *, allow_404: bool = False) -> Optional[Any]:
        url = f"{self._base}/v1/{path}"
        retrying = AsyncRetrying(
            stop=stop_after_attempt(self._settings.http_retry_attempts),
            wait=wait_exponential(
                multiplier=self._settings.http_retry_backoff_seconds,
                max=self._settings.http_retry_backoff_max_seconds,
            ),
            retry=retry_if_exception(_is_transient),
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    response = await self._http.get(url)
                    if allow_404 and response.status_code == 404:
                        return None
                    response.raise_for_status()
                    return response.json()
        except httpx.HTTPStatusError as exc:
            raise ArtifactUnavailable(
                f"GET {path} returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ArtifactUnavailable(f"GET {path} failed: {type(exc).__name__}") from exc
        except ValueError as exc:  # invalid JSON body
            raise ArtifactContractError(f"GET {path}: response is not JSON") from exc
        return None  # pragma: no cover - AsyncRetrying always returns or raises

    async def get_manifest(self, *, force_refresh: bool = False) -> Manifest:
        async def fetch() -> Dict[str, Any]:
            return _check_schema_version(await self._get_json("manifest.json"), "manifest.json")

        payload = await cached_call(
            self._key("manifest"),
            fetch,
            ttl_seconds=self._settings.manifest_cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        # Re-check: a cached value written by an older/newer deployment must
        # not slip past the version check either.
        payload = _check_schema_version(payload, "manifest.json")
        try:
            return Manifest.model_validate(payload)
        except ValidationError as exc:
            raise ArtifactContractError(f"manifest.json: {exc.error_count()} invalid field(s)") from exc

    async def get_gene(
        self, hugo_symbol: str, release_id: str, *, force_refresh: bool = False
    ) -> Optional[GeneArtifact]:
        """The gene file, or None when the stats repo has no file for the symbol."""
        path = f"genes/{hugo_symbol}.json"

        async def fetch() -> Dict[str, Any]:
            payload = await self._get_json(path, allow_404=True)
            if payload is None:
                return dict(_MISSING)
            return _check_schema_version(payload, path)

        payload = await cached_call(
            self._key("gene", release_id, hugo_symbol),
            fetch,
            ttl_seconds=self._settings.gene_cache_ttl_seconds,
            force_refresh=force_refresh,
        )
        if isinstance(payload, dict) and payload.get(next(iter(_MISSING))):
            return None
        payload = _check_schema_version(payload, path)
        try:
            return GeneArtifact.model_validate(payload)
        except ValidationError as exc:
            raise ArtifactContractError(f"{path}: {exc.error_count()} invalid field(s)") from exc

    async def get_aliases(self, release_id: str) -> Dict[str, str]:
        """Upper-cased alias -> canonical symbol (empty if aliases.json is absent)."""

        async def fetch() -> Dict[str, Any]:
            payload = await self._get_json("aliases.json", allow_404=True)
            if payload is None:
                return {}
            if not isinstance(payload, dict):
                raise ArtifactContractError("aliases.json: expected a JSON object")
            return payload

        payload = await cached_call(
            self._key("aliases", release_id),
            fetch,
            ttl_seconds=self._settings.gene_cache_ttl_seconds,
        )
        return {
            str(alias).strip().upper(): str(symbol).strip().upper()
            for alias, symbol in (payload or {}).items()
            if isinstance(symbol, str) and symbol.strip()
        }
