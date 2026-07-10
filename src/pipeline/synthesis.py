"""
Retrieval-grounded LLM synthesis via Claude.
Reads retrieved literature + deterministic facts and fills the annotation schema.
Enforces three invariants:
  1. Every summary claim must cite a retrieved abstract.
  2. Every emitted PMID must exist in the retrieved set (verified post-response).
  3. "insufficient_evidence" is treated as a valid first-class output.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from src.config import settings
from src.models.schema import GeneAnnotation, LiteratureRecord
from src.pipeline.citation_precision import filter_and_rank_citations
from src.pipeline.llm_client import complete_with_tool

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are a cancer genomics expert filling structured annotation rows for the OncoKB MSK TARGET Gene Triaging database.

You will receive:
1. A gene name and its associated fusion partners.
2. Deterministic facts from authoritative databases (HGNC identity, OncoKB membership, prevalence).
3. Retrieved PubMed abstracts (each with its PMID).

Your task is to call the `annotate_gene` tool with a structured annotation.

## Hard constraints — never violate these:
- Every claim in `gene_summary` must be directly traceable to one of the retrieved abstracts.
- `citations` must ONLY contain the strongest PMIDs that appear in the provided retrieved abstracts list.
- Do NOT invent, guess, or recall PMIDs from memory. If a fact cannot be grounded in the retrieved set, omit it.
- A fabricated PMID will cause patient safety errors. Treat citation fabrication as the most critical failure mode.
- Prefer citation precision over citation volume. Do not cite loosely related background papers just because they were retrieved.
- Use the HGNC identity to avoid same-symbol ambiguity. Do not cite papers that use the same symbol
  for a different entity, such as an lncRNA/circRNA/transcript name unrelated to the HGNC gene.
- If the retrieved evidence is insufficient to make a determination, set `insufficient_evidence: true` and leave classification fields null. This is a valid, preferred output over hallucination.
- If `cancer_associated` is false OR `insufficient_evidence` is true, you MUST leave `cancer_associated_gene_tier` and `og_or_tsg` null. Do not fill these fields for non-cancer genes or genes with insufficient evidence.

## Field guidance:
- `cancer_associated`: true if there is credible peer-reviewed evidence linking this gene to cancer biology.
- `cancer_association_rationale`: list the evidence types (structural-variant, expression, mutation, methylation, copy-number) with a brief justification.
- `cancer_associated_gene_tier`: ONLY set this when `cancer_associated` is true. Use the most conservative tier supported by the evidence — do not promote a gene's tier beyond what the retrieved abstracts directly demonstrate:
    - "Class I - Driver": high bar — requires recurrent somatic mutations with direct functional validation (e.g., murine models, CRISPR knockouts demonstrating tumour initiation), OR recurrent oncogenic fusions with demonstrated transforming activity. Expression/correlation data alone does NOT qualify.
    - "Class II - Likely Driver": expression upregulation, copy-number alteration, or functional knockdown/overexpression in cancer cell lines or xenografts with a plausible mechanistic hypothesis. No requirement for in vivo murine tumour initiation models.
    - "Class III - Cancer Relevant": indirect or contextual association only — prognostic signature membership, immune microenvironment role, metabolic co-dependency, or single-study correlation without mechanistic follow-up. When in doubt between Class II and Class III, choose Class III.
- `og_or_tsg`: ONLY set this when `cancer_associated` is true AND `cancer_associated_gene_tier` is "Class I - Driver" or "Class II - Likely Driver". Leave null for "Class III - Cancer Relevant" genes — contextual or indirect associations do not warrant a directional OG/TSG call. "OG" (promotes growth/survival), "TSG" (suppresses growth), "OG, TSG" (context-dependent dual role with evidence for both in the retrieved abstracts).
- `gene_class`: molecular/functional class (e.g., "Serine/threonine kinase", "RNA-binding protein", "Transcription factor").
- `signaling_pathways`: comma-separated canonical pathways (e.g., "PI3K/AKT", "RAS/MAPK", "WNT/β-catenin").
- `confidence`: 0.0–1.0 reflecting how well the retrieved evidence supports the annotation.
  - >4 papers with direct functional evidence → 0.8–1.0
  - 2–4 papers with functional/expression data → 0.5–0.8
  - <2 papers or only indirect evidence → 0.2–0.5
  - 0 papers → set insufficient_evidence: true, confidence: 0.0

## Retrieval provenance:
The context will tell you which retrieval tier sourced the literature:
- **Tier 1** (direct NCBI structured query): well-characterised gene with abundant indexed literature.
- **Tier 2** (Claude agentic retrieval): sparse initial results; Claude searched iteratively using aliases,
  fusion-specific terms, and pathway names to surface relevant evidence.
End the `gene_summary` with one parenthetical sentence noting the retrieval tier, for example:
  "(Literature sourced via Tier 1 direct PubMed query.)" or
  "(Literature sourced via Tier 2 Claude agentic retrieval — sparse initial results required expanded search.)"
"""

ANNOTATE_TOOL: dict = {
    "name": "annotate_gene",
    "description": (
        "Produce a structured cancer gene annotation grounded in the retrieved literature. "
        "Only cite PMIDs explicitly provided in the context."
    ),
    "input_schema": {
        "type": "object",
        "required": ["cancer_associated", "insufficient_evidence", "confidence"],
        "properties": {
            "cancer_associated": {
                "type": "boolean",
                "description": "Whether this gene has credible evidence of cancer association.",
            },
            "insufficient_evidence": {
                "type": "boolean",
                "description": (
                    "True when the retrieved literature is too sparse to make a confident determination. "
                    "Prefer this over a low-confidence guess."
                ),
            },
            "cancer_association_rationale": {
                "type": "string",
                "description": (
                    "Brief rationale covering evidence types observed "
                    "(structural-variant, expression, mutation, methylation, copy-number) "
                    "and which cancer types."
                ),
            },
            "cancer_associated_gene_tier": {
                "type": "string",
                "enum": ["Class I - Driver", "Class II - Likely Driver", "Class III - Cancer Relevant"],
                "description": "Driver tier based on strength of functional evidence.",
            },
            "og_or_tsg": {
                "type": "string",
                "enum": ["OG", "TSG", "OG, TSG"],
                "description": "Oncogene, tumor suppressor, or context-dependent dual role.",
            },
            "gene_class": {
                "type": "string",
                "description": "Molecular/functional class of the gene product.",
            },
            "signaling_pathways": {
                "type": "string",
                "description": "Comma-separated associated signaling pathways.",
            },
            "gene_summary": {
                "type": "string",
                "description": (
                    "2–5 sentence prose summary of cancer relevance grounded in retrieved abstracts. "
                    "Cite PMIDs inline as (PMID XXXXXXXX). Only cite retrieved PMIDs."
                ),
            },
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"List of up to {settings.max_citations_per_annotation} strongest PMIDs "
                    "supporting this annotation. MUST be a subset of the retrieved abstracts provided. "
                    "No extras."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence score 0–1 reflecting evidence quality and quantity.",
            },
        },
    },
}


def _build_user_prompt(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int,
    gene_identity: Optional[str] = None,
) -> str:
    tier_label = (
        "Tier 1 (direct NCBI structured query — abundant indexed literature)"
        if retrieval_tier == 1
        else "Tier 2 (Claude agentic retrieval — sparse initial results required expanded search)"
    )
    lines = [
        f"## Gene: {gene}",
        f"Associated fusions: {', '.join(fusions) if fusions else 'none'}",
        f"Retrieval tier: {tier_label}",
        "",
        "### Deterministic database facts (do not contradict or regenerate):",
        f"- HGNC identity: {gene_identity or 'not available'}",
        f"- In OncoKB: {'Yes' if in_oncokb else ('No' if in_oncokb is False else 'Unknown (token not configured)')}",
        f"- Cancer-type prevalence (MSK/GENIE): {cancer_type_prevalence or 'not available'}",
        "",
        f"### Retrieved PubMed abstracts ({len(records)} papers):",
    ]

    if not records:
        lines.append("No abstracts retrieved. Set insufficient_evidence: true.")
    else:
        for rec in records:
            lines += [
                "---",
                f"PMID: {rec.pmid}",
                f"Title: {rec.title}",
                f"Abstract: {rec.abstract}",
            ]

    return "\n".join(lines)


def _verify_citations(
    gene: str,
    citations: List[str],
    records: List[LiteratureRecord],
    max_citations: int,
    gene_identity: Optional[str] = None,
) -> List[str]:
    """
    Remove ambiguous or unretrieved PMIDs from the LLM's citation list, then rank.
    An identifier that was not retrieved is a rejection, not a warning.
    """
    retrieved_pmids = {record.pmid for record in records}
    verified = filter_and_rank_citations(
        gene=gene,
        emitted_citations=citations,
        records=records,
        max_citations=max_citations,
        gene_identity=gene_identity,
        min_score=-99,
    )
    rejected = set(citations) - retrieved_pmids
    if rejected:
        logger.warning(
            "Rejected %d unverified PMIDs from LLM output: %s",
            len(rejected),
            rejected,
        )
    if len(citations) > len(verified):
        logger.info(
            "Kept %d/%d emitted citations after PMID verification, identity filtering, and precision cap",
            len(verified),
            len(citations),
        )
    return verified


async def synthesize_gene_annotation(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    retrieval_tier: int = 1,
    gene_identity: Optional[str] = None,
    local_mode: bool = False,
    local_backend: Optional[str] = None,
) -> Dict:
    """
    Call Claude to produce a structured annotation. Returns raw tool-use input dict.
    Raises on API error.
    """
    user_prompt = _build_user_prompt(
        gene,
        fusions,
        in_oncokb,
        cancer_type_prevalence,
        records,
        retrieval_tier,
        gene_identity,
    )
    tool_input = await complete_with_tool(
        model=settings.synthesis_model,
        system=SYSTEM_PROMPT,
        user=user_prompt,
        tool=ANNOTATE_TOOL,
        max_tokens=2048,
        local_mode=local_mode,
        local_backend=local_backend,
    )

    if not tool_input:
        logger.error("No annotation returned for gene %s", gene)
        return {"insufficient_evidence": True, "cancer_associated": None, "confidence": 0.0}

    # PMID verification — reject any citation not in retrieved set
    if "citations" in tool_input:
        tool_input["citations"] = _verify_citations(
            gene,
            tool_input["citations"],
            records,
            settings.max_citations_per_annotation,
            gene_identity,
        )

    return tool_input


def build_gene_annotation(
    gene: str,
    fusions: List[str],
    in_oncokb: Optional[bool],
    cancer_type_prevalence: Optional[str],
    records: List[LiteratureRecord],
    synthesis_result: Dict,
) -> GeneAnnotation:
    """Merge synthesis output with deterministic facts into a GeneAnnotation."""
    tier = synthesis_result.get("cancer_associated_gene_tier")
    og_or_tsg = synthesis_result.get("og_or_tsg")
    cancer_associated = synthesis_result.get("cancer_associated")
    insufficient_evidence = synthesis_result.get("insufficient_evidence", False)

    if cancer_associated is False or insufficient_evidence:
        tier = None
        og_or_tsg = None
    elif in_oncokb is False and tier == "Class I - Driver":
        logger.info(
            "Downgrading non-OncoKB Class I call for %s to Class II pending stronger curation",
            gene,
        )
        tier = "Class II - Likely Driver"

    return GeneAnnotation(
        gene=gene,
        fusions=list(dict.fromkeys(fusions)),  # deduplicate, preserve order
        in_oncokb=in_oncokb,
        cancer_associated=cancer_associated,
        cancer_association_rationale=synthesis_result.get("cancer_association_rationale"),
        cancer_associated_gene_tier=tier,
        og_or_tsg=og_or_tsg,
        cancer_type_prevalence=cancer_type_prevalence,
        gene_class=synthesis_result.get("gene_class"),
        signaling_pathways=synthesis_result.get("signaling_pathways"),
        gene_summary=synthesis_result.get("gene_summary"),
        citations=synthesis_result.get("citations", []),
        retrieval_count=len(records),
        insufficient_evidence=insufficient_evidence,
        confidence=synthesis_result.get("confidence", 0.0),
    )
