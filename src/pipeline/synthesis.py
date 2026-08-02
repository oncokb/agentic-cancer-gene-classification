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
from src.models.schema import AnnotationMode, GeneAnnotation, LiteratureRecord
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
- `cancer_type_prevalence`: cancer types and alteration contexts for this gene, as a concise bullet list — one cancer type + alteration context per line, each prefixed with "- ", e.g.:
  "- Lung adenocarcinoma (fusion)\n- Breast cancer (amplification)"
  Keep each bullet short (cancer type + alteration type only, no extended explanation). Use the MSK/GENIE prevalence value from deterministic facts when available, reformatted into this bullet style; otherwise infer from the retrieved literature and list the cancer types explicitly mentioned in the abstracts. Never mention MSK/GENIE, prevalence-data availability, or any other meta-commentary about the data source in this field — output only the bullets themselves.
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

CORE_SYSTEM_PROMPT = """\
You are a cancer genomics expert filling only the latency-sensitive core fields for a gene triage result.

Return a compact annotation through the `annotate_gene` tool:
- `cancer_associated`: true only for credible peer-reviewed cancer evidence.
- `cancer_association_rationale`: one concise sentence naming evidence type(s) and cancer context.
- `gene_summary`: 1-2 concise sentences grounded only in the provided abstracts, with inline PMIDs.
- `citations`: strongest PMIDs from the provided abstracts only.
- `insufficient_evidence`: true when the provided abstracts do not support a confident call.

Do not produce supporting quotes, pathways, prevalence, gene class, or extended background.
Never invent PMIDs or use facts outside the provided abstracts.
Prefer a precise short answer over a broad answer.
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
            "cancer_type_prevalence": {
                "type": "string",
                "description": (
                    "Cancer types and alteration contexts for this gene, as a concise bullet "
                    "list — one per line, each prefixed with '- ' (e.g., "
                    "'- Lung adenocarcinoma (fusion)\\n- Breast cancer (amplification)'). "
                    "No mention of MSK/GENIE or data-source availability, bullets only. "
                    "Use the MSK/GENIE value from deterministic facts if available, "
                    "reformatted into bullets; otherwise infer from retrieved literature."
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
            "supporting_quotes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["pmid", "quote"],
                    "properties": {
                        "pmid": {
                            "type": "string",
                            "description": "PMID of the source abstract.",
                        },
                        "quote": {
                            "type": "string",
                            "description": (
                                "1–2 sentence verbatim or near-verbatim passage from the abstract "
                                "that directly supports the cancer_association_rationale. "
                                "Must be traceable to the retrieved text — do not paraphrase or invent."
                            ),
                        },
                    },
                },
                "description": (
                    "1–3 direct quotes from retrieved abstracts grounding the rationale. "
                    "Only include quotes from PMIDs that appear in the citations list. "
                    "Omit entirely if insufficient_evidence is true."
                ),
            },
        },
    },
}

CORE_ANNOTATE_TOOL: dict = {
    "name": "annotate_gene",
    "description": (
        "Produce the latency-sensitive core cancer gene annotation fields grounded in retrieved literature. "
        "Only cite PMIDs explicitly provided in the context."
    ),
    "input_schema": {
        "type": "object",
        "required": [
            "cancer_associated",
            "insufficient_evidence",
            "cancer_association_rationale",
            "gene_summary",
            "citations",
        ],
        "properties": {
            "cancer_associated": {
                "type": "boolean",
                "description": "Whether retrieved abstracts support a cancer association.",
            },
            "insufficient_evidence": {
                "type": "boolean",
                "description": "True when the retrieved abstracts are too weak or sparse.",
            },
            "cancer_association_rationale": {
                "type": "string",
                "description": "One concise sentence naming evidence type(s) and cancer context.",
            },
            "gene_summary": {
                "type": "string",
                "description": "1-2 concise sentences with inline PMIDs from provided abstracts only.",
            },
            "citations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Strongest supporting PMIDs from the provided abstracts only.",
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
    mode: AnnotationMode = "full",
) -> str:
    tier_label = (
        "Tier 1 (direct NCBI structured query — abundant indexed literature)"
        if retrieval_tier == 1
        else "Tier 2 (Claude agentic retrieval — sparse initial results required expanded search)"
    )
    if mode == "core":
        records = records[: settings.core_synthesis_max_papers]
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
    if mode == "core":
        lines += [
            "",
            "Core mode: return only the decision, one-sentence rationale, 1-2 sentence "
            "summary, citations, and insufficient_evidence flag.",
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
                f"Abstract: {rec.abstract[:settings.core_synthesis_abstract_chars] if mode == 'core' else rec.abstract}",
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


def _postprocess_synthesis_output(
    *,
    gene: str,
    tool_input: Dict,
    records: List[LiteratureRecord],
    gene_identity: Optional[str],
) -> Dict:
    """Apply deterministic citation verification to a raw synthesis tool response."""
    if "citations" in tool_input:
        tool_input["citations"] = _verify_citations(
            gene,
            tool_input["citations"],
            records,
            settings.max_citations_per_annotation,
            gene_identity,
        )
    return tool_input


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


def _needs_synthesis_escalation(
    *,
    tool_input: Dict,
    records: List[LiteratureRecord],
    retrieval_tier: int,
    mode: AnnotationMode,
) -> tuple[bool, str]:
    """Decide whether the fast synthesis pass needs a deeper model pass."""
    if not settings.synthesis_model_escalation:
        return False, "disabled"
    if not records:
        return False, "no_retrieved_literature"
    tier2_escalation = (
        settings.core_synthesis_escalation_tier2
        if mode == "core"
        else settings.synthesis_escalation_tier2
    )
    if retrieval_tier == 2 and tier2_escalation:
        return True, "tier2_retrieval"
    if not tool_input:
        return True, "empty_synthesis"
    if not tool_input.get("gene_summary"):
        return True, "missing_gene_summary"
    if not tool_input.get("cancer_association_rationale"):
        return True, "missing_rationale"

    citations = tool_input.get("citations", []) or []
    if (
        len(records) >= settings.min_papers_for_strong_association
        and len(citations) < settings.synthesis_escalation_min_citations
    ):
        return True, "too_few_verified_citations"

    score, _ = _evidence_support(
        citations=citations,
        records=records,
        insufficient_evidence=bool(tool_input.get("insufficient_evidence")),
        cancer_association_rationale=tool_input.get("cancer_association_rationale"),
        gene_summary=tool_input.get("gene_summary"),
    )
    min_support_score = (
        settings.core_synthesis_escalation_min_support_score
        if mode == "core"
        else settings.synthesis_escalation_min_support_score
    )
    if len(records) >= settings.min_papers_for_strong_association and score < min_support_score:
        return True, f"low_support_score_{score:.2f}"
    return False, "fast_pass_sufficient"


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
    mode: AnnotationMode = "full",
) -> Dict:
    """
    Call Claude to produce a structured annotation. Returns raw tool-use input dict.
    Raises on API error.
    """
    prompt_records = (
        records[: settings.core_synthesis_max_papers]
        if mode == "core"
        else records
    )
    user_prompt = _build_user_prompt(
        gene,
        fusions,
        in_oncokb,
        cancer_type_prevalence,
        prompt_records,
        retrieval_tier,
        gene_identity,
        mode,
    )
    tool = CORE_ANNOTATE_TOOL if mode == "core" else ANNOTATE_TOOL
    system_prompt = CORE_SYSTEM_PROMPT if mode == "core" else SYSTEM_PROMPT
    max_tokens = settings.core_synthesis_max_tokens if mode == "core" else 2048

    fast_model = settings.synthesis_fast_model if settings.synthesis_model_escalation else settings.synthesis_model
    fast_purpose = "synthesis_fast" if settings.synthesis_model_escalation else "synthesis"
    tool_input = await complete_with_tool(
        model=fast_model,
        system=system_prompt,
        user=user_prompt,
        tool=tool,
        max_tokens=max_tokens,
        local_mode=local_mode,
        local_backend=local_backend,
        model_purpose=fast_purpose,
    )

    if not tool_input:
        logger.error("No annotation returned for gene %s", gene)
        tool_input = {"insufficient_evidence": True, "cancer_associated": None}

    tool_input = _postprocess_synthesis_output(
        gene=gene,
        tool_input=tool_input,
        records=prompt_records,
        gene_identity=gene_identity,
    )
    should_escalate, reason = _needs_synthesis_escalation(
        tool_input=tool_input,
        records=records,
        retrieval_tier=retrieval_tier,
        mode=mode,
    )
    if should_escalate and not local_mode and settings.synthesis_model != fast_model:
        logger.info(
            "Escalating synthesis for %s from %s to %s: %s",
            gene,
            fast_model,
            settings.synthesis_model,
            reason,
        )
        deep_input = await complete_with_tool(
            model=settings.synthesis_model,
            system=system_prompt,
            user=user_prompt,
            tool=tool,
            max_tokens=max_tokens,
            local_mode=local_mode,
            local_backend=local_backend,
            model_purpose="synthesis",
        )
        if deep_input:
            tool_input = _postprocess_synthesis_output(
                gene=gene,
                tool_input=deep_input,
                records=prompt_records,
                gene_identity=gene_identity,
            )
        else:
            logger.warning("Deep synthesis returned no annotation for gene %s; keeping fast pass", gene)
    else:
        logger.info("Synthesis fast pass accepted for %s: %s", gene, reason)

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
    verified_pmids = set(citations)
    raw_quotes = synthesis_result.get("supporting_quotes", []) or []
    verified_quotes = [
        quote
        for quote in raw_quotes
        if isinstance(quote, dict) and quote.get("pmid") in verified_pmids
    ]
    effective_prevalence = cancer_type_prevalence or synthesis_result.get("cancer_type_prevalence")
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
        cancer_type_prevalence=effective_prevalence,
        gene_class=synthesis_result.get("gene_class"),
        signaling_pathways=synthesis_result.get("signaling_pathways"),
        gene_summary=synthesis_result.get("gene_summary"),
        citations=citations,
        supporting_quotes=verified_quotes,
        retrieval_count=len(records),
        retrieved_pmids=[record.pmid for record in records],
        insufficient_evidence=insufficient_evidence,
        evidence_support_score=evidence_support_score,
        evidence_support_explanation=evidence_support_explanation,
        cache_status="refreshed",
        cache_reason="computed_new_annotation",
    )
