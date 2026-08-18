from __future__ import annotations

from datetime import datetime, timezone

from src.models.schema import LiteratureRecord
from src.pipeline import literature
from src.pipeline.literature import (
    _fusion_cooccurrence_score,
    _pubtype_score,
    _query_tier_score,
    _recency_score,
    rank_literature_for_synthesis,
    score_literature_records,
)

CURRENT_YEAR = datetime.now(timezone.utc).year


def _record(pmid: str, **overrides) -> LiteratureRecord:
    defaults = dict(
        pmid=pmid,
        title="Title",
        abstract="Abstract text.",
        journal="",
        publication_types=[],
    )
    defaults.update(overrides)
    return LiteratureRecord(**defaults)


# ---------------------------------------------------------------------------
# Query-tier signal
# ---------------------------------------------------------------------------

def test_query_tier_score_prefers_mesh_over_free_text():
    mesh = _record("1", matched_query_tiers=["mesh_gene_name"])
    free_text = _record("2", matched_query_tiers=["free_text"])

    assert _query_tier_score(mesh) > _query_tier_score(free_text)
    assert _query_tier_score(mesh) == 1.0
    assert _query_tier_score(free_text) == 0.5


def test_query_tier_score_takes_max_across_multiple_matches():
    record = _record("1", matched_query_tiers=["free_text", "mesh_gene_name"])
    assert _query_tier_score(record) == 1.0


def test_query_tier_score_defaults_neutral_when_untagged():
    record = _record("1", matched_query_tiers=[])
    assert _query_tier_score(record) == 0.5


def test_tier2_agentic_scores_between_mesh_and_free_text_weight():
    tier2 = _record("1", matched_query_tiers=["tier2_agentic"])
    assert _query_tier_score(tier2) == 0.5


# ---------------------------------------------------------------------------
# Fusion co-occurrence signal
# ---------------------------------------------------------------------------

def test_fusion_cooccurrence_none_when_not_a_fusion_annotation():
    record = _record("1", title="ALK in cancer", abstract="ALK is a kinase.")
    assert _fusion_cooccurrence_score(record, []) is None


def test_fusion_cooccurrence_full_credit_when_partner_mentioned():
    record = _record("1", title="EML4-ALK fusion", abstract="ALK fuses to EML4 in NSCLC.")
    assert _fusion_cooccurrence_score(record, ["EML4"]) == 1.0


def test_fusion_cooccurrence_zero_when_partner_not_mentioned():
    record = _record("1", title="ALK amplification", abstract="ALK copy number gain in neuroblastoma.")
    assert _fusion_cooccurrence_score(record, ["EML4"]) == 0.0


def test_fusion_cooccurrence_partial_credit_for_multi_partner_fusion():
    record = _record("1", title="NUP98 fusions", abstract="NUP98 fuses to NSD1 in AML.")
    # Only one of two partners mentioned in text.
    assert _fusion_cooccurrence_score(record, ["NSD1", "KDM5A"]) == 0.5


def test_fusion_cooccurrence_uses_word_boundaries():
    # "ALK" should not match inside "ALKBH5".
    record = _record("1", title="ALKBH5 in cancer", abstract="ALKBH5 regulates m6A methylation.")
    assert _fusion_cooccurrence_score(record, ["ALK"]) == 0.0


# ---------------------------------------------------------------------------
# Recency signal
# ---------------------------------------------------------------------------

def test_recency_score_current_year_is_near_one(monkeypatch):
    monkeypatch.setattr(literature.settings, "citation_score_recency_half_life_years", 8.0)
    record = _record("1", publication_year=CURRENT_YEAR)
    assert _recency_score(record) == 1.0


def test_recency_score_decays_with_age(monkeypatch):
    monkeypatch.setattr(literature.settings, "citation_score_recency_half_life_years", 8.0)
    recent = _record("1", publication_year=CURRENT_YEAR)
    old = _record("2", publication_year=CURRENT_YEAR - 16)  # two half-lives

    assert _recency_score(old) < _recency_score(recent)
    assert abs(_recency_score(old) - 0.25) < 0.01  # 0.5 ** 2


def test_recency_score_neutral_when_year_missing():
    record = _record("1", publication_year=None)
    assert _recency_score(record) == 0.5


def test_recency_half_life_configurable_relaxes_decay(monkeypatch):
    record = _record("1", publication_year=CURRENT_YEAR - 16)

    monkeypatch.setattr(literature.settings, "citation_score_recency_half_life_years", 8.0)
    tight = _recency_score(record)

    monkeypatch.setattr(literature.settings, "citation_score_recency_half_life_years", 32.0)
    relaxed = _recency_score(record)

    assert relaxed > tight


# ---------------------------------------------------------------------------
# Publication-type signal
# ---------------------------------------------------------------------------

def test_pubtype_citation_profile_prefers_trials_over_reviews():
    trial = _record("1", publication_types=["Randomized Controlled Trial"])
    review = _record("2", publication_types=["Review"])

    assert _pubtype_score(trial, "citation") > _pubtype_score(review, "citation")


def test_pubtype_context_profile_prefers_reviews_over_trials():
    trial = _record("1", publication_types=["Clinical Trial, Phase I"])
    review = _record("2", publication_types=["Review"])

    assert _pubtype_score(review, "context") > _pubtype_score(trial, "context")


def test_pubtype_score_falls_back_to_original_research_default():
    record = _record("1", publication_types=["Journal Article"])
    assert _pubtype_score(record, "citation") == literature.settings.citation_score_pubtype_original_research_weight
    assert _pubtype_score(record, "context") == literature.settings.context_score_pubtype_original_research_weight


def test_pubtype_score_takes_max_across_multiple_types():
    # Trial outranks case report under the citation profile — max wins.
    record = _record("1", publication_types=["Case Reports", "Randomized Controlled Trial"])
    assert _pubtype_score(record, "citation") == literature.settings.citation_score_pubtype_trial_weight


# ---------------------------------------------------------------------------
# Composite scoring
# ---------------------------------------------------------------------------

def test_composite_renormalizes_when_fusion_signal_not_applicable():
    """A standalone-gene record and a fusion record with an identical fusion
    co-occurrence miss should NOT score the same — the standalone gene's score
    should reflect only the 3 applicable signals, not be penalized for a
    signal that doesn't apply to it."""
    record = _record(
        "1",
        title="ALK in cancer",
        abstract="ALK is an oncogene.",
        matched_query_tiers=["mesh_gene_name"],
        publication_year=CURRENT_YEAR,
        publication_types=["Randomized Controlled Trial"],
    )

    scores_standalone = score_literature_records([record], gene="ALK", fusions=[])
    scores_fusion = score_literature_records([record], gene="ALK", fusions=["ALK::EML4"])

    assert scores_standalone[0].fusion_cooccurrence_score is None
    assert scores_fusion[0].fusion_cooccurrence_score == 0.0  # EML4 not mentioned in text
    # Standalone should score higher than an unrewarded fusion miss on the same paper.
    assert scores_standalone[0].citation_composite_score > scores_fusion[0].citation_composite_score


def test_composite_scores_are_bounded_and_ordered_sensibly():
    strong = _record(
        "1",
        title="EML4-ALK fusion in NSCLC",
        abstract="ALK fuses to EML4 driving oncogenesis.",
        matched_query_tiers=["mesh_gene_name", "fusion_partner"],
        publication_year=CURRENT_YEAR,
        publication_types=["Randomized Controlled Trial"],
    )
    weak = _record(
        "2",
        title="Unrelated topic",
        abstract="No relevant mention here.",
        matched_query_tiers=["free_text"],
        publication_year=CURRENT_YEAR - 40,
        publication_types=["Letter"],
    )

    scores = score_literature_records([strong, weak], gene="ALK", fusions=["ALK::EML4"])
    by_pmid = {s.pmid: s for s in scores}

    assert 0.0 <= by_pmid["1"].citation_composite_score <= 1.0
    assert 0.0 <= by_pmid["2"].citation_composite_score <= 1.0
    assert by_pmid["1"].citation_composite_score > by_pmid["2"].citation_composite_score


def test_context_composite_can_outrank_citation_composite_for_reviews():
    review = _record(
        "1",
        title="ALK fusions: a review",
        abstract="Review of ALK fusion biology.",
        matched_query_tiers=["mesh_gene_name"],
        publication_year=CURRENT_YEAR,
        publication_types=["Review"],
    )
    scores = score_literature_records([review], gene="ALK", fusions=[])[0]
    assert scores.publication_type_context_score > scores.publication_type_citation_score
    assert scores.context_composite_score > scores.citation_composite_score


# ---------------------------------------------------------------------------
# Candidate pool merge (citation-ranked core + review supplement)
# ---------------------------------------------------------------------------

def test_rank_literature_for_synthesis_builds_citation_core_plus_review_supplement(monkeypatch):
    monkeypatch.setattr(literature.settings, "citation_score_review_supplement_count", 1)

    trial = _record(
        "1",
        title="ALK trial",
        abstract="Phase I trial of ALK inhibitor.",
        matched_query_tiers=["mesh_gene_name"],
        publication_year=CURRENT_YEAR,
        publication_types=["Clinical Trial, Phase I"],
    )
    another_trial = _record(
        "2",
        title="ALK trial 2",
        abstract="Another ALK trial.",
        matched_query_tiers=["mesh_gene_name"],
        publication_year=CURRENT_YEAR,
        publication_types=["Clinical Trial, Phase I"],
    )
    review = _record(
        "3",
        title="ALK review",
        abstract="Comprehensive review of ALK.",
        matched_query_tiers=["free_text"],
        publication_year=CURRENT_YEAR - 10,
        publication_types=["Review"],
    )

    candidate_pool, scores = rank_literature_for_synthesis(
        [trial, another_trial, review], gene="ALK", fusions=[], max_papers=2,
    )

    pmids = [r.pmid for r in candidate_pool]
    assert pmids == ["1", "2", "3"]  # citation core (both trials) then review supplement

    by_pmid = {s.pmid: s for s in scores}
    assert by_pmid["1"].pool == "citation"
    assert by_pmid["2"].pool == "citation"
    assert by_pmid["3"].pool == "context_supplement"


def test_rank_literature_for_synthesis_skips_supplement_if_already_in_core(monkeypatch):
    monkeypatch.setattr(literature.settings, "citation_score_review_supplement_count", 2)

    only_paper = _record(
        "1",
        title="ALK",
        abstract="ALK review and trial in one.",
        matched_query_tiers=["mesh_gene_name"],
        publication_year=CURRENT_YEAR,
        publication_types=["Review"],
    )

    candidate_pool, scores = rank_literature_for_synthesis(
        [only_paper], gene="ALK", fusions=[], max_papers=5,
    )

    assert [r.pmid for r in candidate_pool] == ["1"]
    assert scores[0].pool == "citation"  # already in the core; not duplicated as a supplement


def test_rank_literature_for_synthesis_scores_cover_full_pool_not_just_candidates(monkeypatch):
    monkeypatch.setattr(literature.settings, "citation_score_review_supplement_count", 0)

    kept = _record("1", matched_query_tiers=["mesh_gene_name"], publication_year=CURRENT_YEAR)
    dropped = _record("2", matched_query_tiers=["free_text"], publication_year=CURRENT_YEAR - 30)

    candidate_pool, scores = rank_literature_for_synthesis(
        [kept, dropped], gene="ALK", fusions=[], max_papers=1,
    )

    assert [r.pmid for r in candidate_pool] == ["1"]
    # Full audit trail still covers the dropped paper, just without a pool tag.
    assert {s.pmid for s in scores} == {"1", "2"}
    assert {s.pmid: s.pool for s in scores}["2"] is None
