"""Curator overrides applied at read time.

A curator override (curated_overrides, see curation_store.py) replaces
sections of the pipeline's output for one subject: a gene annotation keyed
like gene_annotations (gene, tumor_type), an exact fusion-pair evidence
result keyed by ('GENEA::GENEB', tumor_type), or a fusion-partner precedent
lookup keyed by (partner gene, search tumor type). The curator's version
always wins, except that:

- retracted PMIDs are still stripped from curated content (same semantics as
  result_sanitizer.strip_retracted_pmids_from_annotation), with a flag; and
- when the pipeline now cites PMIDs absent from its evidence at curation time,
  a non-blocking `newer_pipeline_evidence` flag lists them.

Lookups are exact: an override saved with no tumor type applies only where
the served result has no tumor type, never as a fallback for tumor types
without their own override.

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
    FusionInput,
    FusionPartnerCurationPayload,
    FusionPartnerEvidenceResult,
    GeneAnnotation,
    GeneCurationPayload,
    QualityFlag,
)
from src.pipeline.literature import find_retracted_pmids
from src.pipeline.normalization import (
    FUSION_SEPARATORS,
    gene_tumor_types,
    normalize_fusions,
    split_fusion,
    split_input_tumor_types,
)
from src.pipeline.result_sanitizer import strip_retracted_pmids_from_annotation

logger = logging.getLogger(__name__)

GENE = "gene"
FUSION = "fusion"
FUSION_PARTNER = "fusion_partner"
SUBJECT_TYPES = (GENE, FUSION, FUSION_PARTNER)

NEWER_EVIDENCE_FLAG = "newer_pipeline_evidence"
CURATED_RETRACTED_FLAG = "curated_retracted_pmids_removed"
CURATION_UNAVAILABLE_FLAG = "curation_unavailable"
CURATOR_CALL_FLAG = "curator_set_cancer_association"

CurationPayload = GeneCurationPayload | FusionCurationPayload | FusionPartnerCurationPayload
_PAYLOAD_MODELS = {
    GENE: GeneCurationPayload,
    FUSION: FusionCurationPayload,
    FUSION_PARTNER: FusionPartnerCurationPayload,
}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def normalize_tumor_type(tumor_type: Optional[str]) -> str:
    """Same normalization as the gene_annotations cache key ('' when none)."""
    return " ".join((tumor_type or "").strip().lower().split())


def normalize_subject_key(subject_type: str, subject_key: str) -> str:
    """Upper-cased gene symbol (gene and fusion_partner subjects), or
    'GENEA::GENEB' for a fusion (5' partner first; '--' and '/' separators are
    accepted). Raises ValueError."""
    raw = (subject_key or "").strip()
    if subject_type in (GENE, FUSION_PARTNER):
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


def fusion_evidence_pmids(result: FusionEvidenceResult | FusionPartnerEvidenceResult) -> List[str]:
    """Evidence PMIDs of a fusion-pair or fusion-partner result: pmids, then cards."""
    return _dedupe([*result.pmids, *(card.pmid for card in result.evidence_cards)])


def partner_search_tumor_type(result: FusionPartnerEvidenceResult) -> Optional[str]:
    """The tumor type a partner-precedent lookup was scoped to: its tumor type
    for a tumor-type-scoped search, None for an all-tumor-types search (which
    is how an agnostic follow-up is keyed even when the request named one)."""
    return result.tumor_type if result.scope == "tumor_type_scoped" else None


def parse_payload(subject_type: str, payload: Dict[str, Any]) -> CurationPayload:
    return _PAYLOAD_MODELS[subject_type].model_validate(payload)


def payload_pmids(payload: CurationPayload) -> List[str]:
    """Every PMID a curator payload references, in first-seen order."""
    if isinstance(payload, (FusionCurationPayload, FusionPartnerCurationPayload)):
        return _dedupe([*(payload.pmids or []), *(card.pmid for card in payload.evidence_cards or [])])
    pmids: List[str] = list(payload.citations or [])
    pmids.extend(card.pmid for card in payload.evidence_cards or [])
    pmids.extend(quote.pmid for quote in payload.supporting_quotes or [])
    if payload.therapeutic_precedent:
        pmids.extend(precedent.pmid for precedent in payload.therapeutic_precedent.precedents)
    return _dedupe(pmids)


def payload_fields(payload: CurationPayload) -> List[str]:
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


def _call_text(value: Optional[bool]) -> str:
    return "undetermined" if value is None else ("Yes" if value else "No")


def _curator_call_flag(curated: bool, pipeline: Optional[bool]) -> QualityFlag:
    return QualityFlag(
        code=CURATOR_CALL_FLAG,
        label="Curator-set cancer association",
        severity="info",
        detail=(
            f"A curator set the cancer-associated call to {_call_text(curated)}; "
            f"the pipeline's call was {_call_text(pipeline)}."
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
    if payload.cancer_associated is not None:
        curated.cancer_associated = payload.cancer_associated
        _add_flag(curated.quality_flags, _curator_call_flag(payload.cancer_associated, pipeline.cancer_associated))

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
    if payload.cancer_associated is not None:
        # The curator made the call, so it is not undetermined, even when the
        # citations override (or the retraction guard) left no citations: the
        # gene is served with the results, and its flags explain the evidence.
        curated.insufficient_evidence = False

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


def apply_partner_override(
    result: FusionPartnerEvidenceResult,
    override: CuratedOverride,
    retracted_pmids: Optional[Set[str]] = None,
    exclude_pmids: Optional[Set[str]] = None,
) -> FusionPartnerEvidenceResult:
    """Return a copy of a pipeline fusion-partner precedent lookup with
    `override` applied. `exclude_pmids` (an agnostic follow-up's "already
    shown" PMIDs) are dropped from curated content too, so the follow-up still
    lists only new evidence."""
    retracted = set(retracted_pmids or ())
    excluded = set(exclude_pmids or ())
    payload = parse_payload(FUSION_PARTNER, override.payload)
    assert isinstance(payload, FusionPartnerCurationPayload)
    pipeline_pmids = fusion_evidence_pmids(result)
    curated = result.model_copy(deep=True)

    if payload.interpretation is not None:
        curated.interpretation = payload.interpretation
    if payload.pmids is not None:
        curated.pmids = [pmid for pmid in payload.pmids if pmid not in excluded]
    if payload.evidence_cards is not None:
        curated.evidence_cards = [card.model_copy() for card in payload.evidence_cards if card.pmid not in excluded]

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
    if payload.has_precedent is not None:
        curated.has_precedent = payload.has_precedent
    elif payload.pmids is not None:
        curated.has_precedent = bool(curated.pmids)
    if removed:
        _add_flag(curated.quality_flags, _curated_retracted_flag(removed))

    newer = _newer_evidence(pipeline_pmids, override, curated_pmids, retracted | excluded)
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


def has_curation_unavailable(
    items: Iterable[GeneAnnotation | FusionEvidenceResult | FusionPartnerEvidenceResult],
) -> bool:
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


async def apply_current_partner_override(
    result: FusionPartnerEvidenceResult,
    run_store: Any,
    exclude_pmids: Optional[Set[str]] = None,
) -> FusionPartnerEvidenceResult:
    """Overlay the CURRENT fusion_partner override for a pipeline partner
    lookup, keyed by (partner gene, partner_search_tumor_type)."""
    try:
        key = (
            normalize_subject_key(FUSION_PARTNER, result.gene),
            normalize_tumor_type(partner_search_tumor_type(result)),
        )
    except ValueError:
        return result
    overrides, failed = await _lookup_overrides(run_store, FUSION_PARTNER, [key])
    if failed:
        flagged = result.model_copy(deep=True)
        _add_flag(flagged.quality_flags, _curation_unavailable_flag())
        return flagged
    override = overrides.get((FUSION_PARTNER, *key))
    if override is None:
        return result
    retracted = await _retracted_among([override], FUSION_PARTNER)
    try:
        return apply_partner_override(result, override, retracted, exclude_pmids)
    except Exception:
        logger.error("Failed to apply curated override for fusion partner %s", result.gene, exc_info=True)
        flagged = result.model_copy(deep=True)
        _add_flag(flagged.quality_flags, _curation_unavailable_flag())
        return flagged


def _annotation_tumor_types(result: AnnotationResult) -> List[Optional[str]]:
    return [
        result.annotation_tumor_types[annotation.gene]
        if annotation.gene in result.annotation_tumor_types
        else pipeline_original(annotation).analysis_tumor_type
        for annotation in result.annotations
    ]


async def overlay_annotation_result(result: AnnotationResult, run_store: Any) -> AnnotationResult:
    """A copy of a (pipeline) result with the CURRENT overrides applied to its
    gene annotations and fusion evidence, and curation_unavailable set.

    Genes whose tumor context is unresolved get no override at all (applying
    the untyped one could be wrong) and are flagged curation_unavailable."""
    unresolved = result._unresolved_tumor_genes
    tumor_types = _annotation_tumor_types(result)
    resolvable = [i for i, a in enumerate(result.annotations) if a.gene not in unresolved]
    overlaid = await apply_current_gene_overrides(
        [result.annotations[i] for i in resolvable], run_store, [tumor_types[i] for i in resolvable]
    )
    by_index = dict(zip(resolvable, overlaid))
    if unresolved:
        logger.error(
            "Tumor context unresolved for %s in run %s; their curator overrides are not applied",
            ", ".join(sorted(unresolved)), result.run_id,
        )
    annotations = [
        by_index[i] if i in by_index else _flag_gene_unavailable(pipeline_original(a))
        for i, a in enumerate(result.annotations)
    ]
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


def _request_fusion_inputs(request: Dict[str, Any]) -> List[FusionInput]:
    """The saved request's inputs as FusionInputs (AnnotateRequest, or a
    GeneAnnotateRequest's single gene). Raises on a malformed request."""
    if "fusions" not in request:
        return [FusionInput(gene=request["gene"], tumor_type=request.get("tumor_type"))]
    return [
        item if isinstance(item, str) else FusionInput.model_validate(item)
        for item in request.get("fusions") or []
    ]


async def ensure_annotation_tumor_types(
    result: AnnotationResult, run_store: Any, run_id: str
) -> Dict[str, Optional[str]]:
    """Reconstruct annotation_tumor_types for runs saved before it existed.

    Replays the pipeline's own rule (normalization.gene_tumor_types) on the
    saved request, HGNC-normalizing only the inputs that carry a tumor type
    (the only ones that can give a gene tumor context), so aliases map to
    their canonical gene. Only CONFIRMED entries are added to the map; when the
    request or alias resolution is unavailable, the missing genes are recorded
    in result._unresolved_tumor_genes instead. Returns the newly confirmed
    entries (so the caller can persist exactly those)."""
    missing = [a.gene for a in result.annotations if a.gene not in result.annotation_tumor_types]
    # Only matters where overrides exist; other stores (benchmark, tests) skip it.
    if not missing or getattr(run_store, "get_curated_overrides", None) is None:
        return {}

    def unresolved(reason: str) -> Dict[str, Optional[str]]:
        logger.error("Cannot reconstruct tumor context for run %s (%s): %s", run_id, reason, ", ".join(missing))
        result._unresolved_tumor_genes.update(missing)
        return {}

    getter = getattr(run_store, "get_run_request", None)
    if getter is None:
        return unresolved("store cannot load run requests")
    try:
        request = await getter(run_id)
        if not request:
            return unresolved("saved request not found")
        input_strings, tumor_type_by_input = split_input_tumor_types(_request_fusion_inputs(request))
    except Exception:
        logger.warning("Failed to load request for run %s", run_id, exc_info=True)
        return unresolved("saved request unavailable")

    tumor_types: Dict[str, Optional[str]] = {}
    if tumor_type_by_input:
        typed_inputs = list(dict.fromkeys(v for v in input_strings if v in tumor_type_by_input))
        try:
            gene_map = await normalize_fusions(typed_inputs)
        except Exception:
            logger.warning("Alias resolution failed for run %s", run_id, exc_info=True)
            return unresolved("alias resolution failed")
        if any(resolved.lookup_failed for resolved, _ in gene_map.values()):
            return unresolved("gene symbol lookup unavailable")
        tumor_types = gene_tumor_types(gene_map, tumor_type_by_input)
    # A gene no tumor-typed input resolves to is confirmed to have none.
    confirmed = {gene: tumor_types.get(gene) for gene in missing}
    result.annotation_tumor_types.update(confirmed)
    return confirmed


def carry_tumor_context(target: AnnotationResult, source: AnnotationResult) -> None:
    """Copy reconstructed tumor context onto a freshly reloaded copy of the run."""
    for gene, tumor_type in source.annotation_tumor_types.items():
        target.annotation_tumor_types.setdefault(gene, tumor_type)
    target._unresolved_tumor_genes.update(
        gene for gene in source._unresolved_tumor_genes if gene not in target.annotation_tumor_types
    )
