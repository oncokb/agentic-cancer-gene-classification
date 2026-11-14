"""Pydantic models: the release-bundle contract (input) and the API (output).

Contract: expression_stats/docs/contract.md (bundle schema_version 1;
statistical method m2, plus the additive method-m3 fields).
Artifact models ignore unknown fields (pydantic's default), as the contract
requires; ``schema_version`` is checked by the bundle loader before parsing.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

SUPPORTED_SCHEMA_VERSION = "1"
# Method versions this module understands. m3 only adds optional fields, so
# m2 bundles still load; method_version is informational, never enforced.
SUPPORTED_METHOD_VERSIONS = ("2", "3")
HIGH_CONFIDENCE = "high"
# expression_stats contract version that introduced per-pair confidence_tier.
CONFIDENCE_TIER_CONTRACT = (1, 3)


_CONTRACT_VERSION = re.compile(r"[0-9]+\.[0-9]+(?:\.[0-9]+)?")


def purity_check_required(contract_version: Any) -> bool:
    """Only a well-formed pre-1.4 contract may omit the purity gate."""
    if not isinstance(contract_version, str) or not _CONTRACT_VERSION.fullmatch(contract_version):
        return True
    return tuple(int(part) for part in contract_version.split(".")) >= (1, 4)


def confidence_tier_required(contract_version: Any) -> bool:
    """Whether a bundle's pairs must carry ``confidence_tier`` (fail closed).

    Only a well-formed version string (``<major>.<minor>[.<patch>]``, ASCII
    digits, nothing else: no sign, no whitespace) below 1.3 keeps the legacy
    "missing => high" fallback. Everything else requires tiers: 1.3 or later, a
    missing/None/empty version, a non-string, or any malformed value.
    """
    if not isinstance(contract_version, str) or not _CONTRACT_VERSION.fullmatch(
        contract_version
    ):
        return True
    version = tuple(int(part) for part in contract_version.split("."))
    return version >= CONFIDENCE_TIER_CONTRACT


PASS_MODE_LABELS = {"lineage": "Lineage enriched", "subgroup": "Subgroup enriched"}
# Canonical/alias gene symbols as accepted from users and from bundles.
SYMBOL_PATTERN = r"[A-Z0-9][A-Z0-9._-]{0,63}\Z"

# "candidate": passes the v3 statistics but not the high-confidence tier
# (contract 1.3); never surfaced, never sent to the literature gate.
ExclusionReason = Literal["stats", "precheck", "literature", "pending", "candidate"]
JobState = Literal["queued", "running", "succeeded", "failed"]


# --------------------------------------------------------------------------
# Release bundle contract, schema_version 1 (authoritative:
# expression_stats/docs/contract.md)
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
    # Additive contract minor version ("1.1", "1.2", "1.3", ...). Absent, empty or
    # malformed means tiers are required (fail closed; see confidence_tier_required).
    contract_version: Optional[Any] = None
    params: Dict[str, Any] = Field(default_factory=dict)
    source: Dict[str, Any] = Field(default_factory=dict)
    cancer_types: List[ManifestCancerType] = Field(default_factory=list)

    @property
    def confidence_tier_required(self) -> bool:
        """True unless the version is well-formed and below 1.3: every stats pass
        must then carry ``confidence_tier`` (see ``confidence_tier_required``)."""
        return confidence_tier_required(self.contract_version)

    @property
    def purity_check_required(self) -> bool:
        return purity_check_required(self.contract_version)

    def cancer_type(self, oncotree_code: str) -> Optional[ManifestCancerType]:
        for cancer_type in self.cancer_types:
            if cancer_type.oncotree_code == oncotree_code:
                return cancer_type
        return None


class BundleManifest(Manifest):
    """``manifest.json`` release asset: the release manifest plus bundle fields."""

    bundle_tag: str
    pubmed_snapshot_date: date
    gene_count: int = Field(ge=0)
    # Lower-case SHA-256 hex and byte length of the compressed genes.jsonl.gz.
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


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

    # ---- method m3 (additive; absent in m2 bundles, so all optional) ----
    # How the pair passed: "lineage" (whole-cohort enrichment) or "subgroup"
    # (a high-expressing subset). Kept as a string so an unknown future
    # value can't reject a whole bundle; only the two known values get a label.
    pass_mode: Optional[str] = None
    # The gene is broadly expressed, so lineage enrichment was blocked for
    # this pair (it can still pass in subgroup mode).
    lineage_blocked_by_broad: Optional[bool] = None
    subgroup_high_count: Optional[int] = None
    subgroup_high_fraction: Optional[float] = None
    subgroup_high_fraction_ratio: Optional[float] = None
    p95_log2_diff: Optional[float] = None
    subgroup_q: Optional[float] = None

    # ---- contract 1.3 (optional; absent in older bundles) ----
    # "high" | "candidate" | null (null when the v3 pass fails). A string so an
    # unknown future value can't reject a bundle; only "high" counts as high.
    confidence_tier: Optional[str] = None
    # Contract 1.4: optional for old bundles; only known retaining states allow high.
    purity_check: Optional[str] = None
    purity_rho: Optional[float] = None
    purity_q: Optional[float] = None

    @property
    def broad_blocked(self) -> bool:
        """Lineage enrichment blocked by broad expression and no subgroup pass:
        visible, never passing, never sent to the literature gate."""
        return bool(self.lineage_blocked_by_broad) and self.pass_mode != "subgroup"

    @property
    def passes_stats(self) -> bool:
        """The statistics call ACGC acts on (stats_pass, minus broad-blocked pairs)."""
        return self.stats_pass and not self.broad_blocked

    @property
    def confidence_tier_missing(self) -> bool:
        """A passing pair whose document omits ``confidence_tier`` entirely."""
        return self.passes_stats and "confidence_tier" not in self.model_fields_set

    def effective_confidence_tier(
        self, *, tier_required: bool, purity_required: bool = False
    ) -> Optional[str]:
        """The tier ACGC acts on, None unless ``passes_stats``.

        ``tier_required`` is ``Manifest.confidence_tier_required``. Bundles with a
        well-formed contract_version below 1.3 omit ``confidence_tier``: there (and
        only there) a missing field on a passing pair counts as "high", so old data
        behaves exactly as before. Otherwise a missing field fails closed (None).
        An explicit null or an unknown value on a passing pair is never high.
        """
        if not self.passes_stats:
            return None
        if self.purity_check == "demoted":
            return "candidate"
        tier = self.confidence_tier
        if "confidence_tier" not in self.model_fields_set:
            tier = None if tier_required else HIGH_CONFIDENCE
        if (
            tier == HIGH_CONFIDENCE
            and purity_required
            and self.purity_check not in {"pass", "not_applicable", "unassessed"}
        ):
            return None
        return tier

    def high_confidence(self, *, tier_required: bool, purity_required: bool = False) -> bool:
        return (
            self.effective_confidence_tier(
                tier_required=tier_required, purity_required=purity_required
            )
            == HIGH_CONFIDENCE
        )

    @property
    def enrichment_label(self) -> Optional[str]:
        if not self.passes_stats or self.pass_mode is None:
            return None
        return PASS_MODE_LABELS.get(self.pass_mode, self.pass_mode)


class GeneArtifact(_Artifact):
    schema_version: str
    release_id: str
    gene: GeneRef
    # m3: the gene is broadly expressed across cohorts (absent in m2).
    broadly_expressed: Optional[bool] = None
    cancer_types: List[CancerTypeStats] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Literature verdicts
# --------------------------------------------------------------------------


TumorVerdict = Literal["supported", "not_supported", "insufficient"]


@dataclass(frozen=True)
class TumorContext:
    """The tumor type a literature check must confirm."""

    oncotree_code: str  # TCGA PanCancer cohort code from the manifest
    name: str  # e.g. "Kidney Chromophobe"
    tumor_type_query: str  # disease phrase, e.g. "chromophobe renal cell carcinoma"

    @property
    def terms(self) -> List[str]:
        """Distinct names for the tumor type, most specific first."""
        out: List[str] = []
        for term in (self.tumor_type_query, self.name, self.oncotree_code):
            term = (term or "").strip()
            if term and term.lower() not in {t.lower() for t in out}:
                out.append(term)
        return out


class PaperJudgement(BaseModel):
    """One retrieved paper, judged for the specific tumor type."""

    pmid: str
    title: Optional[str] = None
    # Tumor-type terms found in its title/abstract (deterministic match).
    matched_terms: List[str] = Field(default_factory=list)
    # Describes the gene's expression/role IN this tumor type.
    tumor_specific: bool = False
    reason: Optional[str] = None


class LiteratureVerdict(BaseModel):
    literature_gate: str
    tumor_type_query: str
    supported: bool
    insufficient_evidence: bool
    # Citations shown for the pair. For tumor-specific gates (v2+) these are
    # ONLY the PMIDs judged to describe the gene in this tumor type.
    pmids: List[str] = Field(default_factory=list)
    rationale: Optional[str] = None
    evidence_support_score: Optional[float] = None
    evaluated_at: datetime
    # ---- tumor-specific gates (acgc_tumor_specific_v2); None for v1 ----
    verdict: Optional[TumorVerdict] = None
    oncotree_code: Optional[str] = None
    cancer_type_name: Optional[str] = None
    retrieved_count: Optional[int] = None
    papers: List[PaperJudgement] = Field(default_factory=list)
    # "guard": no paper mentioned the tumor type, so no LLM judgement was made
    # (always "insufficient"); "judge": the LLM judged the papers.
    decided_by: Optional[Literal["guard", "judge"]] = None


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
    bundle_tag: Optional[str] = None


class GeneResult(BaseModel):
    hugo_symbol: str
    broadly_expressed: Optional[bool] = None  # m3; None for m2 bundles
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
    bundle_tag: Optional[str] = None
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


# --------------------------------------------------------------------------
# Bundles: provenance and the per-gene statistics view (stats page)
# --------------------------------------------------------------------------

# "expired": the bundle's pre-check counts are past source.precheck_expires_at,
# so the pre-check doesn't exclude (the literature gate decides).
PrecheckState = Literal["pass", "fail", "not_run", "expired"]
# "candidate": v3 stats pass below the high-confidence tier; never surfaced.
PairStatus = Literal["surfaced", "excluded", "literature_pending", "candidate"]


class BundleProvenance(BaseModel):
    bundle_tag: str
    release_id: str
    schema_version: str
    method_version: str
    pubmed_snapshot_date: date
    gene_count: int
    published_at: Optional[datetime] = None
    loaded_at: Optional[datetime] = None
    release_url: Optional[str] = None
    source: Dict[str, Any] = Field(default_factory=dict)
    params: Dict[str, Any] = Field(default_factory=dict)


class RefreshState(BaseModel):
    last_checked_at: Optional[datetime] = None
    next_check_after: Optional[datetime] = None
    last_error: Optional[str] = None
    last_error_at: Optional[datetime] = None
    pinned_bundle_tag: Optional[str] = None
    releases_repo: str


class BundleStatus(BaseModel):
    active: Optional[BundleProvenance] = None
    refresh: RefreshState


class PairStatsView(BaseModel):
    oncotree_code: str
    cancer_type_name: Optional[str] = None
    study_id: Optional[str] = None
    tumor_type_query: Optional[str] = None
    stats: CancerTypeStats
    # The statistics call: stats_pass, minus pairs whose lineage enrichment
    # was blocked by broad expression (m3).
    stats_pass: bool
    # m3 display fields (None/False for m2 bundles).
    enrichment: Optional[str] = None  # "Lineage enriched" / "Subgroup enriched"
    broad_blocked: bool = False
    precheck: PrecheckState
    # "high" | "candidate" | None as acted on (missing field in an older bundle
    # on a passing pair = "high"); None when the statistics fail.
    confidence_tier: Optional[str] = None
    # Sent to the literature gate: stats_pass AND high tier AND precheck is not "fail".
    literature_eligible: bool
    status: PairStatus
    exclusion_reason: Optional[ExclusionReason] = None
    literature: Optional[LiteratureVerdict] = None


class GeneStatsSummary(BaseModel):
    surfaced: int = 0
    literature_pending: int = 0
    excluded: int = 0
    candidate: int = 0


class GeneStatsView(BaseModel):
    query: str
    hugo_symbol: str
    entrez_gene_id: Optional[int] = None
    broadly_expressed: Optional[bool] = None  # m3; None for m2 bundles
    literature_gate: str
    bundle: BundleProvenance
    summary: GeneStatsSummary
    pairs: List[PairStatsView]
    has_stored_result: bool = False
