from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

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
    publication_types: list[str] = []


class SupportingQuote(BaseModel):
    pmid: str
    quote: str


class EvidenceCard(BaseModel):
    pmid: str
    title: str = ""
    journal: str = ""
    evidence_type: str = "other"
    selected_reason: str = ""
    quote: Optional[str] = None


class QualityFlag(BaseModel):
    code: str
    label: str
    severity: Literal["info", "warning", "critical"] = "info"
    detail: str = ""


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
    quality_flags: List[QualityFlag] = Field(default_factory=list)
    date_annotated: str = Field(
        default_factory=lambda: date.today().strftime("%-m/%-d/%y")
    )

    # Internal quality metadata (not exported to Nicole's sheet)
    retrieval_count: int = 0
    retrieved_pmids: List[str] = Field(default_factory=list)
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
    timings_ms: Dict[str, float] = Field(default_factory=dict)
