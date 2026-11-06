"""GitHub Releases client: discover statistics bundles and download their assets.

Contract (expression_stats/docs/contract.md, "Release transport and atomicity"):
bundles are non-draft, non-prerelease releases whose tag starts with
``expression-stats-``; the newest by ``published_at`` wins. Assets:
``manifest.json``, ``genes.jsonl.gz`` and (optionally) ``aliases.json``.

Rate limits: unauthenticated GitHub API calls are limited to 60/h per IP. Only
*discovery* uses the API (``api.github.com``); asset downloads go to
``browser_download_url`` (github.com → its CDN), which is not API-metered.
403/429 responses are reported as :class:`RateLimited` with the time GitHub
says to retry, and are never retried here. ``EXPRESSION_CONTEXT_GITHUB_TOKEN``
(optional) is sent to the API host only, never to download hosts.
"""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, AsyncIterator, Dict, List, Optional
from urllib.parse import quote, urlsplit

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from src.expression_context.settings import BUNDLE_TAG_PREFIX, ExpressionContextSettings

logger = logging.getLogger(__name__)

USER_AGENT = "acgc-expression-context"
_PER_PAGE = 100
_DEFAULT_RATE_LIMIT_BACKOFF = timedelta(hours=1)
_NEXT_LINK = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


class ReleasesError(RuntimeError):
    """GitHub could not be queried (after retries) or answered unexpectedly."""


class TransientReleasesError(ReleasesError):
    """A download failed in a way worth retrying (transport error, HTTP 5xx)."""


class DiscoveryIncomplete(ReleasesError):
    """The page cap was reached before the end of the release list, so the
    newest bundle can't be known; callers must not activate anything."""


class RateLimited(ReleasesError):
    """GitHub refused with 403/429; do not call again before ``retry_at``."""

    def __init__(self, message: str, retry_at: datetime) -> None:
        super().__init__(message)
        self.retry_at = retry_at


@dataclass(frozen=True)
class ReleaseAsset:
    name: str
    size: int
    download_url: str


@dataclass(frozen=True)
class BundleRelease:
    tag: str
    published_at: Optional[datetime]
    html_url: Optional[str] = None
    assets: Dict[str, ReleaseAsset] = field(default_factory=dict)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def rate_limit_retry_at(response: httpx.Response, now: Optional[datetime] = None) -> datetime:
    """When GitHub says the next request may be made (Retry-After, then X-RateLimit-Reset)."""
    now = now or _utcnow()
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return now + timedelta(seconds=max(0.0, float(retry_after)))
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    reset = response.headers.get("x-ratelimit-reset")
    if reset:
        try:
            return max(now, datetime.fromtimestamp(int(reset), tz=timezone.utc))
        except (ValueError, OverflowError, OSError):
            pass
    return now + _DEFAULT_RATE_LIMIT_BACKOFF


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, TransientReleasesError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return False


def parse_release(payload: Dict[str, Any]) -> Optional[BundleRelease]:
    """A bundle release from one GitHub release object, or None if it isn't one."""
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith(BUNDLE_TAG_PREFIX):
        return None
    if payload.get("draft") or payload.get("prerelease"):
        return None
    assets: Dict[str, ReleaseAsset] = {}
    for asset in payload.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        name, url, size = asset.get("name"), asset.get("browser_download_url"), asset.get("size")
        if isinstance(name, str) and isinstance(url, str) and url.startswith("https://"):
            assets[name] = ReleaseAsset(
                name=name, size=int(size) if isinstance(size, int) else -1, download_url=url
            )
    return BundleRelease(
        tag=tag,
        published_at=_parse_time(payload.get("published_at")),
        html_url=payload.get("html_url") if isinstance(payload.get("html_url"), str) else None,
        assets=assets,
    )


def newest_bundle(releases: List[BundleRelease]) -> Optional[BundleRelease]:
    published = [release for release in releases if release.published_at is not None]
    if not published:
        return None
    return max(published, key=lambda release: (release.published_at, release.tag))


class ReleasesClient:
    def __init__(
        self,
        settings: ExpressionContextSettings,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._settings = settings
        self._api = settings.github_api_url
        self._api_host = urlsplit(self._api).netloc
        self._repo = settings.releases_repo
        self._owns_client = http_client is None
        self._http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                settings.http_timeout_seconds, connect=settings.http_connect_timeout_seconds
            ),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http.aclose()

    # ------------------------------------------------------------ API calls

    def _api_headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": USER_AGENT,
        }
        token = self._settings.github_token
        if token is not None:
            headers["Authorization"] = f"Bearer {token.get_secret_value()}"
        return headers

    def _retrying(self) -> AsyncRetrying:
        return AsyncRetrying(
            stop=stop_after_attempt(self._settings.http_retry_attempts),
            wait=wait_exponential(
                multiplier=self._settings.http_retry_backoff_seconds,
                max=self._settings.http_retry_backoff_max_seconds,
            ),
            retry=retry_if_exception(_is_transient),
            reraise=True,
        )

    async def _api_get(self, url: str, *, allow_404: bool = False) -> Optional[httpx.Response]:
        if urlsplit(url).netloc != self._api_host:
            raise ReleasesError(f"refusing to send API request to {urlsplit(url).netloc}")
        try:
            async for attempt in self._retrying():
                with attempt:
                    response = await self._http.get(url, headers=self._api_headers())
                    if response.status_code in (403, 429):
                        raise RateLimited(
                            f"GitHub API returned HTTP {response.status_code}",
                            rate_limit_retry_at(response),
                        )
                    if allow_404 and response.status_code == 404:
                        return None
                    response.raise_for_status()
                    return response
        except RateLimited:
            raise
        except httpx.HTTPStatusError as exc:
            raise ReleasesError(f"GitHub API returned HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ReleasesError(f"GitHub API request failed: {type(exc).__name__}") from exc
        return None  # pragma: no cover - AsyncRetrying always returns or raises

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ReleasesError("GitHub API response is not JSON") from exc

    async def latest_bundle(self) -> Optional[BundleRelease]:
        """Newest published bundle release, or None if the repo has none.

        GitHub lists releases by creation, but the contract picks the newest
        by ``published_at``, so every page is read (at most
        ``max_release_pages`` API calls). If the list goes on past the cap,
        :class:`DiscoveryIncomplete` is raised rather than guessing.
        """
        url: Optional[str] = f"{self._api}/repos/{self._repo}/releases?per_page={_PER_PAGE}"
        bundles: List[BundleRelease] = []
        pages = 0
        while url is not None:
            if pages >= self._settings.max_release_pages:
                raise DiscoveryIncomplete(
                    f"release list continues past {pages} pages "
                    "(EXPRESSION_CONTEXT_MAX_RELEASE_PAGES); discovery incomplete"
                )
            response = await self._api_get(url)
            pages += 1
            payload = self._json(response)
            if not isinstance(payload, list):
                raise ReleasesError("GitHub release list is not a JSON array")
            bundles.extend(
                release
                for release in (parse_release(item) for item in payload if isinstance(item, dict))
                if release is not None
            )
            match = _NEXT_LINK.search(response.headers.get("link", ""))
            url = match.group(1) if match else None
        return newest_bundle(bundles)

    async def bundle_by_tag(self, tag: str) -> Optional[BundleRelease]:
        response = await self._api_get(
            f"{self._api}/repos/{self._repo}/releases/tags/{quote(tag, safe='')}", allow_404=True
        )
        if response is None:
            return None
        payload = self._json(response)
        if not isinstance(payload, dict):
            raise ReleasesError("GitHub release is not a JSON object")
        return parse_release(payload)

    # ------------------------------------------------------------- downloads

    @asynccontextmanager
    async def stream_asset(self, asset: ReleaseAsset) -> AsyncIterator[AsyncIterator[bytes]]:
        """Stream an asset's bytes (no Authorization header; follows the CDN redirect)."""
        try:
            async with self._http.stream(
                "GET",
                asset.download_url,
                headers={"Accept": "application/octet-stream", "User-Agent": USER_AGENT},
            ) as response:
                if response.status_code in (403, 429):
                    raise RateLimited(
                        f"asset {asset.name} download returned HTTP {response.status_code}",
                        rate_limit_retry_at(response),
                    )
                if response.status_code != 200:
                    error = (
                        TransientReleasesError
                        if response.status_code >= 500
                        else ReleasesError
                    )
                    raise error(
                        f"asset {asset.name} download returned HTTP {response.status_code}"
                    )
                yield response.aiter_bytes()
        except httpx.TransportError as exc:
            raise TransientReleasesError(
                f"asset {asset.name} download failed: {type(exc).__name__}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ReleasesError(
                f"asset {asset.name} download failed: {type(exc).__name__}"
            ) from exc

    async def fetch_json_asset(self, asset: ReleaseAsset, *, max_bytes: int) -> Any:
        if asset.size > max_bytes:
            raise ReleasesError(f"asset {asset.name} is {asset.size} bytes (limit {max_bytes})")
        body = bytearray()
        async for attempt in self._retrying():
            with attempt:
                body.clear()
                async with self.stream_asset(asset) as chunks:
                    async for chunk in chunks:
                        body += chunk
                        if len(body) > max_bytes:
                            raise ReleasesError(
                                f"asset {asset.name} exceeds {max_bytes} bytes"
                            )
        try:
            return json.loads(bytes(body))
        except ValueError as exc:
            raise ReleasesError(f"asset {asset.name} is not valid JSON") from exc
