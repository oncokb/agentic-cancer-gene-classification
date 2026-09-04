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
from src.pipeline.cache import cached_call

logger = logging.getLogger(__name__)

STREAMING_ANALYSIS_PATH = "/streaming/analysis"
_DONE_MARKER = "[DONE]"

# HTTP statuses worth retrying: request timeout, rate limit, and 5xx. Any other
# 4xx (e.g. 401 bad API key, 404) can never succeed on retry, so fail fast.
_RETRYABLE_HTTP_STATUSES = frozenset({408, 429})


class OpenEvidenceConfigurationError(RuntimeError):
    """Raised when OpenEvidence lookups are requested without required configuration."""


class OpenEvidenceIncompleteStreamError(RuntimeError):
    """Raised when an OpenEvidence SSE stream ends without a [DONE] marker —
    e.g. a dropped connection mid-response. Never treated as a successful
    result: callers must retry rather than cache a partial/empty output."""


def _is_transient_openevidence_error(exc: BaseException) -> bool:
    """Retry predicate: only network/connection errors and 408/429/5xx are
    transient. A permanent 4xx (e.g. 401 bad API key) can never succeed on
    retry, so it must fail fast instead of burning the retry budget."""
    if isinstance(exc, OpenEvidenceIncompleteStreamError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in _RETRYABLE_HTTP_STATUSES or status >= 500
    if isinstance(exc, httpx.HTTPError):
        # Any other HTTPError subclass here is a connection/timeout/network
        # failure (HTTPStatusError is already handled above), always transient.
        return True
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


def _stream_saw_done_marker(raw: str) -> bool:
    """Whether this SSE stream body actually reached its [DONE] terminator.

    A stream that never emits [DONE] (e.g. a connection dropped mid-response)
    must never be treated as a complete, cacheable result.
    """
    return any(payload == _DONE_MARKER for payload in _iter_sse_payloads(raw))


def _parse_sse_events(raw: str) -> List[dict]:
    """Parse an SSE stream body into a list of JSON event payloads.

    A payload of exactly "[DONE]" ends the stream. Malformed payloads are
    skipped rather than failing the whole parse, since a single bad delta
    shouldn't discard everything accumulated so far.
    """
    events: List[dict] = []
    for payload in _iter_sse_payloads(raw):
        if payload == _DONE_MARKER:
            break
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            logger.warning("Skipping malformed OpenEvidence SSE payload: %r", payload[:200])
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _citation_from_event(event: dict) -> Optional[OpenEvidenceCitation]:
    citation_key = event.get("citation_key")
    if not citation_key:
        return None
    source_texts = event.get("source_texts")
    if not source_texts:
        reference_text = event.get("reference_text")
        source_texts = [reference_text] if reference_text else []
    return OpenEvidenceCitation(
        citation_key=str(citation_key),
        title=event.get("title") or "",
        authors=event.get("authors") or "",
        journal=event.get("journal") or "",
        date=event.get("date") or "",
        doi=event.get("doi") or "",
        url=event.get("url") or "",
        source_texts=[text for text in source_texts if text],
    )


def _build_analysis(question: str, events: List[dict]) -> OpenEvidenceAnalysis:
    text_parts: List[str] = []
    citations_by_key: Dict[str, OpenEvidenceCitation] = {}

    for event in events:
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
            continue
        delta = event.get("text")
        if delta:
            text_parts.append(delta)

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

    async with client.stream(
        "POST", url, json=payload, headers=headers, timeout=settings.openevidence_timeout_seconds
    ) as response:
        response.raise_for_status()
        chunks = [chunk async for chunk in response.aiter_text()]
    raw = "".join(chunks)

    if not _stream_saw_done_marker(raw):
        raise OpenEvidenceIncompleteStreamError(
            "OpenEvidence stream ended without a [DONE] marker "
            "(connection dropped mid-response) — refusing to treat as complete"
        )
    return raw


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
        cache_key = "openevidence:" + json.dumps(
            {
                "gene": gene.strip().upper(),
                "tumor_type": (tumor_type or "").strip().lower(),
                "model": settings.openevidence_model,
            },
            sort_keys=True,
        )

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
        except (httpx.HTTPError, OpenEvidenceIncompleteStreamError, RetryError) as exc:
            # cached_call only caches a successful compute() result (see
            # src.pipeline.cache) — an exception here, including a retry-
            # exhausted incomplete stream, is never cached.
            logger.error("OpenEvidence lookup failed for %s: %s", gene, exc)
            raise
        return OpenEvidenceAnalysis(**payload)
