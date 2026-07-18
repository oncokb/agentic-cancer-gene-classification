from __future__ import annotations

from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


CancerTier = Literal[
    "Class I - Driver",
    "Class II - Likely Driver",
    "Class III - Cancer Relevant",
]

OgTsg = Literal["OG", "TSG", "OG, TSG"]
LocalBackend = Literal["claude-code", "codex", "copilot", "antigravity"]
InstallableLocalBackend = Literal["claude-code", "codex", "copilot", "antigravity"]
LoginableLocalBackend = Literal["copilot"]


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


class GeneAnnotation(BaseModel):
    """One row in Nicole's spreadsheet, keyed by gene."""

    gene: str
    fusions: List[str] = Field(default_factory=list)
    in_oncokb: Optional[bool] = None  # None when OncoKB token not configured

    cancer_associated: Optional[bool] = None
    cancer_association_rationale: Optional[str] = None
    cancer_associated_gene_tier: Optional[CancerTier] = None
    og_or_tsg: Optional[OgTsg] = None
    cancer_type_prevalence: Optional[str] = None
    gene_class: Optional[str] = None
    signaling_pathways: Optional[str] = None
    gene_summary: Optional[str] = None
    citations: List[str] = Field(default_factory=list)  # verified supporting PMIDs only
    date_annotated: str = Field(
        default_factory=lambda: date.today().strftime("%-m/%-d/%y")
    )

    # Internal quality metadata (not exported to Nicole's sheet)
    retrieval_count: int = 0
    retrieved_pmids: List[str] = Field(default_factory=list)
    insufficient_evidence: bool = False
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    error: Optional[str] = None


class AnnotateRequest(BaseModel):
    fusions: List[str] = Field(
        ...,
        description="Gene fusions in GENE1::GENE2 or GENE1--GENE2 format",
        min_length=1,
    )
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description=(
            "Optional local agent backend for LLM calls. When unset, the Anthropic SDK path is used."
        ),
    )


class LocalBackendStatus(BaseModel):
    backend: LocalBackend
    command: str
    installed: bool
    version: Optional[str] = None
    path: Optional[str] = None
    error: Optional[str] = None


class LocalBackendsStatusResponse(BaseModel):
    backends: List[LocalBackendStatus]
    setup_required: bool
    minimum_setup_complete: bool
    anthropic_sdk_configured: bool
    local_backend_configured: bool
    oncokb_configured: bool
    setup_messages: List[str] = Field(default_factory=list)
    operating_system: str


class LocalBackendInstallRequest(BaseModel):
    backend: InstallableLocalBackend


class LocalBackendInstallerInfo(BaseModel):
    backend: InstallableLocalBackend
    supported: bool
    command: List[str]
    display_command: str
    setup_url: Optional[str] = None
    post_install_steps: List[str] = Field(default_factory=list)


class LocalBackendInstallResponse(BaseModel):
    backend: InstallableLocalBackend
    installed: bool
    return_code: int
    command: str
    stdout: str = ""
    stderr: str = ""
    next_steps: List[str] = Field(default_factory=list)


class LocalBackendPrepareResponse(BaseModel):
    configured_count: int
    configured_paths: Dict[LocalBackend, str] = Field(default_factory=dict)
    config_path: str
    message: str


class LocalBackendLoginRequest(BaseModel):
    backend: LoginableLocalBackend


class LocalBackendLoginResponse(BaseModel):
    backend: LoginableLocalBackend
    return_code: int
    command: str
    stdout: str = ""
    stderr: str = ""
    next_steps: List[str] = Field(default_factory=list)


class BenchmarkRequest(BaseModel):
    local_backend: Optional[LocalBackend] = Field(
        default=None,
        description="Optional local backend for benchmark pipeline calls.",
    )
    no_judge: bool = Field(
        default=True,
        description="Skip LLM-as-a-judge summary scoring.",
    )


class AnnotationResult(BaseModel):
    run_id: str
    timestamp: str
    fusions_processed: int
    genes_annotated: int
    annotations: List[GeneAnnotation]
    run_error: Optional[str] = None


class GoogleSheetExportRequest(BaseModel):
    result: AnnotationResult
    spreadsheet_id: str = Field(..., min_length=1)
    sheet_name: str = Field(default="Annotation Results", min_length=1)


class GoogleSheetExportResponse(BaseModel):
    spreadsheet_id: str
    sheet_name: str
    updated_range: str
    updated_rows: int
    updated_columns: int
    spreadsheet_url: str


GoogleSheetsCredentialsSource = Literal["environment", "local_upload"]
OncoKBTokenSource = Literal["environment", "local_upload"]
NCBIAPIKeySource = Literal["environment", "local_upload"]


class GoogleSheetsConfigStatus(BaseModel):
    configured: bool
    source: Optional[GoogleSheetsCredentialsSource] = None
    service_account_email: Optional[str] = None
    credentials_path: Optional[str] = None


class GoogleSheetsServiceAccountConfigRequest(BaseModel):
    service_account_json: str = Field(..., min_length=1)


class GoogleSheetsServiceAccountConfigResponse(BaseModel):
    configured: bool
    service_account_email: str
    credentials_path: str
    message: str


class OncoKBConfigStatus(BaseModel):
    configured: bool
    source: Optional[OncoKBTokenSource] = None


class OncoKBTokenConfigRequest(BaseModel):
    api_token: str = Field(..., min_length=1)


class OncoKBTokenConfigResponse(BaseModel):
    configured: bool
    source: OncoKBTokenSource
    message: str


class NCBIConfigStatus(BaseModel):
    configured: bool
    source: Optional[NCBIAPIKeySource] = None


class NCBIAPIKeyConfigRequest(BaseModel):
    api_key: str = Field(..., min_length=1)


class NCBIAPIKeyConfigResponse(BaseModel):
    configured: bool
    source: NCBIAPIKeySource
    message: str


class BenchmarkResult(BaseModel):
    n_genes: int
    categorical_metrics: Dict
    per_gene_report: List[Dict]
    judge: Optional[Dict] = None
    pipeline_result: AnnotationResult
