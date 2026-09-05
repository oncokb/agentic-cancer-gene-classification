"""
OpenEvidence supplementary evidence lookup.

Off by default (settings.openevidence_enabled). When enabled, this posts a
gene (+ optional tumor type) question to OpenEvidence's streaming analysis
endpoint and returns accumulated prose plus a deduplicated citation list.

This is a SUPPLEMENTARY input to synthesis, not a LiteratureRecord
replacement: OpenEvidence citations are not guaranteed to be PMIDs in the
retrieved literature set, so they must never be passed through
src.pipeline.citation_precision.filter_and_rank_citations or merged into
GeneAnnotation.citations. Callers must always surface this output as clearly
labeled unverified/supplementary evidence.
"""

from __future__ import annotations

import json
import logging
from typing import Dict, List, Optional

import httpx
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import OpenEvidenceAnalysis, OpenEvidenceCitation
from src.pipeline.cache import _get_client, cached_call

logger = logging.getLogger(__name__)

STREAMING_ANALYSIS_PATH = "/streaming/analysis"

# HTTP statuses worth retrying: request timeout, rate limit, and 5xx. Any other
# 4xx (e.g. 401 bad API key, 404) can never succeed on retry, so fail fast.
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429})


class OpenEvidenceConfigurationError(RuntimeError):
    """Raised when OpenEvidence lookups are requested without required configuration."""


def _is_transient_openevidence_error(exc: BaseException) -> bool:
    """Retry predicate: network/connection errors and 408/429/5xx are
    transient and worth retrying. A permanent 4xx (e.g. 401 bad API key) can
    never succeed on retry, so it fails fast instead of burning the retry
    budget.

    A read/connect/pool timeout is deliberately NOT retried either, even
    though it's "transient" in the usual sense: a live-verified smoke test
    against the real API took ~220s and still hadn't finished a single
    moderately complex clinical question. Retrying a slow-but-functioning
    server would only multiply an already multi-minute wait for what is
    meant to be a quick, best-effort supplementary lookup — a timeout is
    treated as "no supplementary evidence available this time", the same
    normal, non-alarming outcome as any other best-effort lookup failure
    (see orchestrator.py's _maybe_fetch_openevidence_context).
    """
    if isinstance(exc, httpx.TimeoutException):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRYABLE_HTTP_STATUSES or status >= 500
    if isinstance(exc, httpx.HTTPError):
        # Any other HTTPError subclass here is a connection/network failure
        # (HTTPStatusError and TimeoutException are already handled above),
        # always transient — e.g. a dropped/reset connection mid-stream,
        # which is how an incomplete stream actually surfaces against the
        # real API (there is no application-level completion sentinel to
        # check for; see _post_streaming_analysis).
        return True
    return False


def _cache_key(gene: str, tumor_type: Optional[str] = None) -> str:
    """Shared cache-key derivation, used both to fetch/store a live analysis
    (OpenEvidenceClient.get_gene_analysis) and to passively peek whether one
    already exists (has_cached_analysis), so the two paths can never drift
    apart on key format."""
    return "openevidence:" + json.dumps(
        {
            "gene": gene.strip().upper(),
            "tumor_type": (tumor_type or "").strip().lower(),
            "model": settings.openevidence_model,
        },
        sort_keys=True,
    )


async def has_cached_analysis(gene: str, tumor_type: Optional[str] = None) -> bool:
    """Whether a Redis cache entry already exists for this gene/tumor_type —
    a passive peek that makes no live HTTP call and needs no API key.

    Used by the gene-annotation reuse/staleness check (see
    orchestrator.py's _maybe_reuse_cached_annotation) to detect when
    OpenEvidence data has become available since a stored annotation was
    last synthesized without it — e.g. via benchmarks/warm_openevidence_cache.py,
    or a slow live call that finished after that gene's synthesis had
    already proceeded without it. Fails closed (returns False) if Redis is
    unreachable, consistent with this being a best-effort supplementary
    signal, never something that should block or error out a cache read.
    """
    key = _cache_key(gene, tumor_type)
    try:
        return bool(await _get_client().exists(key))
    except Exception as exc:
        logger.warning("OpenEvidence cache existence check failed for %s: %s", gene, exc)
        return False


def _build_question(gene: str, tumor_type: Optional[str] = None) -> str:
    tumor_note = f" in {tumor_type}" if tumor_type else ""
    return (
        f"What does the peer-reviewed evidence show about {gene}'s role in "
        f"cancer{tumor_note}? Summarize the key clinical and molecular evidence."
    )


def _iter_sse_payloads(raw: str) -> List[str]:
    """Split an SSE stream body into raw per-event data payloads.

    Each event is a blank-line-separated block containing one or more
    `data:` lines. Per the SSE spec, multiple `data:` lines within one event
    are joined with "\\n" between them (NOT concatenated directly — plain
    concatenation can corrupt JSON payload semantics when a single JSON
    payload is split across lines).
    """
    payloads: List[str] = []
    for block in raw.replace("\r\n", "\n").split("\n\n"):
        data_lines = [
            line[len("data:"):].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        payloads.append("\n".join(data_lines))
    return payloads


def _parse_sse_events(raw: str) -> List[dict]:
    """Parse an SSE stream body into a list of JSON event payloads.

    There is no application-level stream-termination sentinel in the real
    OpenEvidence API (confirmed absent from both the official docs and a
    live-captured response) — completion is signalled entirely by the HTTP
    response body ending normally, which the transport layer (httpx) is
    responsible for detecting; see _post_streaming_analysis. Malformed
    payloads are skipped rather than failing the whole parse, since a single
    bad delta shouldn't discard everything accumulated so far.
    """
    events: List[dict] = []
    for payload in _iter_sse_payloads(raw):
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed OpenEvidence SSE payload: %r", payload[:200])
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _citation_from_event(event: dict) -> Optional[OpenEvidenceCitation]:
    """Extract citation metadata from a citation-bearing event.

    Confirmed against the real API (official docs + a live-captured
    response): citation data is nested under event["reference"], with
    bibliographic fields a further level deep under
    event["reference"]["reference_detail"] — NOT flat top-level event
    fields. citation_key is an integer on the wire; cast to str for
    OpenEvidenceCitation.citation_key. journal_name is preferred over the
    abbreviated journal_short_name when both are present.
    """
    reference = event.get("reference")
    if not isinstance(reference, dict):
        return None
    citation_key = reference.get("citation_key")
    if citation_key is None:
        return None
    detail = reference.get("reference_detail")
    if not isinstance(detail, dict):
        detail = {}
    source_texts = reference.get("source_texts") or []
    return OpenEvidenceCitation(
        citation_key=str(citation_key),
        title=detail.get("title") or "",
        authors=detail.get("authors_string") or "",
        journal=detail.get("journal_name") or detail.get("journal_short_name") or "",
        date=detail.get("publication_date") or "",
        doi=detail.get("doi") or "",
        url=detail.get("url") or "",
        source_texts=[text for text in source_texts if text],
    )


def _build_analysis(question: str, events: List[dict]) -> OpenEvidenceAnalysis:
    """Accumulate prose text and dedupe citations across all events.

    Per the official docs, "concatenating together the text fields from the
    data messages will produce the full analysis text" — this includes
    citation-bearing events, whose `text` is typically an inline marker like
    "[[1]]", not just plain message events. So every event's `text` is
    appended when present, citation or not.

    `table` events (top-level key "table") are a v1 limitation: they
    represent the full current state of a table, not prose or a PMID-style
    citation, and this supplementary-text integration has no rendering for
    them. They're intentionally and silently ignored here — no crash, no
    attempt to flatten tabular content into text, nothing added to
    `citations` either. A future iteration could add real table rendering.
    """
    text_parts: List[str] = []
    citations_by_key: Dict[str, OpenEvidenceCitation] = {}

    for event in events:
        if "table" in event:
            continue

        delta = event.get("text")
        if delta:
            text_parts.append(delta)

        citation = _citation_from_event(event)
        if citation is not None:
            existing = citations_by_key.get(citation.citation_key)
            if existing is None:
                citations_by_key[citation.citation_key] = citation
            else:
                merged_source_texts = list(
                    dict.fromkeys(existing.source_texts + citation.source_texts)
                )
                citations_by_key[citation.citation_key] = existing.model_copy(
                    update={"source_texts": merged_source_texts}
                )

    return OpenEvidenceAnalysis(
        question=question,
        text="".join(text_parts),
        citations=list(citations_by_key.values()),
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_transient_openevidence_error),
)
async def _post_streaming_analysis(question: str, api_key: str, client: httpx.AsyncClient) -> str:
    base_url = settings.openevidence_base_url.strip().rstrip("/")
    url = f"{base_url}{STREAMING_ANALYSIS_PATH}"
    headers = {
        "Authorization": f"Token {api_key}",
        "Accept": "text/event-stream",
    }
    payload = {"text": question, "model": settings.openevidence_model}

    # No [DONE]-style completion sentinel exists in the real API — a stream
    # that ends because the connection was dropped/reset mid-response raises
    # from within aiter_text() itself (an httpx transport exception), which
    # the retry predicate above already treats as transient. A stream that
    # finishes this loop without raising is, by definition, complete.
    async with client.stream(
        "POST", url, json=payload, headers=headers, timeout=settings.openevidence_timeout_seconds
    ) as response:
        response.raise_for_status()
        chunks = [chunk async for chunk in response.aiter_text()]
    return "".join(chunks)


class OpenEvidenceClient:
    """OpenEvidence streaming-analysis client with an explicit shared cache."""

    def __init__(self, api_key: Optional[str] = None) -> None:
        self.api_key = api_key if api_key is not None else settings.openevidence_api_key

    async def get_gene_analysis(
        self,
        gene: str,
        tumor_type: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> OpenEvidenceAnalysis:
        """Return a supplementary, unverified OpenEvidence analysis for `gene`.

        Raises OpenEvidenceConfigurationError if no API key is configured.
        Pass a shared httpx.AsyncClient (e.g. for tests) or one will be
        created and closed for this call.
        """
        if not self.api_key:
            raise OpenEvidenceConfigurationError(
                "OPENEVIDENCE_API_KEY is required when OPENEVIDENCE_ENABLED is true"
            )

        question = _build_question(gene, tumor_type)
        cache_key = _cache_key(gene, tumor_type)

        async def _compute() -> dict:
            if client is not None:
                raw = await _post_streaming_analysis(question, self.api_key, client)
            else:
                async with httpx.AsyncClient() as owned_client:
                    raw = await _post_streaming_analysis(question, self.api_key, owned_client)
            events = _parse_sse_events(raw)
            return _build_analysis(question, events).model_dump()

        try:
            payload = await cached_call(
                cache_key, _compute, ttl_seconds=settings.openevidence_cache_ttl_seconds
            )
        except (httpx.HTTPError, RetryError) as exc:
            # cached_call only caches a successful compute() result (see
            # src.pipeline.cache) — an exception here (including a
            # retry-exhausted transient failure, or a fail-fast timeout) is
            # never cached.
            logger.error("OpenEvidence lookup failed for %s: %s", gene, exc)
            raise
        return OpenEvidenceAnalysis(**payload)
