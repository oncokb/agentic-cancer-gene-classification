"""Tests wiring OpenEvidence supplementary evidence into synthesis/orchestrator.

Covers the acceptance contract for the integration:
  (a) OPENEVIDENCE_ENABLED=False is a zero-behavior-change path.
  (b) OpenEvidence citations never flow through citation verification as if
      they were verified PMID citations.
"""

from __future__ import annotations

from src.models.schema import (
    LiteratureRecord,
    OpenEvidenceAnalysis,
    OpenEvidenceCitation,
    ResolvedGene,
)
from src.pipeline import orchestrator, synthesis
from src.pipeline.openevidence import OpenEvidenceClient

RECORDS = [
    LiteratureRecord(pmid="1", title="Paper 1", abstract="GENE cancer", publication_types=["Journal Article"]),
]

FAKE_ANALYSIS = OpenEvidenceAnalysis(
    question="What does the evidence show about GENE's role in cancer?",
    text="OpenEvidence summary text about GENE.",
    citations=[
        OpenEvidenceCitation(
            citation_key="oe-99",
            title="An OpenEvidence-only source",
            journal="Some Journal",
            date="2021",
            url="https://example.com/oe-99",
        )
    ],
)


# ---------------------------------------------------------------------------
# (a) Disabled path is byte-for-byte unchanged.
# ---------------------------------------------------------------------------

def test_build_user_prompt_identical_without_openevidence_context():
    """Same call, only difference is the (unused, defaulted) new parameter."""
    args = ("GENE", [], False, None, RECORDS, 1, "GENE (HGNC:0000)", "full")

    before = synthesis._build_user_prompt(*args)
    after = synthesis._build_user_prompt(*args, openevidence_context=None)

    assert before == after
    assert "OpenEvidence" not in before


async def test_synthesize_gene_annotation_prompt_unchanged_when_no_context(monkeypatch):
    seen = {}

    async def fake_complete_with_tool(**kwargs):
        seen.update(kwargs)
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Supported by a retrieved PMID.",
            "gene_summary": "GENE is associated with cancer (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", False)

    result = await synthesis.synthesize_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=RECORDS,
        retrieval_tier=1,
    )

    assert "OpenEvidence" not in seen["user"]
    assert result["citations"] == ["1"]


async def test_annotate_gene_never_calls_openevidence_when_disabled(monkeypatch):
    async def fail_get_gene_analysis(self, gene, tumor_type=None, client=None):
        raise AssertionError("OpenEvidence must not be called when OPENEVIDENCE_ENABLED=false")

    async def fake_check_oncokb_membership(gene, lookup=None):
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        return (RECORDS, 1)

    async def fake_select_papers(*args, **kwargs):
        return args[1]

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        assert kwargs.get("openevidence_context") is None
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "GENE has retrieved cancer evidence (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", False)
    monkeypatch.setattr(OpenEvidenceClient, "get_gene_analysis", fail_get_gene_analysis)
    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation)

    annotation = await orchestrator._annotate_gene(
        gene="GENE",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="GENE", canonical_symbol="GENE", resolved=True),
        unresolvable=False,
        skip_literature_for_oncokb=False,
    )

    assert annotation.citations == ["1"]
    assert annotation.openevidence_supplementary is None
    assert "openevidence" not in annotation.timings_ms


# ---------------------------------------------------------------------------
# (b) Enabled path: supplementary section appears, citations stay unverified.
# ---------------------------------------------------------------------------

def test_build_user_prompt_includes_labeled_supplementary_section():
    prompt = synthesis._build_user_prompt(
        "GENE", [], False, None, RECORDS, 1, "GENE (HGNC:0000)", "full",
        openevidence_context=FAKE_ANALYSIS,
    )

    assert "Supplementary AI-synthesized evidence (unverified, from OpenEvidence)" in prompt
    assert FAKE_ANALYSIS.text in prompt
    assert "An OpenEvidence-only source" in prompt
    # The retrieved-PubMed section is untouched — still present above the supplement.
    assert "PMID: 1" in prompt


async def test_synthesize_gene_annotation_does_not_verify_openevidence_citations(monkeypatch):
    """The LLM must never be able to sneak an OpenEvidence citation_key into
    `citations` — _verify_citations() only accepts PMIDs from the retrieved set."""
    seen = {}

    async def fake_complete_with_tool(**kwargs):
        seen.update(kwargs)
        # Simulate a misbehaving model trying to cite the OpenEvidence source.
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Supported by a retrieved PMID.",
            "gene_summary": "GENE is associated with cancer (PMID 1).",
            "citations": ["1", "oe-99"],
        }

    monkeypatch.setattr(synthesis, "complete_with_tool", fake_complete_with_tool)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", False)

    result = await synthesis.synthesize_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=RECORDS,
        retrieval_tier=1,
        openevidence_context=FAKE_ANALYSIS,
    )

    assert "OpenEvidence" in seen["user"]
    # oe-99 was rejected by verification since it isn't a retrieved PMID.
    assert result["citations"] == ["1"]


def test_build_gene_annotation_surfaces_openevidence_without_touching_citations():
    synthesis_result = {
        "cancer_associated": True,
        "insufficient_evidence": False,
        "cancer_association_rationale": "Supported by a retrieved PMID.",
        "gene_summary": "GENE is associated with cancer (PMID 1).",
        "citations": ["1"],
    }

    annotation = synthesis.build_gene_annotation(
        gene="GENE",
        fusions=[],
        in_oncokb=False,
        cancer_type_prevalence=None,
        records=RECORDS,
        synthesis_result=synthesis_result,
        retrieval_tier=1,
        openevidence_context=FAKE_ANALYSIS,
    )

    assert annotation.citations == ["1"]
    assert annotation.openevidence_supplementary == FAKE_ANALYSIS
    assert annotation.openevidence_supplementary.citations[0].citation_key == "oe-99"
    assert "oe-99" not in annotation.citations


async def test_annotate_gene_wires_openevidence_context_when_enabled(monkeypatch):
    async def fake_get_gene_analysis(self, gene, tumor_type=None, client=None):
        return FAKE_ANALYSIS

    async def fake_check_oncokb_membership(gene, lookup=None):
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        return (RECORDS, 1)

    async def fake_select_papers(*args, **kwargs):
        return args[1]

    captured_kwargs = {}

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "GENE has retrieved cancer evidence (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    monkeypatch.setattr(OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation)

    annotation = await orchestrator._annotate_gene(
        gene="GENE",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="GENE", canonical_symbol="GENE", resolved=True),
        unresolvable=False,
        skip_literature_for_oncokb=False,
    )

    assert captured_kwargs["openevidence_context"] == FAKE_ANALYSIS
    assert annotation.citations == ["1"]
    assert annotation.openevidence_supplementary == FAKE_ANALYSIS
    assert "openevidence" in annotation.timings_ms


async def test_annotate_gene_treats_openevidence_failure_as_supplementary_only(monkeypatch):
    """A failing OpenEvidence lookup must not break the core annotation."""

    async def failing_get_gene_analysis(self, gene, tumor_type=None, client=None):
        raise RuntimeError("boom")

    async def fake_check_oncokb_membership(gene, lookup=None):
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        return (RECORDS, 1)

    async def fake_select_papers(*args, **kwargs):
        return args[1]

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        assert kwargs.get("openevidence_context") is None
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "GENE has retrieved cancer evidence (PMID 1).",
            "citations": ["1"],
        }

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    monkeypatch.setattr(OpenEvidenceClient, "get_gene_analysis", failing_get_gene_analysis)
    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation)

    annotation = await orchestrator._annotate_gene(
        gene="GENE",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="GENE", canonical_symbol="GENE", resolved=True),
        unresolvable=False,
        skip_literature_for_oncokb=False,
    )

    assert annotation.citations == ["1"]
    assert annotation.openevidence_supplementary is None
