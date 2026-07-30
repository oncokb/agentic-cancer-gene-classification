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
from src.pipeline.literature import _HIGH_IMPACT_JOURNALS, _publication_evidence_rank
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

## Field guidance:
- `cancer_associated`: true if there is credible peer-reviewed evidence linking this gene to cancer biology.
- `cancer_association_rationale`: list the evidence types (structural-variant, expression, mutation, methylation, copy-number) with a brief justification.
- `gene_class`: molecular/functional class (e.g., "Serine/threonine kinase", "RNA-binding protein", "Transcription factor").
- `signaling_pathways`: comma-separated associated signaling pathways (e.g., "PI3K/AKT", "RAS/MAPK", "WNT/β-catenin").
- Do not assign a generic certainty/probability score. The application calculates
  an evidence support score after PMID verification.

## Literature quality signals:
- Abstracts marked ★ are from high-impact journals (NEJM, Lancet, Nature, Cell, JCO, Cancer Cell, etc.).
  Weight these more heavily when the evidence they provide is directly relevant to the gene's cancer role.

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
        "required": ["cancer_associated", "insufficient_evidence"],
        "properties": {
            "cancer_associated": {
                "type": "boolean",
                "description": "Whether this gene has credible evidence of cancer association.",
            },
            "insufficient_evidence": {
                "type": "boolean",
                "description": (
                    "True when the retrieved literature is too sparse to make a confident determination. "
                    "Prefer this over a weakly supported guess."
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
            journal_tag = f" [{rec.journal}]" if rec.journal else ""
            impact_tag = " ★" if rec.journal in _HIGH_IMPACT_JOURNALS else ""
            lines += [
                "---",
                f"PMID: {rec.pmid}{journal_tag}{impact_tag}",
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


def _evidence_support(
    citations: List[str],
    records: List[LiteratureRecord],
    insufficient_evidence: bool,
    cancer_association_rationale: Optional[str],
    gene_summary: Optional[str],
) -> tuple[float, str]:
    """Calculate an explainable evidence-support score for curator-facing output."""
    cited_records = [record for record in records if record.pmid in set(citations)]
    evidence_records = cited_records or records
    score = 0.0
    reasons = []

    citation_count = len(citations)
    if citation_count >= 1:
        score += 0.20
        reasons.append(f"{citation_count} verified PMID citation(s)")
    if citation_count >= 3:
        score += 0.20
        reasons.append("3+ verified PMIDs")
    if citation_count >= 5:
        score += 0.10
        reasons.append("5+ verified PMIDs")

    if any(_publication_evidence_rank(record) == 0 for record in evidence_records):
        score += 0.20
        reasons.append("human clinical evidence signal")
    if any(record.journal in _HIGH_IMPACT_JOURNALS for record in evidence_records):
        score += 0.10
        reasons.append("high-impact journal signal")
    if citations and cancer_association_rationale and gene_summary and not insufficient_evidence:
        score += 0.10
        reasons.append("summary, rationale, and citations are all present")

    if insufficient_evidence:
        score -= 0.30
        reasons.append("insufficient-evidence flag")
    if not citations:
        score -= 0.20
        reasons.append("no verified citations")
    if len(records) < 2:
        score -= 0.10
        reasons.append("sparse retrieval corpus")

    score = round(min(1.0, max(0.0, score)), 2)
    if reasons:
        explanation = (
            f"Evidence support score {score:.2f}: "
            + "; ".join(reasons)
            + ". This score estimates literature grounding, not biological truth or clinical actionability."
        )
    else:
        explanation = (
            f"Evidence support score {score:.2f}: no strong support signals were detected. "
            "This score estimates literature grounding, not biological truth or clinical actionability."
        )
    return score, explanation


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
        return {"insufficient_evidence": True, "cancer_associated": None}

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
    citations = synthesis_result.get("citations", [])
    insufficient_evidence = synthesis_result.get("insufficient_evidence", False)
    evidence_support_score, evidence_support_explanation = _evidence_support(
        citations=citations,
        records=records,
        insufficient_evidence=insufficient_evidence,
        cancer_association_rationale=synthesis_result.get("cancer_association_rationale"),
        gene_summary=synthesis_result.get("gene_summary"),
    )
    return GeneAnnotation(
        gene=gene,
        fusions=list(dict.fromkeys(fusions)),  # deduplicate, preserve order
        in_oncokb=in_oncokb,
        cancer_associated=synthesis_result.get("cancer_associated"),
        cancer_association_rationale=synthesis_result.get("cancer_association_rationale"),
        cancer_type_prevalence=cancer_type_prevalence,
        gene_class=synthesis_result.get("gene_class"),
        signaling_pathways=synthesis_result.get("signaling_pathways"),
        gene_summary=synthesis_result.get("gene_summary"),
        citations=citations,
        retrieval_count=len(records),
        insufficient_evidence=insufficient_evidence,
        evidence_support_score=evidence_support_score,
        evidence_support_explanation=evidence_support_explanation,
        cache_status="refreshed",
        cache_reason="computed_new_annotation",
    )
