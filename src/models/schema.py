from __future__ import annotations

from datetime import date, datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, PrivateAttr, constr, model_validator

LocalBackend = Literal["claude-code", "codex", "antigravity"]
AnnotationMode = Literal["full", "core"]
CacheStatus = Literal["miss", "reused", "refreshed", "bypassed"]
PMIDEvidenceType = Literal["clinical", "preclinical", "case_report", "review", "other"]
PMIDOncogenicRole = Literal["oncogene", "tumor_suppressor", "resistance", "neutral", "unknown"]


class ResolvedGene(BaseModel):
    """Result of HGNC normalization for a single gene symbol."""
    input_symbol: str
    canonical_symbol: Optional[str] = None
    hgnc_id: Optional[str] = None
    name: Optional[str] = None
    alias_symbols: List[str] = Field(default_factory=list)
    # HGNC's separate "prev_symbol" field: retired/superseded HUGO symbols
    # for this gene (distinct from alias_symbol, which is a synonym HGNC
    # never formally assigned as the gene's own symbol). For KAT6A, HGNC
    # lists MOZ under alias_symbol but MYST3/ZNF220 under prev_symbol —
    # legacy fusion literature uses both kinds of name interchangeably, so
    # callers wanting full legacy-nomenclature coverage should union both.
    prev_symbols: List[str] = Field(default_factory=list)
    locus_type: Optional[str] = None
    resolved: bool
    unresolvable: bool = False  # bare Ensembl ID or unannotated locus
    # True when unresolvable only because the lookup service failed (timeout,
    # 5xx, ...), as opposed to the service confirming the symbol doesn't exist.
    lookup_failed: bool = False
    # Why an Ensembl/RefSeq-form input (ENST..., GENE(NM_...), NM_...:c....)
    # could not be used, e.g. an unknown ID or a transcript of a different gene.
    resolution_error: Optional[str] = None
    # Transcript an ENST/ENSP input resolved through (ENSP -> its parent ENST).
    ensembl_transcript_id: Optional[str] = None
    # RefSeq transcript (NM_/NR_/XM_/XR_) an input named; None for NP_/XP_ proteins.
    refseq_transcript_id: Optional[str] = None


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


class PMIDEvidenceRecord(BaseModel):
    """A permanent, MySQL-persisted distillation of one published abstract.

    Published papers are immutable historical records, so — unlike the
    Redis-backed literature/OpenEvidence caches, which carry TTLs — this is
    never expired. Once a PMID has been distilled into a short
    `distilled_takeaway`, every future synthesis run across every gene that
    retrieves this PMID can reuse it instead of re-spending ~450 words of
    raw abstract tokens on it (see synthesis.py's _build_user_prompt)."""

    pmid: str
    doi: Optional[str] = None
    title: str
    journal: str
    publication_year: Optional[int] = None
    evidence_type: PMIDEvidenceType = "other"
    oncogenic_role: PMIDOncogenicRole = "unknown"
    distilled_takeaway: str
    supporting_quote: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


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
    # PubMed PublicationType values for the record, and whether one of them is
    # "Preprint" (a bioRxiv/medRxiv preprint, not peer reviewed). Optional and
    # additive: cards cached before these fields existed load as not-preprint.
    publication_types: List[str] = Field(default_factory=list)
    is_preprint: bool = False
    evidence_type: str = "other"
    selected_reason: str = ""
    quote: Optional[str] = None
    abstract: Optional[str] = None


class AliasMatch(BaseModel):
    """One fusion-partner gene matched in a record's text only via an HGNC
    alias/synonym symbol (e.g. gene=KAT6A, alias=MOZ) rather than its
    submitted/current HGNC symbol."""

    gene: str
    alias: str


class FusionEvidenceCard(EvidenceCard):
    fusion: str = ""
    # True when this record was found/matched only through an HGNC alias form
    # of one (or both) fusion partners rather than the literal submitted/
    # current symbol — e.g. a paper describing "MOZ-CBP" for a submitted
    # KAT6A::CREBBP query. False (with alias_matches empty) for a fully
    # literal match, which behaves exactly as before this field existed.
    matched_via_alias: bool = False
    alias_matches: List[AliasMatch] = Field(default_factory=list)
    requested_tumor_terms: List[str] = Field(
        default_factory=list,
        description=(
            "Requested-tumor terms (the tumor type and its known aliases) found in this paper's "
            "title or abstract. Empty when no tumor type was requested or none is mentioned. A "
            "mention only; it does not mean the paper shows the fusion in that tumor type."
        ),
    )


class FusionEvidenceResult(BaseModel):
    fusion: str
    partial: bool = Field(
        default=False,
        description="True when one exact-pair search failed; retained evidence is incomplete.",
    )
    input: Optional[str] = Field(
        default=None,
        description="The fusion as submitted, when it differs from `fusion` (e.g. Ensembl IDs resolved to symbols).",
    )
    tumor_type: Optional[str] = None
    well_supported: bool = False
    retrieved_count: int = 0
    pmids: List[str] = Field(default_factory=list)
    interpretation: str = ""
    evidence_cards: List[FusionEvidenceCard] = Field(default_factory=list)
    literature_scope: Optional[
        Literal["requested_tumor", "other_or_unspecified", "any_tumor", "no_literature"]
    ] = Field(
        default=None,
        description=(
            "How the exact-pair literature relates to the requested tumor type, computed over every "
            "retained PMID (not just the evidence cards). requested_tumor: at least one paper's "
            "title or abstract MENTIONS the requested tumor type or an alias; it does not mean the "
            "fusion was shown in that tumor type. other_or_unspecified: a tumor type was requested "
            "but no retained paper mentions it. any_tumor: no tumor type was requested and papers "
            "were found. no_literature: retrieval succeeded and no paper was retained. null when "
            "retrieval failed, no exact-pair search ran, or the result predates this field."
        ),
    )
    requested_tumor_pmids: List[str] = Field(
        default_factory=list,
        description=(
            "PMIDs, in ranked order, whose title or abstract mentions the requested tumor type. "
            "A subset of pmids; may include papers without an evidence card."
        ),
    )
    retrieval_status: Literal["ok", "failed"] = Field(
        default="ok",
        description=(
            "failed when exact-pair retrieval raised an error. An empty failed result is not "
            "evidence of missing literature, and its literature_scope is null."
        ),
    )


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
    abstract: Optional[str] = None


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
    reference_text: Optional[str] = None
    publication_info_string: Optional[str] = None
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


class OpenEvidenceGuideline(BaseModel):
    """One clinical practice guideline reference (NCCN/ASCO/ESMO) extracted
    from an OpenEvidenceAnalysis by distill_openevidence."""

    title: str
    url: str
    page_anchor: Optional[str] = None


class OpenEvidenceTrialMention(BaseModel):
    """One sentence from an OpenEvidenceAnalysis naming a clinical trial
    acronym or a trial outcome statistic (PFS/OS/HR), extracted by
    distill_openevidence."""

    trial: Optional[str] = None
    sentence: str


class DistilledOpenEvidence(BaseModel):
    """Deterministic distillation of an OpenEvidenceAnalysis into the pieces
    worth surfacing as an independent, non-blocking clinical reference card
    — never merged into GeneAnnotation or the synthesis prompt. See
    src.pipeline.openevidence.distill_openevidence.

    `citation_count`, `guidelines`, and `trial_mentions` reflect all of the
    source analysis's citations/mentions when built by distill_openevidence
    directly, or only the ones judged additive (not redundant with the core
    pipeline's own PubMed evidence) when built by
    src.pipeline.openevidence.distill_additive_openevidence — see that
    function for the additivity rule, including why a trial mention is
    dropped only when every citation backing it was itself dropped as
    redundant. `redundant_citation_count` is only ever populated by the
    latter (always 0 from distill_openevidence), and counts citations
    dropped as redundant.
    """

    question: str
    consensus_role: Optional[str] = None
    guidelines: List[OpenEvidenceGuideline] = Field(default_factory=list)
    trial_mentions: List[OpenEvidenceTrialMention] = Field(default_factory=list)
    citation_count: int = 0
    redundant_citation_count: int = 0


class ResolvedInput(BaseModel):
    """A submitted input that was rewritten before annotation, e.g. an Ensembl
    transcript ID resolved to its gene symbol."""

    input: str = Field(..., description="The input as submitted, e.g. `ENST00000269305`.")
    resolved: str = Field(..., description="The gene or fusion string that was annotated, e.g. `TP53`.")
    five_transcript: Optional[str] = Field(default=None, description="5' partner transcript taken from the input.")
    three_transcript: Optional[str] = Field(default=None, description="3' partner transcript taken from the input.")
    warnings: List[str] = Field(default_factory=list, description="Caveats about how the input was read.")


class GeneAnnotation(BaseModel):
    """One row in Nicole's spreadsheet, keyed by gene."""

    gene: str
    fusions: List[str] = Field(default_factory=list)
    resolved_inputs: List[ResolvedInput] = Field(
        default_factory=list,
        description=(
            "Submitted inputs for this gene that were rewritten before annotation (Ensembl IDs, RefSeq "
            "accessions, GENE(ENST...)/GENE(NM_...) and ENST...:c..../NM_...:c.... forms), with what each "
            "resolved to."
        ),
    )
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
    # In-process only (not in any schema, never serialized or persisted): the
    # tumor type the pipeline actually annotated this gene with, and whether
    # symbol resolution failed because the lookup service was unavailable.
    _analysis_tumor_type: Optional[str] = PrivateAttr(default=None)
    _symbol_lookup_failed: bool = PrivateAttr(default=False)

    @property
    def analysis_tumor_type(self) -> Optional[str]:
        """Tumor type run_pipeline annotated this gene with (None if none)."""
        return self._analysis_tumor_type

    @analysis_tumor_type.setter
    def analysis_tumor_type(self, value: Optional[str]) -> None:
        self._analysis_tumor_type = value

    @property
    def symbol_lookup_failed(self) -> bool:
        """True when the gene symbol couldn't be resolved because the lookup failed."""
        return self._symbol_lookup_failed

    @symbol_lookup_failed.setter
    def symbol_lookup_failed(self, value: bool) -> None:
        self._symbol_lookup_failed = value


class GeneAnnotationWithRun(GeneAnnotation):
    """A GeneAnnotation plus the saved run it belongs to (POST /v1/annotate/gene)."""

    run_id: Optional[str] = Field(
        default=None, description="ID of the saved annotation run containing this gene."
    )
    view_url: Optional[str] = Field(
        default=None,
        description="Absolute link that opens the full run in the ACGC UI (requires login).",
    )
    warnings: List[str] = Field(
        default_factory=list,
        description=(
            "Request-level warnings for the saved run, such as conflicting tumor types "
            "or fusion-like notation. Same list as AnnotationResult.warnings."
        ),
    )


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
        """Allow structured rows to name the required value as gene/input/query,
        or to give fusion partners separately as gene1/gene2."""
        if not isinstance(data, dict):
            return data
        if "gene1" in data or "gene2" in data:
            gene1 = str(data.get("gene1") or "").strip()
            gene2 = str(data.get("gene2") or "").strip()
            if not (gene1 and gene2):
                raise ValueError("gene1 and gene2 must both be given to describe a fusion")
            fusion = f"{gene1}::{gene2}"
            given = data.get("fusion")
            # Deferred: normalization imports this module.
            from src.pipeline.normalization import split_fusion

            if given is not None and tuple(
                (part or "").upper() for part in split_fusion(str(given))
            ) != (gene1.upper(), gene2.upper()):
                raise ValueError(
                    f"fusion '{given}' conflicts with gene1/gene2 ('{fusion}'); give one or the other"
                )
            data = {key: value for key, value in data.items() if key not in ("gene1", "gene2")}
            return {**data, "fusion": fusion if given is None else given}
        if "fusion" not in data:
            for alias in ("gene", "input", "query"):
                if alias in data:
                    return {**data, "fusion": data[alias]}
        return data


class AnnotateRequest(BaseModel):
    include_fusion_evidence: bool = Field(
        default=False,
        description="Retrieve exact-pair fusion evidence inline before returning and saving the annotation run.",
    )
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
        description="Bypass stored gene annotations and fusion evidence and recompute results.",
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
        if isinstance(data, dict) and isinstance(data.get("fusions"), list):
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
    fusion_evidence: List[FusionEvidenceResult] = Field(
        default_factory=list,
        description=(
            "Exact fusion-pair evidence, empty by default. Set include_fusion_evidence=true "
            "on the annotation request to populate it inline; otherwise run "
            "POST /v1/fusion-evidence/jobs (optionally with run_id) to populate it."
        ),
    )
    warnings: List[str] = Field(
        default_factory=list,
        description="Request-level warnings, including the tumor type used and ignored conflicting types per gene.",
    )
    timings_ms: Dict[str, float] = Field(default_factory=dict)


FeedbackCategory = Literal["bug", "feature_request", "gene_annotation_issue", "other"]


class FeedbackRequest(BaseModel):
    category: FeedbackCategory
    message: str = Field(..., min_length=1, max_length=4000, description="Free-text feedback from the curator")
    contact_email: Optional[str] = Field(default=None, description="Optional email for follow-up")
    run_id: Optional[str] = Field(
        default=None, description="Run ID this feedback pertains to, so the run can be reproduced"
    )
    gene: Optional[str] = Field(
        default=None, description="Specific gene within the run this feedback pertains to, if any"
    )
    page_url: Optional[str] = Field(
        default=None, max_length=2048, description="URL of the page feedback was submitted from"
    )
    pmids: Optional[List[constr(max_length=64)]] = Field(
        default=None,
        max_length=20,
        description=(
            "Optional structured list of PMIDs cited by the feedback. Values are "
            "parsed leniently; unparseable entries are reported in the citation check."
        ),
    )


class FeedbackResponse(BaseModel):
    feedback_id: str
    issue_title: Optional[str] = None
    issue_body: Optional[str] = None
    issue_url: Optional[str] = None
