"""
OpenEvidence supplementary evidence lookup.

Off by default (settings.openevidence_enabled). When enabled, this posts a
closed, targeted question — gene classification (+ optional tumor type), or
fusion-partner oncogenicity when the gene is part of a fusion — to
OpenEvidence's streaming analysis endpoint and returns accumulated prose plus
a deduplicated citation list.

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
import re
from typing import Dict, List, Optional

import httpx
from tenacity import RetryError, retry, retry_if_exception, stop_after_attempt, wait_exponential

from src.config import settings
from src.models.schema import (
    DistilledOpenEvidence,
    OpenEvidenceAnalysis,
    OpenEvidenceCitation,
    OpenEvidenceGuideline,
    OpenEvidenceTrialMention,
)
from src.pipeline.cache import _get_client, cached_call
from src.pipeline.normalization import split_fusion

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


def _refresh_attempt_key(gene: str, tumor_type: Optional[str] = None) -> str:
    return "openevidence:refresh_attempt:" + json.dumps(
        {"gene": gene.strip().upper(), "tumor_type": (tumor_type or "").strip().lower()},
        sort_keys=True,
    )


async def mark_refresh_attempted(gene: str, tumor_type: Optional[str] = None) -> None:
    """Record that a freshness-triggered refresh was just attempted for this
    gene/tumor_type, so the gene-annotation reuse check won't re-trigger
    another one until OPENEVIDENCE_REFRESH_COOLDOWN_SECONDS has passed —
    even if the refresh attempt itself never ends up populating
    openevidence_supplementary (e.g. a downstream synthesis error unrelated
    to OpenEvidence skips persisting the refreshed annotation entirely).
    Recorded via a self-expiring Redis key rather than a persisted
    annotation field, since the cooldown must apply regardless of whether
    the refreshed annotation gets persisted at all.
    """
    key = _refresh_attempt_key(gene, tumor_type)
    try:
        await _get_client().set(key, "1", ex=settings.openevidence_refresh_cooldown_seconds)
    except Exception as exc:
        logger.warning("Failed to record OpenEvidence refresh attempt for %s: %s", gene, exc)


async def was_refresh_recently_attempted(gene: str, tumor_type: Optional[str] = None) -> bool:
    """Whether a freshness-triggered refresh was attempted for this
    gene/tumor_type within the cooldown window. Fails closed (returns
    False, i.e. "safe to attempt") if Redis is unreachable — consistent
    with has_cached_analysis, this is a best-effort signal, not something
    that should itself block a cache read.
    """
    key = _refresh_attempt_key(gene, tumor_type)
    try:
        return bool(await _get_client().exists(key))
    except Exception as exc:
        logger.warning("OpenEvidence refresh-attempt check failed for %s: %s", gene, exc)
        return False


def _build_question(gene: str, tumor_type: Optional[str] = None, fusion: Optional[str] = None) -> str:
    """Build a closed, targeted question aimed at the one content type our
    own PubMed-abstract-only retrieval structurally cannot ever produce:
    clinical practice guideline (NCCN/ASCO/ESMO) and trial-level treatment
    evidence, rather than re-asking the oncogene/tumor-suppressor
    classification our own synthesis prompt already derives from retrieved
    abstracts.

    This replaces an earlier classification question ("is GENE an oncogene
    or tumor suppressor... state the classification") — see
    benchmarks/openevidence_value_report.md and
    benchmarks/openevidence_vendor_assessment.md on the
    agcg-openevidence-vendor-assessment branch. That benchmark found two
    things: (1) on genes our own retrieval already supported well (BRAF,
    EGFR, KRAS, TP53, BRCA1, ALK), OpenEvidence's verified-citation count was
    literally unchanged (4→4) — the classification question was redundant
    with what our own pipeline already does; (2) OpenEvidence's citation set
    included an NCCN guideline reference for 10 of 16 genes regardless of
    how well our own retrieval already supported the gene — content
    genuinely orthogonal to a PubMed-abstract corpus, since guidelines
    aren't indexed in PubMed at all. Asking directly for that content (guidelines/
    trials), instead of a classification restatement, concentrates the
    130-250s call on the evidence type it has demonstrated unique value for.

    `fusion` (a raw "GENE1::GENE2"-style input string, see
    normalization.is_fusion_input) asks about guideline/trial evidence for
    the fusion itself rather than each partner gene in isolation.

    `tumor_type`, when present, replaces the generic "cancer" context rather
    than being appended after it (avoiding an awkward "...in cancer in
    breast cancer?" double-up) — tumor_type names are already
    cancer-specific (e.g. "breast cancer", "melanoma", "NSCLC").
    """
    cancer_context = tumor_type if tumor_type else "cancer"
    if fusion:
        gene1, gene2 = split_fusion(fusion)
        if gene1 and gene2:
            return (
                "What NCCN, ASCO, or ESMO clinical practice guideline "
                "recommendations or clinical trial evidence address targeted "
                f"therapy for the {gene1}::{gene2} fusion in {cancer_context}? "
                "Cite the specific guideline or trial."
            )
    return (
        "What NCCN, ASCO, or ESMO clinical practice guideline recommendations "
        f"or clinical trial evidence address targeted therapy for {gene} "
        f"alterations in {cancer_context}? Cite the specific guideline or trial."
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


# Domains that identify a citation as a clinical practice guideline rather
# than a journal article, per Task 2's distillation spec.
_GUIDELINE_URL_DOMAINS = ("nccn.org", "asco.org", "esmo.org")

# Non-exhaustive seed list of well-known trial acronyms — a sentence naming
# one of these OR an outcome statistic (PFS/OS/HR/ORR/DFS) is surfaced as a
# trial mention even without a recognized acronym, so this list only needs
# to catch named trials that don't otherwise report a statistic in the same
# sentence.
_KNOWN_TRIAL_ACRONYMS = (
    "ALEX", "FLAURA", "ADAURA", "CROWN", "J-ALEX", "ALTA", "ALTA-1L",
    "ASCEND", "PROFILE", "PALOMA", "MONALEESA", "KEYNOTE", "CHECKMATE",
    "IMPOWER", "OAK", "FLEX", "eXalt3", "LIBRETTO", "ARROW",
)
_TRIAL_ACRONYM_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in _KNOWN_TRIAL_ACRONYMS) + r")\b"
)
_OUTCOME_STAT_PATTERN = re.compile(r"\b(PFS|OS|HR|ORR|DFS)\b")
_CITATION_MARKER_PATTERN = re.compile(r"\[\[\d+\]\]")
_SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> List[str]:
    cleaned = _CITATION_MARKER_PATTERN.sub("", text)
    return [sentence.strip() for sentence in _SENTENCE_SPLIT_PATTERN.split(cleaned) if sentence.strip()]


def _extract_guidelines(citations: List[OpenEvidenceCitation]) -> List[OpenEvidenceGuideline]:
    """Clinical practice guideline references — citations whose URL points
    at NCCN, ASCO, or ESMO, per Task 2's distillation spec."""
    guidelines: List[OpenEvidenceGuideline] = []
    for citation in citations:
        url = citation.url or ""
        if not url or not any(domain in url.lower() for domain in _GUIDELINE_URL_DOMAINS):
            continue
        page_anchor = url.split("#", 1)[1] if "#" in url else None
        guidelines.append(
            OpenEvidenceGuideline(
                title=citation.title or citation.citation_key,
                url=url,
                page_anchor=page_anchor or None,
            )
        )
    return guidelines


def _extract_trial_mentions(text: str) -> List[OpenEvidenceTrialMention]:
    """Sentences naming a known clinical trial acronym or an outcome
    statistic (PFS/OS/HR/ORR/DFS), per Task 2's distillation spec."""
    mentions: List[OpenEvidenceTrialMention] = []
    for sentence in _split_sentences(text):
        trial_match = _TRIAL_ACRONYM_PATTERN.search(sentence)
        if trial_match is None and _OUTCOME_STAT_PATTERN.search(sentence) is None:
            continue
        mentions.append(
            OpenEvidenceTrialMention(
                trial=trial_match.group(1) if trial_match else None,
                sentence=sentence,
            )
        )
    return mentions


def distill_openevidence(analysis: OpenEvidenceAnalysis) -> DistilledOpenEvidence:
    """Deterministically extract the pieces of an OpenEvidenceAnalysis worth
    surfacing as an independent, non-blocking clinical reference card:
    clinical practice guideline references, trial/outcome-statistic
    sentences, and the opening summary sentence (`consensus_role`) — now a
    guideline/trial-recommendation lead-in rather than an oncogene/tumor-
    suppressor classification statement, since _build_question no longer
    asks for the latter (our own synthesis already derives it from retrieved
    abstracts). Purely rule-based (regex over analysis.text/citations, no
    LLM call) — safe to run synchronously inside the sidecar endpoint's
    request path (see GET /v1/genes/{gene}/openevidence in main.py).
    """
    sentences = _split_sentences(analysis.text)
    return DistilledOpenEvidence(
        question=analysis.question,
        consensus_role=sentences[0] if sentences else None,
        guidelines=_extract_guidelines(analysis.citations),
        trial_mentions=_extract_trial_mentions(analysis.text),
        citation_count=len(analysis.citations),
    )


# Domains that identify a citation as coming from a trial registry rather
# than a journal article. Distinct from _GUIDELINE_URL_DOMAINS (clinical
# practice guidelines) but grouped with it below since both are content our
# own PubMed-abstract-only retrieval structurally cannot produce, regardless
# of whether it overlaps anything the core pipeline already found.
_TRIAL_REGISTRY_URL_DOMAINS = ("clinicaltrials.gov",)

# A citation whose URL matches any of these domains is "non-PubMed-sourced":
# real-world OpenEvidence citation samples (benchmarks/results/
# openevidence_pointed_20260908/enabled.json) show every other citation —
# regardless of whether its URL happens to be a pubmed.ncbi.nlm.nih.gov link,
# a doi.org redirect, or a publisher site like nejm.org/wiley — is a
# PubMed-indexable journal article: the kind of content our own retrieval
# could in principle have found, even if it happened not to for this
# specific paper. Only guideline/trial-registry domains name a source type
# retrieval can never reach at all.
_NON_PUBMED_SOURCE_URL_DOMAINS = _GUIDELINE_URL_DOMAINS + _TRIAL_REGISTRY_URL_DOMAINS

# Matches a PMID out of a pubmed.ncbi.nlm.nih.gov citation URL, e.g.
# "https://pubmed.ncbi.nlm.nih.gov/38211832" -> "38211832". This is the only
# reliable, regex-extractable PMID signal on OpenEvidenceCitation today (no
# dedicated pmid field exists on the model).
_PUBMED_CITATION_URL_PATTERN = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d+)")

_TITLE_NORMALIZATION_PATTERN = re.compile(r"[^a-z0-9]+")


def _normalize_title_for_matching(title: str) -> str:
    """Lowercase and strip all non-alphanumeric characters so titles that
    differ only in punctuation/whitespace/case still compare equal."""
    return _TITLE_NORMALIZATION_PATTERN.sub("", title.lower())


def is_non_pubmed_sourced_citation(citation: OpenEvidenceCitation) -> bool:
    """Whether `citation` is sourced from a clinical practice guideline
    (NCCN/ASCO/ESMO) or a trial registry rather than a PubMed-indexable
    journal article — content our own PubMed-abstract-only retrieval
    structurally cannot ever produce, regardless of gene or overlap with
    the core pipeline's own citations. See module-level
    _NON_PUBMED_SOURCE_URL_DOMAINS for the grounding of this classification."""
    url = (citation.url or "").lower()
    return bool(url) and any(domain in url for domain in _NON_PUBMED_SOURCE_URL_DOMAINS)


def _citation_overlaps_core_pipeline_evidence(
    citation: OpenEvidenceCitation, core_pmids: frozenset, core_titles: frozenset
) -> bool:
    """Whether `citation` strongly matches something the core pipeline's own
    GeneAnnotation already surfaced (its verified PMID citations or
    evidence_cards), by PMID (extracted from a pubmed.ncbi.nlm.nih.gov URL)
    or by normalized title. Either signal alone is enough — OpenEvidence's
    citation_key is not a PMID, so PMID is only recoverable from the URL,
    and not every citation has a pubmed.ncbi.nlm.nih.gov-shaped URL even when
    it is the same paper the core pipeline already cited via PMID."""
    match = _PUBMED_CITATION_URL_PATTERN.search(citation.url or "")
    if match is not None and match.group(1) in core_pmids:
        return True
    normalized_title = _normalize_title_for_matching(citation.title or "")
    return bool(normalized_title) and normalized_title in core_titles


def is_additive_citation(
    citation: OpenEvidenceCitation, core_pmids: frozenset, core_titles: frozenset
) -> bool:
    """Whether `citation` is worth surfacing in the sidecar at all: a
    guideline/trial-registry citation always is (our own retrieval
    structurally cannot produce that content); a PubMed-sourced (journal
    article) citation only is when it doesn't overlap the core pipeline's
    own verified citations/evidence_cards for this gene — otherwise showing
    it adds vendor-call latency for content the curator already has."""
    if is_non_pubmed_sourced_citation(citation):
        return True
    return not _citation_overlaps_core_pipeline_evidence(citation, core_pmids, core_titles)


def distill_additive_openevidence(
    analysis: OpenEvidenceAnalysis,
    core_pmids: Optional[List[str]] = None,
    core_titles: Optional[List[str]] = None,
) -> DistilledOpenEvidence:
    """Like distill_openevidence, but first drops citations that are
    redundant with the core pipeline's own PubMed-derived evidence for this
    gene — `core_pmids` (GeneAnnotation.citations) and `core_titles`
    (GeneAnnotation.evidence_cards titles), passed in by the caller (see
    GET /v1/genes/{gene}/openevidence in main.py, which has the
    already-computed GeneAnnotation in hand for the same gene).

    Only affects `guidelines` and `citation_count` (both derived from
    analysis.citations) plus the new `redundant_citation_count` (how many
    citations were dropped). `consensus_role` and `trial_mentions` are
    derived from analysis.text, not analysis.citations, so are unaffected —
    see is_additive_citation's docstring for why a guideline/trial-registry
    citation is always additive regardless of overlap.
    """
    normalized_pmids = frozenset(pmid.strip() for pmid in (core_pmids or []) if pmid and pmid.strip())
    normalized_titles = frozenset(
        _normalize_title_for_matching(title) for title in (core_titles or []) if title and title.strip()
    )
    additive_citations = [
        citation
        for citation in analysis.citations
        if is_additive_citation(citation, normalized_pmids, normalized_titles)
    ]
    redundant_count = len(analysis.citations) - len(additive_citations)
    filtered_analysis = analysis.model_copy(update={"citations": additive_citations})
    distilled = distill_openevidence(filtered_analysis)
    return distilled.model_copy(update={"redundant_citation_count": redundant_count})


def distilled_openevidence_has_additive_content(distilled: DistilledOpenEvidence) -> bool:
    """Whether `distilled` has anything genuinely worth rendering as its own
    card: a guideline reference, a trial/outcome-statistic mention, or at
    least one additive (non-redundant) citation. Used by the sidecar
    endpoint to return {"available": false} instead of a card with nothing
    in it — see distill_additive_openevidence."""
    return bool(distilled.guidelines or distilled.trial_mentions or distilled.citation_count)


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
        fusion: Optional[str] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> OpenEvidenceAnalysis:
        """Return a supplementary, unverified OpenEvidence analysis for `gene`.

        `fusion`, when the gene is part of a fusion (a raw "GENE1::GENE2"
        input string — see orchestrator.py's _annotate_gene), makes the
        question fusion-specific via _build_question instead of asking about
        `gene` in isolation.

        A genuine cache hit is returned regardless of whether an API key is
        configured — a Redis cache entry existing does not depend on THIS
        process being able to make a new live call (it may have been warmed
        by benchmarks/warm_openevidence_cache.py, or by a different process
        that had a key configured). OpenEvidenceConfigurationError is only
        raised on an actual cache MISS, when a live HTTP call is genuinely
        about to be made and therefore genuinely needs a key. Pass a shared
        httpx.AsyncClient (e.g. for tests) or one will be created and closed
        for this call.

        The cache key intentionally still derives only from gene/tumor_type/
        model (see _cache_key), not from `fusion` — cache-key shape is
        explicitly out of scope for this question-text change.
        """
        question = _build_question(gene, tumor_type, fusion=fusion)
        cache_key = _cache_key(gene, tumor_type)

        async def _compute() -> dict:
            # Only reached on a cache miss (cached_call checks Redis first) —
            # this is where a live call is genuinely about to happen, so
            # this is where the API key actually matters.
            if not self.api_key:
                raise OpenEvidenceConfigurationError(
                    "OPENEVIDENCE_API_KEY is required when OPENEVIDENCE_ENABLED is true"
                )
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
        except (httpx.HTTPError, RetryError, OpenEvidenceConfigurationError) as exc:
            # cached_call only caches a successful compute() result (see
            # src.pipeline.cache) — an exception here (including a
            # retry-exhausted transient failure, a fail-fast timeout, or a
            # missing-API-key cache miss) is never cached.
            logger.error("OpenEvidence lookup failed for %s: %s", gene, exc)
            raise
        return OpenEvidenceAnalysis(**payload)
