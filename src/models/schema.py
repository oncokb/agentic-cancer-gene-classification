from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_serializer, model_validator

LocalBackend = Literal["claude-code", "codex", "antigravity"]
AnnotationMode = Literal["full", "core"]
CacheStatus = Literal["miss", "reused", "refreshed", "bypassed"]


class ResolvedGene(BaseModel):
    """Result of HGNC normalization for a single gene symbol."""
    input_symbol: str
    canonical_symbol: Optional[str] = None
    hgnc_id: Optional[str] = None
    name: Optional[str] = None
    alias_symbols: List[str] = Field(default_factory=list)
    locus_type: Optional[str] = None
    resolved: bool
    unresolvable: bool = False  # bare Ensembl ID or unannotated locus


class LiteratureRecord(BaseModel):
    pmid: str
    title: str
    abstract: str
    journal: str = ""
    publication_types: List[str] = Field(default_factory=list)
    publication_year: Optional[int] = None
    pubmed_comment_ref_types: List[str] = Field(default_factory=list)
    pubmed_comment_pmids: List[str] = Field(default_factory=list)
    # Query families that surfaced this PMID during retrieval (e.g. "mesh_gene_name",
    # "free_text", "tier2_agentic") — a PMID can be found by more than one query.
    # Feeds the query-tier precision signal in the citation pre-ranking heuristic.
    matched_query_tiers: List[str] = Field(default_factory=list)


class SupportingQuote(BaseModel):
    pmid: str
    quote: str


class LiteraturePaperScore(BaseModel):
    """Composite pre-ranking score for one retrieved paper, with the individual signal
    values that produced it — for auditing why a PMID ranked where it did."""

    pmid: str
    citation_composite_score: float
    context_composite_score: float
    query_tier_score: float
    fusion_cooccurrence_score: Optional[float] = None  # None when not a fusion annotation
    recency_score: float
    publication_type_citation_score: float
    publication_type_context_score: float
    matched_query_tiers: List[str] = Field(default_factory=list)
    publication_year: Optional[int] = None
    # Which candidate pool this paper was merged into ahead of the Haiku selection
    # pass, if any — "citation" (top of the citation-weighted ranking) or
    # "context_supplement" (review-leaning paper pulled in for summary framing).
    pool: Optional[Literal["citation", "context_supplement"]] = None


class EvidenceCard(BaseModel):
    pmid: str
    title: str = ""
    journal: str = ""
    evidence_type: str = "other"
    selected_reason: str = ""
    quote: Optional[str] = None


class FusionEvidenceCard(EvidenceCard):
    fusion: str = ""


class FusionEvidenceResult(BaseModel):
    fusion: str
    tumor_type: Optional[str] = None
    well_supported: bool = False
    retrieved_count: int = 0
    pmids: List[str] = Field(default_factory=list)
    interpretation: str = ""
    evidence_cards: List[FusionEvidenceCard] = Field(default_factory=list)


class FusionPartnerEvidenceRequest(BaseModel):
    """On-demand lookup: does `gene` have precedent as an oncogenic fusion partner
    elsewhere? Not the same question as FusionEvidenceResult, which checks an exact
    fusion pair — this checks a single partner gene against any reported fusion."""

    gene: str = Field(..., description="Partner gene symbol to check for oncogenic fusion precedent")
    tumor_type: Optional[str] = Field(
        default=None,
        description="Tumor type context. When set, the default search is scoped to this tumor type.",
    )
    agnostic: bool = Field(
        default=False,
        description=(
            "Broaden the search to all tumor types instead of scoping to `tumor_type`. Intended as a "
            "follow-up call after reviewing the tumor-type-scoped result."
        ),
    )
    exclude_pmids: List[str] = Field(
        default_factory=list,
        description="PMIDs already surfaced by a prior call to exclude, so results show only new evidence.",
    )


class FusionPartnerEvidenceResult(BaseModel):
    gene: str
    tumor_type: Optional[str] = None
    scope: Literal["tumor_type_scoped", "all_tumor_types"] = "all_tumor_types"
    has_precedent: bool = False
    retrieved_count: int = 0
    pmids: List[str] = Field(default_factory=list)
    interpretation: str = ""
    evidence_cards: List[EvidenceCard] = Field(default_factory=list)


class QualityFlag(BaseModel):
    code: str
    label: str
    severity: Literal["info", "warning", "critical"] = "info"
    detail: str = ""


class ClinicalActionabilityEvidence(BaseModel):
    pmid: str
    title: str = ""
    evidence_type: Literal["clinical", "preclinical", "case_report", "review", "other"] = "other"
    therapies: List[str] = Field(default_factory=list)
    domains: List[str] = Field(default_factory=list)
    matched_terms: List[str] = Field(default_factory=list)
    quote: Optional[str] = None


class ClinicalActionabilityScoreComponent(BaseModel):
    code: str
    label: str
    delta: float
    pmids: List[str] = Field(default_factory=list)
    detail: str = ""


class ClinicalActionability(BaseModel):
    """High-confidence literature-derived therapeutic precedent.

    This is intentionally conservative and deterministic. It is not a treatment
    recommendation; it only indicates that retrieved, verified literature contains
    a strong enough therapeutic/domain-actionability signal to show curators.
    """

    confidence_score: float = Field(ge=0.0, le=1.0)
    confidence_level: Literal["high"] = "high"
    summary: str
    confidence_explanation: str
    pmids: List[str] = Field(default_factory=list)
    score_components: List[ClinicalActionabilityScoreComponent] = Field(default_factory=list)
    evidence: List[ClinicalActionabilityEvidence] = Field(default_factory=list)


class FusionTreatmentKnowledge(BaseModel):
    """Deterministic, CIViC-sourced treatment/evidence data for a fusion, keyed by
    gene pair (e.g. EML4::ALK). Never LLM-generated — displayed as-is."""

    oncogenic: Optional[str] = None
    therapies: List[str] = Field(default_factory=list)
    evidence: List[dict] = Field(default_factory=list)
    diseases: List[str] = Field(default_factory=list)
    sources: List[str] = Field(default_factory=list)


class FusionPartnerContext(BaseModel):
    """Domain-retention and breakpoint context for one side of a fusion."""

    gene: str
    side: Literal["five_prime", "three_prime"]
    transcript_id: Optional[str] = None
    exon: Optional[str] = None
    genomic_breakpoint: Optional[str] = None
    transcript_breakpoint: Optional[str] = None
    protein_breakpoint: Optional[str] = None
    retained_domains: List[str] = Field(default_factory=list)
    lost_domains: List[str] = Field(default_factory=list)
    disrupted_domains: List[str] = Field(default_factory=list)


class FusionPositionContext(BaseModel):
    """Protein-level fusion context: per-partner domain retention plus fusion-level
    treatment knowledge. Fetched on demand, never blocking the core annotation."""

    fusion: str
    five_prime: FusionPartnerContext
    three_prime: FusionPartnerContext
    kinase_gene: Optional[str] = None
    kinase_gene_side: Optional[str] = None
    kinase_domain_status: Optional[str] = None
    knowledge: Optional[FusionTreatmentKnowledge] = None
    source: Literal["input", "genome_nexus"] = "input"
    error: Optional[str] = None


class OpenEvidenceCitation(BaseModel):
    """One bibliographic source cited by an OpenEvidence analysis.

    Unverified — these citation_keys are not guaranteed to correspond to
    PMIDs in the retrieved LiteratureRecord set, so they must never be
    merged into GeneAnnotation.citations or run through PMID verification.
    """

    citation_key: str
    title: str = ""
    authors: str = ""
    journal: str = ""
    date: str = ""
    doi: str = ""
    url: str = ""
    source_texts: List[str] = Field(default_factory=list)


class OpenEvidenceAnalysis(BaseModel):
    """Supplementary AI-synthesized evidence from OpenEvidence for one gene
    question. Unverified: `text` and `citations` are not grounded against the
    retrieved PubMed set and must always be surfaced as clearly-labeled
    supplementary context, never as verified citations."""

    question: str
    text: str = ""
    citations: List[OpenEvidenceCitation] = Field(default_factory=list)


class GeneAnnotation(BaseModel):
    """One row in Nicole's spreadsheet, keyed by gene."""

    gene: str
    fusions: List[str] = Field(default_factory=list)
    in_oncokb: Optional[bool] = None  # None when OncoKB token not configured

    cancer_associated: Optional[bool] = None
    cancer_association_rationale: Optional[str] = None
    cancer_type_prevalence: Optional[str] = None
    gene_class: Optional[str] = None
    signaling_pathways: Optional[str] = None
    gene_summary: Optional[str] = None
    citations: List[str] = Field(default_factory=list)  # verified PMIDs only
    supporting_quotes: List[SupportingQuote] = Field(default_factory=list)
    evidence_cards: List[EvidenceCard] = Field(default_factory=list)
    clinical_actionability: Optional[ClinicalActionability] = None
    quality_flags: List[QualityFlag] = Field(default_factory=list)
    # Supplementary AI-synthesized evidence from OpenEvidence, only populated
    # when OPENEVIDENCE_ENABLED=true. Unverified — never counted among the
    # verified PMID `citations` above.
    openevidence_supplementary: Optional[OpenEvidenceAnalysis] = None
    date_annotated: str = Field(
        default_factory=lambda: date.today().strftime("%-m/%-d/%y")
    )

    # Internal quality metadata (not exported to Nicole's sheet)
    retrieval_count: int = 0
    retrieved_pmids: List[str] = Field(default_factory=list)
    retrieval_ranking: List[LiteraturePaperScore] = Field(default_factory=list)
    insufficient_evidence: bool = False
    evidence_support_score: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "Deterministic evidence support score. This estimates how strongly the "
            "generated annotation is supported by retrieved literature and verified "
            "PMID citations; it is not a calibrated probability of biological truth "
            "or clinical actionability."
        ),
    )
    evidence_support_explanation: str = Field(
        default=(
            "Evidence support score estimates how well the generated annotation is "
            "grounded in retrieved literature and verified PMIDs; it is not a "
            "probability of biological truth or clinical actionability."
        ),
    )
    cache_status: Optional[CacheStatus] = None
    cache_reason: Optional[str] = None
    cached_at: Optional[str] = None
    last_pubmed_checked_at: Optional[str] = None
    error: Optional[str] = None
    timings_ms: Dict[str, float] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def _omit_openevidence_supplementary_when_absent(self, handler):
        """Omit `openevidence_supplementary` entirely (not present-as-null)
        when it's None, so the OPENEVIDENCE_ENABLED=false path has zero new
        keys in any serialized GeneAnnotation — API responses, exports, and
        persisted run/gene-cache JSON — matching current main's shape
        exactly. Deliberately scoped to this one field only: this is NOT a
        model-wide exclude_none, so every other None-valued field (e.g.
        clinical_actionability) still serializes as null, unchanged.
        """
        data = handler(self)
        if self.openevidence_supplementary is None:
            data.pop("openevidence_supplementary", None)
        return data


class FusionInput(BaseModel):
    """Structured gene or fusion input supporting optional tumor type and breakpoint context."""
    fusion: str = Field(
        ...,
        description="Single gene symbol or gene fusion in GENE1::GENE2 or GENE1--GENE2 format",
    )
    tumor_type: Optional[str] = Field(default=None, description="Tumor type for literature retrieval")
    five_exon: Optional[int] = Field(default=None, description="5' partner exon number at breakpoint")
    three_exon: Optional[int] = Field(default=None, description="3' partner exon number at breakpoint")
    five_genomic: Optional[str] = Field(default=None, description="5' genomic breakpoint (chr:pos)")
    three_genomic: Optional[str] = Field(default=None, description="3' genomic breakpoint (chr:pos)")
    five_transcript: Optional[str] = Field(default=None, description="5' transcript breakpoint")
    three_transcript: Optional[str] = Field(default=None, description="3' transcript breakpoint")

    @model_validator(mode="before")
    @classmethod
    def coerce_gene_input_aliases(cls, data: Any) -> Any:
        """Allow structured rows to name the required value as gene/input/query."""
        if isinstance(data, dict) and "fusion" not in data:
            for alias in ("gene", "input", "query"):
                if alias in data:
                    return {**data, "fusion": data[alias]}
        return data


class AnnotateRequest(BaseModel):
    fusions: List[FusionInput] = Field(
        ...,
        description="Gene or fusion inputs with optional tumor type and breakpoint context",
        min_length=1,
    )
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description=(
            "Optional local agent backend for LLM calls. When unset, the Anthropic SDK path is used."
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description="Bypass stored gene annotations and recompute results.",
    )
    skip_literature_for_oncokb: bool = Field(
        default=False,
        description=(
            "When true, genes confirmed present in OncoKB return a deterministic "
            "OncoKB-based annotation without PubMed retrieval or LLM synthesis."
        ),
    )
    mode: AnnotationMode = Field(
        default="full",
        description=(
            "Use 'core' to prioritize cancer association, rationale, summary, citations, "
            "and evidence support."
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def coerce_string_fusions(cls, data: Any) -> Any:
        """Accept plain gene/fusion strings alongside structured FusionInput dicts."""
        if isinstance(data, dict) and "fusions" in data:
            coerced = []
            for item in data["fusions"]:
                if isinstance(item, str):
                    coerced.append({"fusion": item})
                else:
                    coerced.append(item)
            data = {**data, "fusions": coerced}
        return data


class GeneAnnotateRequest(BaseModel):
    gene: str = Field(..., description="Single gene symbol to annotate")
    tumor_type: Optional[str] = Field(default=None, description="Tumor type for literature retrieval")
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description=(
            "Optional local agent backend for LLM calls. When unset, the Anthropic SDK path is used."
        ),
    )
    force_refresh: bool = Field(
        default=False,
        description="Bypass stored gene annotations and recompute results.",
    )
    skip_literature_for_oncokb: bool = Field(
        default=False,
        description=(
            "When true, a gene confirmed present in OncoKB returns a deterministic "
            "OncoKB-based annotation without PubMed retrieval or LLM synthesis."
        ),
    )
    mode: AnnotationMode = Field(
        default="full",
        description=(
            "Use 'core' to prioritize cancer association, rationale, summary, citations, "
            "and evidence support."
        ),
    )


class AnnotationResult(BaseModel):
    run_id: str
    timestamp: str
    fusions_processed: int
    genes_annotated: int
    annotations: List[GeneAnnotation]
    fusion_evidence: List[FusionEvidenceResult] = Field(default_factory=list)
    timings_ms: Dict[str, float] = Field(default_factory=dict)


FeedbackCategory = Literal["bug", "feature_request", "gene_annotation_issue", "other"]


class FeedbackRequest(BaseModel):
    category: FeedbackCategory
    message: str = Field(..., min_length=1, description="Free-text feedback from the curator")
    contact_email: Optional[str] = Field(default=None, description="Optional email for follow-up")
    run_id: Optional[str] = Field(
        default=None, description="Run ID this feedback pertains to, so the run can be reproduced"
    )
    gene: Optional[str] = Field(
        default=None, description="Specific gene within the run this feedback pertains to, if any"
    )
    page_url: Optional[str] = Field(default=None, description="URL of the page feedback was submitted from")


class FeedbackResponse(BaseModel):
    feedback_id: str
    issue_title: Optional[str] = None
    issue_body: Optional[str] = None
    issue_url: Optional[str] = None
