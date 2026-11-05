"""Literature gate: does the literature characterize a gene in a tumor type?

The router and service only see the ``LiteratureGate`` protocol, so the v1
ACGC-pipeline implementation can later be swapped for LRAS
(``/lras/v1/retrieve``) without touching them. The gate name is part of the
stored verdict key, so verdicts from different gates never mix.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from src.expression_context.models import LiteratureVerdict


class LiteratureGateError(RuntimeError):
    """The gate could not produce a verdict (not the same as 'unsupported')."""


@runtime_checkable
class LiteratureGate(Protocol):
    name: str

    async def evaluate(self, hugo_symbol: str, tumor_type_query: str) -> LiteratureVerdict:
        ...


def is_supported(insufficient_evidence: bool, pmids: list[str]) -> bool:
    """v1 rule: supported iff evidence is not insufficient AND >=1 cited PMID."""
    return (not insufficient_evidence) and len(pmids) >= 1


class AcgcPipelineGate:
    """v1 gate: the existing ACGC annotation pipeline, scoped to the tumor type.

    Runs ``run_pipeline`` for the gene with ``tumor_type = tumor_type_query``
    and the app's run store, so the existing per-(gene, tumor_type)
    ``gene_annotations`` cache is read and written through the pipeline's own
    public entry point — this module never touches that table directly.
    """

    name = "acgc_pipeline_v1"

    def __init__(self, run_store_getter: Callable[[], Any], mode: str = "full") -> None:
        self._run_store_getter = run_store_getter
        self._mode = mode

    async def evaluate(self, hugo_symbol: str, tumor_type_query: str) -> LiteratureVerdict:
        # Imported lazily: the pipeline pulls in LLM/NCBI clients that the
        # module doesn't need until a job actually runs.
        from src.models.schema import FusionInput
        from src.pipeline.orchestrator import run_pipeline

        try:
            result = await run_pipeline(
                [FusionInput(fusion=hugo_symbol, tumor_type=tumor_type_query)],
                run_store=self._run_store_getter(),
                mode=self._mode,
                strict_gene_lookup=True,
            )
        except Exception as exc:
            raise LiteratureGateError(f"pipeline failed: {type(exc).__name__}") from exc

        annotation = _pick_annotation(result.annotations, hugo_symbol)
        if annotation is None:
            raise LiteratureGateError("pipeline returned no annotation for the gene")
        pmids = [str(pmid).strip() for pmid in annotation.citations if str(pmid).strip()]
        insufficient = bool(annotation.insufficient_evidence)
        return LiteratureVerdict(
            literature_gate=self.name,
            tumor_type_query=tumor_type_query,
            supported=is_supported(insufficient, pmids),
            insufficient_evidence=insufficient,
            pmids=pmids,
            rationale=annotation.cancer_association_rationale,
            evidence_support_score=annotation.evidence_support_score,
            evaluated_at=datetime.now(timezone.utc),
        )


def _pick_annotation(annotations: list, hugo_symbol: str) -> Optional[Any]:
    for annotation in annotations:
        if str(annotation.gene).upper() == hugo_symbol.upper():
            return annotation
    return annotations[0] if len(annotations) == 1 else None
