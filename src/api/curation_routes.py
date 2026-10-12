"""Curator override API: read, save, revert, and audit curated overrides.

Subjects are a gene annotation (`gene`, keyed like gene_annotations by gene
and tumor type) or an exact fusion-pair evidence result (`fusion`, keyed by
'GENEA::GENEB' and tumor type). Reads need any login; writes need the curator
or admin role (require_curator). See src/pipeline/curation.py for how
overrides are applied wherever results are served.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Dict, List, Literal, Optional, Tuple, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.auth import AuthenticatedUser, require_auth, require_curator
from src.models.schema import (
    CuratedOverride,
    CurationHistoryEntry,
    FusionCurationPayload,
    GeneAnnotation,
    GeneCurationPayload,
    LiteratureRecord,
)
from src.observability import record_user_action
from src.pipeline import literature
from src.pipeline.curation import (
    GENE,
    fusion_evidence_pmids,
    gene_evidence_pmids,
    normalize_subject_key,
    normalize_tumor_type,
    parse_payload,
    payload_fields,
    payload_pmids,
)
from src.pipeline.curation_store import CurationStorageContention, CurationVersionConflict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/curation", tags=["curation"])

T = TypeVar("T")

SubjectTypeParam = Literal["gene", "fusion"]

# Upper bound on the PubMed call that snapshots fusion evidence on save; a
# cache miss would otherwise run a full fusion retrieval inside the request.
_FUSION_SNAPSHOT_TIMEOUT_SECONDS = 20.0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CurationSaveRequest(BaseModel):
    """Body of PUT /v1/curation/{subject_type}/{subject_key}."""

    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(
        default=0,
        ge=0,
        description="Version the curator edited (from GET); 0 when creating. 409 when stale.",
    )
    payload: Dict[str, Any] = Field(
        ...,
        description=(
            "GeneCurationPayload for genes, FusionCurationPayload for fusions. Omitted/null "
            "sections keep the pipeline value."
        ),
    )
    note: Optional[str] = Field(default=None, max_length=2000, description="Optional note kept in history.")


class CurationResponse(BaseModel):
    subject_type: SubjectTypeParam
    subject_key: str
    tumor_type: str
    version: int = Field(description="Current override version; 0 when there is no override.")
    override: Optional[CuratedOverride] = None


class CurationHistoryResponse(BaseModel):
    subject_type: SubjectTypeParam
    subject_key: str
    tumor_type: str
    entries: List[CurationHistoryEntry] = Field(default_factory=list, description="Newest first.")


class CurationRevertResponse(BaseModel):
    subject_type: SubjectTypeParam
    subject_key: str
    tumor_type: str
    reverted_version: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _subject(subject_type: str, subject_key: str, tumor_type: Optional[str]) -> Tuple[str, str]:
    try:
        return normalize_subject_key(subject_type, subject_key), normalize_tumor_type(tumor_type)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


_STORE_UNAVAILABLE = "Curation storage is unavailable; please retry."


async def _store_call(awaitable: Awaitable[T]) -> T:
    """Await a curation store call; storage errors become 503 (never a silent
    'no override'). Version conflicts propagate for the caller to map to 409."""
    try:
        return await awaitable
    except CurationVersionConflict:
        raise
    except CurationStorageContention as exc:
        logger.warning("Curation write gave up on lock contention: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Curation storage contention; please retry.",
            headers={"Retry-After": "1"},
        ) from exc
    except Exception as exc:
        logger.error("Curation store call failed", exc_info=True)
        raise HTTPException(status_code=503, detail=_STORE_UNAVAILABLE) from exc


def _store(http_request: Request) -> Any:
    store = getattr(http_request.app.state, "run_store", None)
    if store is None or not hasattr(store, "get_curated_overrides"):
        raise HTTPException(status_code=503, detail=_STORE_UNAVAILABLE)
    return store


async def _fetch_pubmed(pmids: List[str]) -> Dict[str, LiteratureRecord]:
    try:
        records = await literature._efetch(pmids, literature._get_ncbi_client(), filter_retracted=False)
    except Exception as exc:
        logger.exception("PubMed validation failed for curated PMIDs")
        raise HTTPException(status_code=503, detail="PubMed is unavailable; please retry.") from exc
    return {record.pmid: record for record in records}


def _invalid_pmids(pmids: List[str], records: Dict[str, LiteratureRecord]) -> List[Dict[str, str]]:
    invalid = []
    for pmid in pmids:
        record = records.get(pmid)
        if record is not None and literature._is_retracted_publication(record):
            invalid.append({"pmid": pmid, "reason": "retracted"})
        elif record is None or not record.abstract.strip():
            # efetch drops records without an abstract, so the two look alike.
            invalid.append({"pmid": pmid, "reason": "not_found_or_no_abstract"})
    return invalid


def _fill_from_pubmed(
    payload: GeneCurationPayload | FusionCurationPayload,
    records: Dict[str, LiteratureRecord],
    subject_key: str,
) -> None:
    """Overwrite PubMed-derived fields server-side, whatever the client sent."""
    for card in payload.evidence_cards or []:
        record = records[card.pmid]
        card.title = record.title
        card.journal = record.journal
        card.publication_types = list(record.publication_types)
        card.is_preprint = literature.is_preprint_publication(record.publication_types)
        card.abstract = record.abstract
    if isinstance(payload, FusionCurationPayload):
        for card in payload.evidence_cards or []:
            card.fusion = subject_key
            card.matched_via_alias = False
            card.alias_matches = []
    elif payload.therapeutic_precedent:
        for precedent in payload.therapeutic_precedent.precedents:
            record = records[precedent.pmid]
            precedent.title = record.title
            precedent.abstract = record.abstract


async def _pipeline_pmids_snapshot(
    store: Any, subject_type: str, subject_key: str, tumor_type: str
) -> Optional[List[str]]:
    """The pipeline's current evidence PMIDs (None when unknown)."""
    try:
        if subject_type == GENE:
            cached = await store.get_gene_annotation(subject_key, tumor_type=tumor_type)
            if cached is None:
                return None
            return gene_evidence_pmids(GeneAnnotation(**cached["annotation"]))
        result = await asyncio.wait_for(
            literature.retrieve_fusion_evidence(subject_key, tumor_type=tumor_type or None),
            timeout=_FUSION_SNAPSHOT_TIMEOUT_SECONDS,
        )
        return fusion_evidence_pmids(result)
    except Exception:
        logger.warning("Could not snapshot pipeline PMIDs for %s %s", subject_type, subject_key, exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{subject_type}/{subject_key}", response_model=CurationResponse)
async def get_curation(
    http_request: Request,
    subject_type: SubjectTypeParam = Path(..., description="`gene` or `fusion`."),
    subject_key: str = Path(..., description="Gene symbol, or fusion as `GENEA::GENEB`."),
    tumor_type: Optional[str] = Query(default=None, description="Tumor type context; omit for none."),
    current_user: AuthenticatedUser = Depends(require_auth),
) -> CurationResponse:
    """The current curator override for a subject, if any."""
    key, tt = _subject(subject_type, subject_key, tumor_type)
    override = await _store_call(_store(http_request).get_curated_override(subject_type, key, tt))
    return CurationResponse(
        subject_type=subject_type,
        subject_key=key,
        tumor_type=tt,
        version=override.version if override else 0,
        override=override,
    )


@router.put("/{subject_type}/{subject_key}", response_model=CurationResponse)
async def save_curation(
    request: CurationSaveRequest,
    http_request: Request,
    subject_type: SubjectTypeParam = Path(...),
    subject_key: str = Path(...),
    tumor_type: Optional[str] = Query(default=None),
    current_user: AuthenticatedUser = Depends(require_curator),
) -> CurationResponse:
    """Save a curator override. PMIDs must exist in PubMed with an abstract and
    not be retracted (422 lists offenders); 409 when expected_version is stale."""
    key, tt = _subject(subject_type, subject_key, tumor_type)
    store = _store(http_request)
    try:
        payload = parse_payload(subject_type, request.payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422, detail=exc.errors(include_url=False, include_context=False)
        ) from exc
    sections = payload_fields(payload)
    if not sections:
        raise HTTPException(
            status_code=422, detail="Override sets no sections; use DELETE to revert to pipeline output."
        )

    pmids = payload_pmids(payload)
    records = await _fetch_pubmed(pmids) if pmids else {}
    invalid = _invalid_pmids(pmids, records)
    if invalid:
        raise HTTPException(
            status_code=422,
            detail={"message": "Some PMIDs cannot be used as curated evidence.", "invalid_pmids": invalid},
        )
    _fill_from_pubmed(payload, records, key)

    snapshot = await _pipeline_pmids_snapshot(store, subject_type, key, tt)
    try:
        override = await _store_call(
            store.save_curated_override(
                subject_type=subject_type,
                subject_key=key,
                tumor_type=tt,
                payload=payload.model_dump(mode="json", exclude_none=True),
                curator_email=current_user.email,
                expected_version=request.expected_version,
                pipeline_pmids_snapshot=snapshot,
                note=request.note,
            )
        )
    except CurationVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "This override was changed by someone else; reload and reapply your edits.",
                "current_version": exc.current_version,
            },
        ) from exc

    record_user_action(
        current_user.email,
        action="curation_save",
        details={
            "subject_type": subject_type,
            "subject_key": key,
            "tumor_type": tt,
            "version": override.version,
            "sections": ",".join(sections),
            "pmids_count": len(pmids),
        },
    )
    return CurationResponse(
        subject_type=subject_type, subject_key=key, tumor_type=tt, version=override.version, override=override
    )


@router.delete("/{subject_type}/{subject_key}", response_model=CurationRevertResponse)
async def revert_curation(
    http_request: Request,
    subject_type: SubjectTypeParam = Path(...),
    subject_key: str = Path(...),
    tumor_type: Optional[str] = Query(default=None),
    expected_version: Optional[int] = Query(default=None, ge=1, description="409 when stale, if given."),
    note: Optional[str] = Query(default=None, max_length=2000),
    current_user: AuthenticatedUser = Depends(require_curator),
) -> CurationRevertResponse:
    """Revert a subject to pipeline output; the revert is recorded in history."""
    key, tt = _subject(subject_type, subject_key, tumor_type)
    store = _store(http_request)
    try:
        reverted = await _store_call(
            store.delete_curated_override(
                subject_type=subject_type,
                subject_key=key,
                tumor_type=tt,
                curator_email=current_user.email,
                expected_version=expected_version,
                note=note,
            )
        )
    except CurationVersionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={"message": "This override was changed by someone else.", "current_version": exc.current_version},
        ) from exc
    if reverted is None:
        raise HTTPException(status_code=404, detail="No curated override to revert.")
    record_user_action(
        current_user.email,
        action="curation_revert",
        details={"subject_type": subject_type, "subject_key": key, "tumor_type": tt, "version": reverted},
    )
    return CurationRevertResponse(
        subject_type=subject_type, subject_key=key, tumor_type=tt, reverted_version=reverted
    )


@router.get("/{subject_type}/{subject_key}/history", response_model=CurationHistoryResponse)
async def get_curation_history(
    http_request: Request,
    subject_type: SubjectTypeParam = Path(...),
    subject_key: str = Path(...),
    tumor_type: Optional[str] = Query(default=None),
    current_user: AuthenticatedUser = Depends(require_auth),
) -> CurationHistoryResponse:
    """Every save and revert for a subject, newest first."""
    key, tt = _subject(subject_type, subject_key, tumor_type)
    entries = await _store_call(_store(http_request).list_curation_history(subject_type, key, tt))
    return CurationHistoryResponse(subject_type=subject_type, subject_key=key, tumor_type=tt, entries=entries)

