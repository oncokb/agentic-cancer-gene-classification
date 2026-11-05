"""Pydantic models: the v1 artifact contract (input) and the API (output).

Artifact models ignore unknown fields (pydantic's default), as the contract
requires; ``schema_version`` is checked by the artifact client before parsing.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_SCHEMA_VERSION = "1"

ExclusionReason = Literal["stats", "precheck", "literature"]
JobState = Literal["queued", "running", "succeeded", "failed"]


# --------------------------------------------------------------------------
# Artifact contract, schema_version 1 (authoritative: gene-expression-relevance
# docs/acgc-contract.md)
# --------------------------------------------------------------------------


class _Artifact(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ManifestCancerType(_Artifact):
    study_id: str
    oncotree_code: str
    name: str
    tumor_type_query: str
    import_date: Optional[str] = None


class Manifest(_Artifact):
    schema_version: str
    release_id: str
    method_version: str
    params: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)
    cancer_types: List[ManifestCancerType] = Field(default_factory=list)

    def cancer_type(self, oncotree_code: str) -> Optional[ManifestCancerType]:
        for cancer_type in self.cancer_types:
            if cancer_type.oncotree_code == oncotree_code:
                return cancer_type
        return None


class GeneRef(_Artifact):
    hugo_symbol: str
    entrez_gene_id: Optional[int] = None


class CancerTypeStats(_Artifact):
    """One (gene, cancer type) row of a gene file.

    The producer emits null for the statistics when they are undefined (e.g.
    n == 0 or the test can't run) and for the pre-check fields when the
    pre-check was not run (stats_pass false, or a skip mode).
    """

    oncotree_code: str  # TCGA PanCancer cohort code, e.g. BRCA, COADREAD, GBM
    n: int
    n_rest: int
    median_log2: Optional[float] = None
    rest_median_log2: Optional[float] = None
    p90_log2: Optional[float] = None
    rest_p90_log2: Optional[float] = None
    log2fc: Optional[float] = None
    log2fc_vs_median_of_type_medians: Optional[float] = None
    cliffs_delta: Optional[float] = None
    auc: Optional[float] = None
    p_value: Optional[float] = None
    q_value: Optional[float] = None
    stats_pass: bool
    pubmed_count: Optional[int] = None
    # None = pre-check not run; only an explicit False excludes a pair.
    literature_precheck_pass: Optional[bool] = None
    flags: List[str] = Field(default_factory=list)


class GeneArtifact(_Artifact):
    schema_version: str
    release_id: str
    gene: GeneRef
    cancer_types: List[CancerTypeStats] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Literature verdicts
# --------------------------------------------------------------------------


class LiteratureVerdict(BaseModel):
    literature_gate: str
    tumor_type_query: str
    supported: bool
    insufficient_evidence: bool
    pmids: List[str] = Field(default_factory=list)
    rationale: Optional[str] = None
    evidence_support_score: Optional[float] = None
    evaluated_at: datetime


# --------------------------------------------------------------------------
# Stored rows and API responses
# --------------------------------------------------------------------------


class StoredPair(BaseModel):
    """One row of expression_context_results."""

    hugo_symbol: str
    oncotree_code: str
    release_id: str
    literature_gate: str
    surfaced: bool
    exclusion_reason: Optional[ExclusionReason] = None
    stats: Dict[str, Any]
    evidence: Optional[Dict[str, Any]] = None
    computed_at: datetime
    requested_by: Optional[str] = None


class PairResult(BaseModel):
    oncotree_code: str
    cancer_type_name: Optional[str] = None
    study_id: Optional[str] = None
    surfaced: bool
    exclusion_reason: Optional[ExclusionReason] = None
    stats: CancerTypeStats
    literature: Optional[LiteratureVerdict] = None
    computed_at: datetime


class GeneResult(BaseModel):
    hugo_symbol: str
    release_id: str
    literature_gate: str
    computed_at: datetime
    surfaced: List[PairResult]
    excluded: Optional[List[PairResult]] = None


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    gene: str = Field(..., min_length=1, max_length=64)


class JobAccepted(BaseModel):
    job_id: str
    status: JobState
    status_url: str


class JobPairError(BaseModel):
    oncotree_code: str
    error: str


class JobStatus(BaseModel):
    job_id: str
    status: JobState
    gene: str
    hugo_symbol: str
    release_id: str
    created_at: datetime
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    literature_evaluated: int = 0
    literature_reused: int = 0
    pair_errors: List[JobPairError] = Field(default_factory=list)
    result: Optional[GeneResult] = None


class JobRecord(JobStatus):
    """What the job store keeps; ``owner`` is never returned to clients."""

    owner: Optional[str] = None

    def public(self) -> JobStatus:
        return JobStatus(**self.model_dump(exclude={"owner"}))


class HistoryEntry(BaseModel):
    hugo_symbol: str
    release_id: str
    literature_gate: str
    computed_at: datetime
    pair_count: int
    surfaced_count: int


class HistoryPage(BaseModel):
    items: List[HistoryEntry]
    next_cursor: Optional[str] = None
