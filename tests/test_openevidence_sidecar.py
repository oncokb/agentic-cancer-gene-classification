"""Tests for the OpenEvidence sidecar: deterministic distillation
(src.pipeline.openevidence.distill_openevidence), the standalone
GET /v1/genes/{gene}/openevidence endpoint, and proof that core gene
annotation (_annotate_gene) never waits on OpenEvidence.

OpenEvidence's 130-185s call latency and raw-prose synthesis-prompt
dumping were removed from the synchronous annotation path entirely (see
orchestrator.py and synthesis.py) — it now only runs behind the sidecar
endpoint below, distilled deterministically (no LLM call) before being
returned.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src import main
from src.models.schema import (
    LiteratureRecord,
    OpenEvidenceAnalysis,
    OpenEvidenceCitation,
    ResolvedGene,
)
from src.pipeline import orchestrator
from src.pipeline.openevidence import distill_openevidence

# Real, live-captured citation shapes (see tests/test_openevidence.py) reused
# here to keep the distillation fixtures realistic.
_NCCN_CITATION = OpenEvidenceCitation(
    citation_key="1",
    title="Melanoma: Cutaneous",
    journal="",
    date="2026-09-02",
    url="https://www.nccn.org/professionals/physician_gls/pdf/cutaneous_melanoma.pdf#page=77",
)
_ASCO_CITATION = OpenEvidenceCitation(
    citation_key="2",
    title="ASCO Guideline on NSCLC",
    journal="",
    date="2024-01-01",
    url="https://www.asco.org/guidelines/nsclc",
)
_ESMO_CITATION = OpenEvidenceCitation(
    citation_key="3",
    title="ESMO Clinical Practice Guideline",
    journal="",
    date="2023-05-01",
    url="https://www.esmo.org/guidelines/nsclc",
)
_JOURNAL_CITATION = OpenEvidenceCitation(
    citation_key="4",
    title="A pharmacokinetic study of alectinib",
    journal="Annals of Oncology",
    date="2019-03-01",
    doi="10.1000/example",
    url="https://pubmed.ncbi.nlm.nih.gov/30902613",
)

_ALK_ANALYSIS = OpenEvidenceAnalysis(
    question="Is ALK an oncogene in NSCLC?",
    text=(
        "ALK is classified as an oncogenic driver in ALK-rearranged NSCLC. "
        "[[1]] In the ALEX trial, alectinib demonstrated median PFS of 34.8 "
        "months versus 10.9 months for crizotinib. [[4]] NCCN guidelines "
        "recommend alectinib as first-line therapy. [[2]][[3]]"
    ),
    citations=[_NCCN_CITATION, _ASCO_CITATION, _ESMO_CITATION, _JOURNAL_CITATION],
)


# ---------------------------------------------------------------------------
# distill_openevidence
# ---------------------------------------------------------------------------


def test_distill_openevidence_extracts_guideline_from_nccn_url():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    nccn = next(g for g in distilled.guidelines if "nccn.org" in g.url)
    assert nccn.title == "Melanoma: Cutaneous"
    assert nccn.page_anchor == "page=77"


def test_distill_openevidence_extracts_asco_and_esmo_guidelines():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    urls = {g.url for g in distilled.guidelines}
    assert _ASCO_CITATION.url in urls
    assert _ESMO_CITATION.url in urls


def test_distill_openevidence_ignores_non_guideline_journal_citation():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    urls = {g.url for g in distilled.guidelines}
    assert _JOURNAL_CITATION.url not in urls
    assert len(distilled.guidelines) == 3


def test_distill_openevidence_extracts_trial_mention_by_known_acronym():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    alex_mentions = [m for m in distilled.trial_mentions if m.trial == "ALEX"]
    assert len(alex_mentions) == 1
    assert "34.8" in alex_mentions[0].sentence
    assert "PFS" in alex_mentions[0].sentence


def test_distill_openevidence_extracts_trial_mention_by_outcome_statistic_without_acronym():
    analysis = OpenEvidenceAnalysis(
        question="q",
        text="Median OS was not reached in the treatment arm. HR was 0.43.",
    )
    distilled = distill_openevidence(analysis)

    assert len(distilled.trial_mentions) == 2
    assert all(mention.trial is None for mention in distilled.trial_mentions)


def test_distill_openevidence_extracts_consensus_role_as_opening_sentence():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    assert distilled.consensus_role == (
        "ALK is classified as an oncogenic driver in ALK-rearranged NSCLC."
    )
    # Inline citation markers are stripped, not leaked into the UI text.
    assert "[[1]]" not in distilled.consensus_role


def test_distill_openevidence_citation_count_matches_citations():
    distilled = distill_openevidence(_ALK_ANALYSIS)

    assert distilled.citation_count == 4


def test_distill_openevidence_handles_empty_analysis():
    distilled = distill_openevidence(OpenEvidenceAnalysis(question="q", text=""))

    assert distilled.consensus_role is None
    assert distilled.guidelines == []
    assert distilled.trial_mentions == []
    assert distilled.citation_count == 0


# ---------------------------------------------------------------------------
# GET /v1/genes/{gene}/openevidence
# ---------------------------------------------------------------------------


def test_openevidence_sidecar_endpoint_returns_unavailable_when_disabled(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", False)

    async def fail_if_called(self, *args, **kwargs):
        raise AssertionError("OpenEvidence should never be called when disabled")

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fail_if_called)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert payload["distilled"] is None


def test_openevidence_sidecar_endpoint_returns_distilled_result_when_enabled(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_get_gene_analysis(self, gene, tumor_type=None, fusion=None, client=None):
        assert gene == "ALK"
        return _ALK_ANALYSIS

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_get_gene_analysis)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence", params={"tumor_type": "NSCLC"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["distilled"]["citation_count"] == 4
    assert len(payload["distilled"]["guidelines"]) == 3
    assert any(m["trial"] == "ALEX" for m in payload["distilled"]["trial_mentions"])


def test_openevidence_sidecar_endpoint_returns_unavailable_on_lookup_failure(monkeypatch):
    monkeypatch.setattr(main.settings, "openevidence_enabled", True)

    async def fake_failing_lookup(self, gene, tumor_type=None, fusion=None, client=None):
        raise RuntimeError("upstream timeout")

    monkeypatch.setattr(main.OpenEvidenceClient, "get_gene_analysis", fake_failing_lookup)
    client = TestClient(main.app)

    response = client.get("/v1/genes/ALK/openevidence")

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert "upstream timeout" in payload["error"]


# ---------------------------------------------------------------------------
# _annotate_gene never waits on OpenEvidence
# ---------------------------------------------------------------------------


async def test_annotate_gene_never_touches_openevidence_even_when_enabled(monkeypatch):
    """OPENEVIDENCE_ENABLED=true must have zero effect on _annotate_gene:
    no "openevidence" timing bucket, and orchestrator.py no longer imports
    anything from src.pipeline.openevidence to call in the first place."""
    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    assert not hasattr(orchestrator, "OpenEvidenceClient")
    assert not hasattr(orchestrator, "_maybe_fetch_openevidence_context")

    async def fake_check_oncokb_membership(gene, lookup=None):
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        return (
            [
                LiteratureRecord(
                    pmid="123",
                    title="ALK cancer",
                    abstract="ALK was studied in cancer.",
                    publication_types=["Journal Article"],
                )
            ],
            1,
        )

    async def fake_select_papers(*args, **kwargs):
        return args[1]

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        assert "openevidence_context" not in kwargs
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "ALK has retrieved cancer evidence (PMID 123).",
            "citations": ["123"],
        }

    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(
        orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation
    )

    annotation = await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
    )

    assert "openevidence" not in annotation.timings_ms
    assert annotation.timings_ms["total"] < 15000
