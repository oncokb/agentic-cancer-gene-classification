"""Curator overrides applied at read time.

A curator override (curated_overrides, see curation_store.py) replaces
sections of the pipeline's output for one subject: a gene annotation keyed
like gene_annotations (gene, tumor_type), or an exact fusion-pair evidence
result keyed by ('GENEA::GENEB', tumor_type). The curator's version always
wins, except that:

- retracted PMIDs are still stripped from curated content (same semantics as
  result_sanitizer.strip_retracted_pmids_from_annotation), with a flag; and
- when the pipeline now cites PMIDs absent from its evidence at curation time,
  a non-blocking `newer_pipeline_evidence` flag lists them.

Overrides are applied to what is served, never written into gene_annotations
or the runs table: runs are persisted pipeline-pure (result_payload_for_storage)
and re-overlaid with the CURRENT override when served, so shared links follow
later edits and reverts. A stored run's per-gene tumor types are recovered
from the run's request (restore_run_tumor_types).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from src.models.schema import (
    AnnotationResult,
    ClinicalActionability,
    ClinicalActionabilityEvidence,
    CuratedOverride,
    CuratedTherapeuticPrecedent,
    FusionCurationPayload,
    FusionEvidenceResult,
    GeneAnnotation,
    GeneCurationPayload,
    QualityFlag,
)
from src.pipeline.literature import find_retracted_pmids
from src.pipeline.normalization import FUSION_SEPARATORS, split_fusion
from src.pipeline.result_sanitizer import strip_retracted_pmids_from_annotation

logger = logging.getLogger(__name__)

GENE = "gene"
FUSION = "fusion"
SUBJECT_TYPES = (GENE, FUSION)

NEWER_EVIDENCE_FLAG = "newer_pipeline_evidence"
CURATED_RETRACTED_FLAG = "curated_retracted_pmids_removed"


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def normalize_tumor_type(tumor_type: Optional[str]) -> str:
    """Same normalization as the gene_annotations cache key ('' when none)."""
    return " ".join((tumor_type or "").strip().lower().split())


def normalize_subject_key(subject_type: str, subject_key: str) -> str:
    """Upper-cased gene symbol, or 'GENEA::GENEB' for a fusion (5' partner
    first; '--' and '/' separators are accepted). Raises ValueError."""
    raw = (subject_key or "").strip()
    if subject_type == GENE:
        if not raw or FUSION_SEPARATORS.search(raw):
            raise ValueError("Gene subject must be a single gene symbol")
        return raw.upper()
    if subject_type == FUSION:
        five_prime, three_prime = split_fusion(raw)
        if not five_prime or not three_prime:
            raise ValueError("Fusion subject must be GENEA::GENEB")
        return f"{five_prime.upper()}::{three_prime.upper()}"
    raise ValueError(f"Unknown subject type {subject_type!r}")


# ---------------------------------------------------------------------------
# PMIDs
# ---------------------------------------------------------------------------


def _dedupe(values: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(value for value in values if value))


def gene_evidence_pmids(annotation: GeneAnnotation) -> List[str]:
    """The pipeline's evidence PMIDs for a gene: citations, then card PMIDs."""
    return _dedupe([*annotation.citations, *(card.pmid for card in annotation.evidence_cards)])


def fusion_evidence_pmids(result: FusionEvidenceResult) -> List[str]:
    return _dedupe([*result.pmids, *(card.pmid for card in result.evidence_cards)])


def parse_payload(subject_type: str, payload: Dict[str, Any]) -> GeneCurationPayload | FusionCurationPayload:
    model = GeneCurationPayload if subject_type == GENE else FusionCurationPayload
    return model.model_validate(payload)


def payload_pmids(payload: GeneCurationPayload | FusionCurationPayload) -> List[str]:
    """Every PMID a curator payload references, in first-seen order."""
    if isinstance(payload, FusionCurationPayload):
        return _dedupe([*(payload.pmids or []), *(card.pmid for card in payload.evidence_cards or [])])
    pmids: List[str] = list(payload.citations or [])
    pmids.extend(card.pmid for card in payload.evidence_cards or [])
    pmids.extend(quote.pmid for quote in payload.supporting_quotes or [])
    if payload.therapeutic_precedent:
        pmids.extend(precedent.pmid for precedent in payload.therapeutic_precedent.precedents)
    return _dedupe(pmids)


def payload_fields(payload: GeneCurationPayload | FusionCurationPayload) -> List[str]:
    """Names of the sections the payload sets (non-None), in field order."""
    return [name for name in type(payload).model_fields if getattr(payload, name) is not None]


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------


def _add_flag(flags: List[QualityFlag], flag: QualityFlag) -> None:
    if not any(existing.code == flag.code for existing in flags):
        flags.append(flag)


def _newer_evidence(
    pipeline_pmids: Sequence[str],
    override: CuratedOverride,
    curated_pmids: Set[str],
    retracted_pmids: Set[str],
) -> List[str]:
    if override.pipeline_pmids_snapshot is None:
        return []
    snapshot = set(override.pipeline_pmids_snapshot)
    return [
        pmid
        for pmid in pipeline_pmids
        if pmid not in snapshot and pmid not in curated_pmids and pmid not in retracted_pmids
    ]


def _newer_evidence_flag(pmids: List[str]) -> QualityFlag:
    return QualityFlag(
        code=NEWER_EVIDENCE_FLAG,
        label="Newer pipeline evidence since curation",
        severity="warning",
        detail=(
            "The pipeline now cites PMID(s) that were not in its evidence when this was curated: "
            f"{', '.join(pmids)}. Consider reviewing the curated version."
        ),
    )


def _curated_retracted_flag(pmids: List[str]) -> QualityFlag:
    return QualityFlag(
        code=CURATED_RETRACTED_FLAG,
        label="Retracted PMIDs removed from curated content",
        severity="critical",
        detail=f"Removed retracted PMID(s) from the curator's version: {', '.join(pmids)}.",
    )


# ---------------------------------------------------------------------------
# Apply (pure)
# ---------------------------------------------------------------------------


def _curated_actionability(
    precedent: CuratedTherapeuticPrecedent, override: CuratedOverride
) -> Optional[ClinicalActionability]:
    if not precedent.precedents:
        return None
    return ClinicalActionability(
        confidence_score=1.0,
        confidence_level="high",
        summary=precedent.summary or "Curator-verified therapeutic precedent.",
        confidence_explanation=(
            f"Curator-verified by {override.curator_email} on {override.updated_at[:10]}; "
            "replaces the automated literature-keyword score."
        ),
        pmids=[item.pmid for item in precedent.precedents],
        evidence=[
            ClinicalActionabilityEvidence(
                pmid=item.pmid,
                title=item.title,
                evidence_type=item.evidence_type,
                therapies=item.therapies,
                quote=item.quote,
                abstract=item.abstract,
            )
            for item in precedent.precedents
        ],
        curated=True,
        curated_by=override.curator_email,
        curated_at=override.updated_at,
    )


def apply_override(
    annotation: GeneAnnotation,
    override: CuratedOverride,
    retracted_pmids: Optional[Set[str]] = None,
) -> GeneAnnotation:
    """Return a copy of a pipeline gene annotation with `override` applied.

    `retracted_pmids` are stripped from the result (curated content included).
    The input is left untouched and kept as the copy's pipeline original."""
    retracted = set(retracted_pmids or ())
    payload = parse_payload(GENE, override.payload)
    assert isinstance(payload, GeneCurationPayload)
    pipeline = annotation._pipeline_annotation or annotation
    pipeline_pmids = gene_evidence_pmids(pipeline)
    curated = pipeline.model_copy(deep=True)

    if payload.cancer_association_rationale is not None:
        curated.cancer_association_rationale = payload.cancer_association_rationale
    if payload.citations is not None:
        curated.citations = list(payload.citations)
        curated.insufficient_evidence = not payload.citations
        if payload.citations:
            curated.quality_flags = [
                flag for flag in curated.quality_flags if flag.code != "no_verified_citations"
            ]
    if payload.evidence_cards is not None:
        curated.evidence_cards = [card.model_copy() for card in payload.evidence_cards]
    if payload.supporting_quotes is not None:
        curated.supporting_quotes = [quote.model_copy() for quote in payload.supporting_quotes]
    if payload.therapeutic_precedent is not None:
        curated.clinical_actionability = _curated_actionability(payload.therapeutic_precedent, override)

    curated.curated = True
    curated.curated_by = override.curator_email
    curated.curated_at = override.updated_at
    curated.curation_version = override.version
    curated.curated_fields = payload_fields(payload)
    curated._pipeline_annotation = pipeline

    curated_pmids = set(payload_pmids(payload))
    removed = sorted(curated_pmids & retracted)
    if retracted:
        strip_retracted_pmids_from_annotation(curated, retracted)
    if removed:
        _add_flag(curated.quality_flags, _curated_retracted_flag(removed))

    newer = _newer_evidence(pipeline_pmids, override, curated_pmids, retracted)
    if newer:
        _add_flag(curated.quality_flags, _newer_evidence_flag(newer))
    return curated


def apply_fusion_override(
    result: FusionEvidenceResult,
    override: CuratedOverride,
    retracted_pmids: Optional[Set[str]] = None,
) -> FusionEvidenceResult:
    """Return a copy of a pipeline fusion evidence result with `override` applied."""
    retracted = set(retracted_pmids or ())
    payload = parse_payload(FUSION, override.payload)
    assert isinstance(payload, FusionCurationPayload)
    pipeline_pmids = fusion_evidence_pmids(result)
    curated = result.model_copy(deep=True)

    if payload.interpretation is not None:
        curated.interpretation = payload.interpretation
    if payload.well_supported is not None:
        curated.well_supported = payload.well_supported
    if payload.pmids is not None:
        curated.pmids = list(payload.pmids)
        curated.retrieved_count = len(curated.pmids)
    if payload.evidence_cards is not None:
        curated.evidence_cards = [
            card.model_copy(update={"fusion": result.fusion}) for card in payload.evidence_cards
        ]

    curated.curated = True
    curated.curated_by = override.curator_email
    curated.curated_at = override.updated_at
    curated.curation_version = override.version
    curated.curated_fields = payload_fields(payload)

    curated_pmids = set(payload_pmids(payload))
    removed = sorted(curated_pmids & retracted)
    if retracted:
        curated.pmids = [pmid for pmid in curated.pmids if pmid not in retracted]
        curated.evidence_cards = [card for card in curated.evidence_cards if card.pmid not in retracted]
        curated.retrieved_count = len(curated.pmids)
    if removed:
        _add_flag(curated.quality_flags, _curated_retracted_flag(removed))

    newer = _newer_evidence(pipeline_pmids, override, curated_pmids, retracted)
    if newer:
        _add_flag(curated.quality_flags, _newer_evidence_flag(newer))
    return curated


# ---------------------------------------------------------------------------
# Apply current overrides (store lookups; fail open)
# ---------------------------------------------------------------------------


async def _lookup_overrides(
    run_store: Any, subject_type: str, keys: List[Tuple[str, str]]
) -> Dict[Tuple[str, str, str], CuratedOverride]:
    getter = getattr(run_store, "get_curated_overrides", None) if run_store is not None else None
    if getter is None or not keys:
        return {}
    try:
        return await getter(subject_type, keys)
    except Exception:
        # Serving pipeline output beats failing the request; logged loudly.
        logger.exception("Curated override lookup failed; serving pipeline output")
        return {}


async def _retracted_among(overrides: Iterable[CuratedOverride], subject_type: str) -> Set[str]:
    pmids = _dedupe(
        pmid
        for override in overrides
        for pmid in payload_pmids(parse_payload(subject_type, override.payload))
    )
    if not pmids:
        return set()
    try:
        return await find_retracted_pmids(pmids)
    except Exception:
        # PMIDs were validated as non-retracted when the curator saved.
        logger.warning("Retraction check for curated PMIDs failed", exc_info=True)
        return set()


def _gene_key(annotation: GeneAnnotation, tumor_type: Optional[str]) -> Optional[Tuple[str, str]]:
    try:
        return normalize_subject_key(GENE, annotation.gene), normalize_tumor_type(tumor_type)
    except ValueError:
        return None


async def apply_current_gene_overrides(
    annotations: List[GeneAnnotation],
    run_store: Any,
    tumor_types: Optional[Sequence[Optional[str]]] = None,
) -> List[GeneAnnotation]:
    """Overlay each annotation's current override (if any), in order.

    tumor_types aligns with annotations; it defaults to each annotation's
    analysis_tumor_type. Pipeline retraction stripping must already be done."""
    if tumor_types is None:
        tumor_types = [annotation.analysis_tumor_type for annotation in annotations]
    keys = [_gene_key(annotation, tt) for annotation, tt in zip(annotations, tumor_types)]
    overrides = await _lookup_overrides(run_store, GENE, [key for key in keys if key])
    if not overrides:
        return list(annotations)
    retracted = await _retracted_among(overrides.values(), GENE)
    applied: List[GeneAnnotation] = []
    for annotation, key in zip(annotations, keys):
        override = overrides.get((GENE, *key)) if key else None
        if override is None:
            applied.append(annotation)
            continue
        try:
            applied.append(apply_override(annotation, override, retracted))
        except Exception:
            logger.exception("Failed to apply curated override for %s", annotation.gene)
            applied.append(annotation)
    return applied


async def apply_current_gene_override(
    annotation: GeneAnnotation, run_store: Any, tumor_type: Optional[str]
) -> GeneAnnotation:
    return (await apply_current_gene_overrides([annotation], run_store, [tumor_type]))[0]


async def apply_current_fusion_overrides(
    results: List[FusionEvidenceResult], run_store: Any
) -> List[FusionEvidenceResult]:
    """Overlay each fusion evidence result's current override (if any), in order."""
    keys: List[Optional[Tuple[str, str]]] = []
    for result in results:
        try:
            keys.append((normalize_subject_key(FUSION, result.fusion), normalize_tumor_type(result.tumor_type)))
        except ValueError:
            keys.append(None)
    overrides = await _lookup_overrides(run_store, FUSION, [key for key in keys if key])
    if not overrides:
        return list(results)
    retracted = await _retracted_among(overrides.values(), FUSION)
    applied: List[FusionEvidenceResult] = []
    for result, key in zip(results, keys):
        override = overrides.get((FUSION, *key)) if key else None
        if override is None:
            applied.append(result)
            continue
        try:
            applied.append(apply_fusion_override(result, override, retracted))
        except Exception:
            logger.exception("Failed to apply curated override for fusion %s", result.fusion)
            applied.append(result)
    return applied


def preserve_curated_fields(enriched: GeneAnnotation, original: GeneAnnotation) -> GeneAnnotation:
    """Carry an input annotation's curated sections through lazy enrichment,
    which rebuilds annotations from scratch and would otherwise drop them."""
    if not original.curated:
        return enriched
    update: Dict[str, Any] = {
        name: getattr(original, name)
        for name in ("curated", "curated_by", "curated_at", "curation_version", "curated_fields")
    }
    for name in original.curated_fields:
        if name == "therapeutic_precedent":
            update["clinical_actionability"] = original.clinical_actionability
        elif name in GeneAnnotation.model_fields:
            update[name] = getattr(original, name)
    if "citations" in original.curated_fields:
        update["insufficient_evidence"] = original.insufficient_evidence
    curation_codes = {NEWER_EVIDENCE_FLAG, CURATED_RETRACTED_FLAG}
    update["quality_flags"] = [
        *(flag for flag in enriched.quality_flags if flag.code not in curation_codes),
        *(flag for flag in original.quality_flags if flag.code in curation_codes),
    ]
    return enriched.model_copy(update=update)


# ---------------------------------------------------------------------------
# Run persistence (pipeline-pure)
# ---------------------------------------------------------------------------


def result_payload_for_storage(result: AnnotationResult) -> Dict[str, Any]:
    """result.model_dump() with curated annotations swapped back to their
    pipeline originals, so the runs table only ever holds pipeline output."""
    payload = result.model_dump()
    for index, annotation in enumerate(result.annotations):
        pipeline = annotation._pipeline_annotation
        if pipeline is not None:
            payload["annotations"][index] = pipeline.model_dump()
    return payload


def _tumor_type_from_request(annotation: GeneAnnotation, request: Optional[Dict[str, Any]]) -> Optional[str]:
    """The pipeline's rule for a gene's tumor type, replayed on a saved
    request: the first tumor type on an input naming this gene (or one of its
    fusions). Inputs given as an alias of the canonical symbol don't match."""
    if not request:
        return None
    if "fusions" not in request:
        return request.get("tumor_type")  # GeneAnnotateRequest
    gene = annotation.gene.upper()
    names = {gene, *(fusion.upper() for fusion in annotation.fusions)}
    for item in request.get("fusions") or []:
        if not isinstance(item, dict) or not item.get("tumor_type"):
            continue
        value = str(item.get("fusion") or "").strip().upper()
        if value in names or gene in split_fusion(value):
            return item["tumor_type"]
    return None


async def restore_run_tumor_types(result: AnnotationResult, run_store: Any, run_id: str) -> None:
    """Set analysis_tumor_type on a stored run's annotations (in place)."""
    getter = getattr(run_store, "get_run_request", None)
    if getter is None or not result.annotations:
        return
    try:
        request = await getter(run_id)
    except Exception:
        logger.warning("Failed to load request for run %s", run_id, exc_info=True)
        return
    for annotation in result.annotations:
        annotation.analysis_tumor_type = _tumor_type_from_request(annotation, request)
