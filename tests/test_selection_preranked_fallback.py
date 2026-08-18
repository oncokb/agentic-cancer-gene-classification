from __future__ import annotations

from datetime import datetime, timezone

from src.models.schema import LiteratureRecord
from src.pipeline import selection
from src.pipeline.literature import rank_literature_for_synthesis
from src.pipeline.selection import select_papers_for_synthesis

CURRENT_YEAR = datetime.now(timezone.utc).year


def _record(pmid: str, **overrides) -> LiteratureRecord:
    defaults = dict(pmid=pmid, title="Title", abstract="Abstract.", publication_types=[])
    defaults.update(overrides)
    return LiteratureRecord(**defaults)


async def test_haiku_failure_fallback_uses_composite_rank_not_input_order(monkeypatch):
    """Before this change, the fallback on Haiku failure was records[:max_papers]
    in whatever order the retrieval pool happened to arrive in (recency-ish, via
    _rank_records' PMID-descending tiebreak). Now the pool handed to selection is
    pre-sorted by the composite heuristic, so the SAME fallback code produces a
    materially different, stronger-evidence-first result."""

    # Deliberately unordered by recency: the weakest paper has the highest PMID
    # (most "recent" under the old PMID-as-recency-proxy heuristic), while the
    # strongest paper (MeSH-tagged, fusion-partner-mentioning, recent trial) has
    # a lower PMID. A pure recency/PMID fallback would rank "weak" above "strong".
    strong = _record(
        "100",
        title="EML4-ALK fusion trial",
        abstract="ALK fuses to EML4; phase I trial results.",
        matched_query_tiers=["mesh_gene_name", "fusion_partner"],
        publication_year=CURRENT_YEAR,
        publication_types=["Clinical Trial, Phase I"],
    )
    weak = _record(
        "999",
        title="Unrelated letter",
        abstract="Brief correspondence, no direct evidence.",
        matched_query_tiers=["free_text"],
        publication_year=CURRENT_YEAR - 20,
        publication_types=["Letter"],
    )

    ranked_records, _scores = rank_literature_for_synthesis(
        [weak, strong], gene="ALK", fusions=["ALK::EML4"], max_papers=1,
    )

    async def fake_complete_with_tool(*args, **kwargs):
        raise RuntimeError("simulated Haiku outage")

    monkeypatch.setattr(selection, "complete_with_tool", fake_complete_with_tool)
    # Force the LLM path to be attempted (and fail) rather than skipped.
    monkeypatch.setattr(selection.settings, "selection_llm_threshold", 10)

    selected = await select_papers_for_synthesis("ALK", ranked_records, max_papers=1)

    assert [r.pmid for r in selected] == ["100"]  # strong paper, not the higher-PMID weak one
