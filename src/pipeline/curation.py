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
later edits and reverts. Job storage likewise keeps pipeline output and
overlays on every poll. Each run stores the tumor type the pipeline used per
gene (AnnotationResult.annotation_tumor_types); older runs replay it from
their request (ensure_annotation_tumor_types).

If overrides cannot be loaded, results are served as pipeline output with a
critical `curation_unavailable` flag rather than silently.
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
from src.pipeline.normalization import FUSION_SEPARATORS, normalize_fusions, split_fusion
from src.pipeline.result_sanitizer import strip_retracted_pmids_from_annotation

logger = logging.getLogger(__name__)

GENE = "gene"
FUSION = "fusion"
SUBJECT_TYPES = (GENE, FUSION)

NEWER_EVIDENCE_FLAG = "newer_pipeline_evidence"
CURATED_RETRACTED_FLAG = "curated_retracted_pmids_removed"
CURATION_UNAVAILABLE_FLAG = "curation_unavailable"


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
    actionability = curated.clinical_actionability
    if actionability is not None and actionability.curated and not actionability.pmids:
        # Every curator-verified precedent was retracted: drop the 1.0-confidence
        # card rather than show it unsupported (the critical flag explains why).
        curated.clinical_actionability = None
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
# Apply current overrides (store lookups)
# ---------------------------------------------------------------------------


def _curation_unavailable_flag() -> QualityFlag:
    return QualityFlag(
        code=CURATION_UNAVAILABLE_FLAG,
        label="Curator edits unavailable",
        severity="critical",
        detail="Curator edits could not be loaded; showing pipeline output.",
    )


def has_curation_unavailable(items: Iterable[GeneAnnotation | FusionEvidenceResult]) -> bool:
    return any(flag.code == CURATION_UNAVAILABLE_FLAG for item in items for flag in item.quality_flags)


def pipeline_original(annotation: GeneAnnotation) -> GeneAnnotation:
    """The pure pipeline annotation behind a served (overlaid) one."""
    return annotation._pipeline_annotation or annotation


async def _lookup_overrides(
    run_store: Any, subject_type: str, keys: List[Tuple[str, str]]
) -> Tuple[Dict[Tuple[str, str, str], CuratedOverride], bool]:
    """(overrides by subject, lookup_failed). A store without curation support
    (e.g. the benchmark store) is not a failure: it simply has no overrides."""
    getter = getattr(run_store, "get_curated_overrides", None) if run_store is not None else None
    if getter is None or not keys:
        return {}, False
    try:
        return await getter(subject_type, list(dict.fromkeys(keys))), False
    except Exception:
        logger.error(
            "Curated override lookup failed for %d %s subject(s); serving pipeline output flagged %s",
            len(keys), subject_type, CURATION_UNAVAILABLE_FLAG, exc_info=True,
        )
        return {}, True


async def _retracted_among(overrides: Iterable[CuratedOverride], subject_type: str) -> Set[str]:
    """One deduplicated retraction check across every override in a batch."""
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


def _flag_gene_unavailable(annotation: GeneAnnotation) -> GeneAnnotation:
    flagged = annotation.model_copy(deep=True)
    _add_flag(flagged.quality_flags, _curation_unavailable_flag())
    flagged._pipeline_annotation = annotation
    return flagged


async def apply_current_gene_overrides(
    annotations: List[GeneAnnotation],
    run_store: Any,
    tumor_types: Optional[Sequence[Optional[str]]] = None,
) -> List[GeneAnnotation]:
    """Overlay each annotation's CURRENT override (if any), in order, with one
    batched lookup and one retraction check. Inputs are never mutated; an
    already-overlaid annotation is re-overlaid from its pipeline original.

    tumor_types aligns with annotations and defaults to each annotation's
    analysis_tumor_type. Pipeline retraction stripping must already be done."""
    originals = [pipeline_original(annotation) for annotation in annotations]
    if tumor_types is None:
        tumor_types = [annotation.analysis_tumor_type for annotation in originals]
    keys = [_gene_key(annotation, tt) for annotation, tt in zip(originals, tumor_types)]
    overrides, failed = await _lookup_overrides(run_store, GENE, [key for key in keys if key])
    if failed:
        return [_flag_gene_unavailable(a) if key else a for a, key in zip(originals, keys)]
    if not overrides:
        return originals
    retracted = await _retracted_among(overrides.values(), GENE)
    applied: List[GeneAnnotation] = []
    for annotation, key in zip(originals, keys):
        override = overrides.get((GENE, *key)) if key else None
        if override is None:
            applied.append(annotation)
            continue
        try:
            applied.append(apply_override(annotation, override, retracted))
        except Exception:
            logger.error("Failed to apply curated override for %s", annotation.gene, exc_info=True)
            applied.append(_flag_gene_unavailable(annotation))
    return applied


def _flag_fusion_unavailable(result: FusionEvidenceResult) -> FusionEvidenceResult:
    flagged = result.model_copy(deep=True)
    _add_flag(flagged.quality_flags, _curation_unavailable_flag())
    return flagged


async def apply_current_fusion_overrides(
    results: List[FusionEvidenceResult], run_store: Any
) -> List[FusionEvidenceResult]:
    """Overlay each pipeline fusion evidence result's CURRENT override (if any)."""
    keys: List[Optional[Tuple[str, str]]] = []
    for result in results:
        try:
            keys.append((normalize_subject_key(FUSION, result.fusion), normalize_tumor_type(result.tumor_type)))
        except ValueError:
            keys.append(None)
    overrides, failed = await _lookup_overrides(run_store, FUSION, [key for key in keys if key])
    if failed:
        return [_flag_fusion_unavailable(r) if key else r for r, key in zip(results, keys)]
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
            logger.error("Failed to apply curated override for fusion %s", result.fusion, exc_info=True)
            applied.append(_flag_fusion_unavailable(result))
    return applied


def _annotation_tumor_types(result: AnnotationResult) -> List[Optional[str]]:
    return [
        result.annotation_tumor_types[annotation.gene]
        if annotation.gene in result.annotation_tumor_types
        else pipeline_original(annotation).analysis_tumor_type
        for annotation in result.annotations
    ]


async def overlay_annotation_result(result: AnnotationResult, run_store: Any) -> AnnotationResult:
    """A copy of a (pipeline) result with the CURRENT overrides applied to its
    gene annotations and fusion evidence, and curation_unavailable set."""
    annotations = await apply_current_gene_overrides(
        result.annotations, run_store, _annotation_tumor_types(result)
    )
    fusion_evidence = await apply_current_fusion_overrides(result.fusion_evidence, run_store)
    return result.model_copy(
        update={
            "annotations": annotations,
            "fusion_evidence": fusion_evidence,
            "curation_unavailable": has_curation_unavailable(annotations)
            or has_curation_unavailable(fusion_evidence),
        }
    )


# ---------------------------------------------------------------------------
# Pipeline-pure results (runs table, job storage)
# ---------------------------------------------------------------------------


def pipeline_result(result: AnnotationResult) -> AnnotationResult:
    """A copy of `result` holding only pipeline output (no overrides/flags)."""
    return result.model_copy(
        update={
            "annotations": [pipeline_original(annotation) for annotation in result.annotations],
            "curation_unavailable": False,
        }
    )


def result_payload_for_storage(result: AnnotationResult) -> Dict[str, Any]:
    """result.model_dump() with curated annotations swapped back to their
    pipeline originals, so the runs table only ever holds pipeline output."""
    return pipeline_result(result).model_dump()


def _request_inputs(request: Dict[str, Any]) -> List[Tuple[str, Optional[str]]]:
    if "fusions" not in request:  # GeneAnnotateRequest
        return [(str(request.get("gene") or ""), request.get("tumor_type"))]
    inputs = []
    for item in request.get("fusions") or []:
        if isinstance(item, str):
            inputs.append((item, None))
        elif isinstance(item, dict):
            inputs.append((str(item.get("fusion") or ""), item.get("tumor_type")))
    return inputs


def _direct_tumor_type(gene: str, fusions: List[str], inputs: List[Tuple[str, Optional[str]]]) -> Optional[str]:
    gene = gene.upper()
    names = {gene, *(fusion.upper() for fusion in fusions)}
    for value, tumor_type in inputs:
        value = value.strip().upper()
        if tumor_type and (value in names or gene in split_fusion(value)):
            return tumor_type
    return None


async def ensure_annotation_tumor_types(result: AnnotationResult, run_store: Any, run_id: str) -> None:
    """Fill result.annotation_tumor_types (in place) for runs saved before it
    existed, by replaying the pipeline's rule on the saved request: HGNC-
    normalize the inputs (so aliases map to their canonical gene) and take the
    first tumor type among each gene's inputs."""
    missing = [a for a in result.annotations if a.gene not in result.annotation_tumor_types]
    getter = getattr(run_store, "get_run_request", None)
    if not missing or getter is None:
        return
    try:
        request = await getter(run_id)
    except Exception:
        logger.warning("Failed to load request for run %s", run_id, exc_info=True)
        return
    inputs = _request_inputs(request or {})
    tumor_by_input: Dict[str, str] = {}
    for value, tumor_type in inputs:
        if tumor_type:
            tumor_by_input.setdefault(value, tumor_type)
    gene_map: Dict[str, Any] = {}
    if tumor_by_input:
        try:
            gene_map = await normalize_fusions([value for value, _ in inputs])
        except Exception:
            logger.warning("Alias resolution failed for run %s; matching inputs literally", run_id, exc_info=True)
    for annotation in missing:
        if annotation.gene in gene_map:
            _, gene_inputs = gene_map[annotation.gene]
            tumor_type = next((tumor_by_input[v] for v in gene_inputs if v in tumor_by_input), None)
        else:
            tumor_type = _direct_tumor_type(annotation.gene, annotation.fusions, inputs)
        result.annotation_tumor_types[annotation.gene] = tumor_type
