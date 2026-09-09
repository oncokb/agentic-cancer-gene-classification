"""Tests for the permanent MySQL-backed pmid_evidence cache (RunStore) and
its integration into the synthesis prompt.

The RunStore round-trip tests run against a real MySQL instance (same
pattern as tests/test_run_store.py) and are skipped cleanly if none is
reachable. The prompt-integration tests are pure unit tests against
synthesis._build_user_prompt and need no external services.
"""

from __future__ import annotations

import asyncio

import pytest

from src.models.schema import LiteratureRecord, PMIDEvidenceRecord, ResolvedGene
from src.pipeline import orchestrator, synthesis
from src.pipeline.run_store import RunStore

_RAW_ABSTRACT = (
    "The ALEX trial was a randomized, open-label, phase 3 study comparing "
    "alectinib with crizotinib in patients with previously untreated, "
    "ALK-positive, advanced non-small-cell lung cancer. A total of 303 "
    "patients underwent randomization. Among patients with ALK-positive "
    "NSCLC, alectinib showed superior efficacy over crizotinib, with a "
    "considerably lower rate of central nervous system progression. Median "
    "progression-free survival was 34.8 months with alectinib compared "
    "with 10.9 months with crizotinib. Adverse events of grade 3 or higher "
    "were less frequent with alectinib than with crizotinib." * 3
)


@pytest.fixture
async def run_store():
    try:
        store = await RunStore.create()
    except Exception as exc:
        pytest.skip(f"MySQL not reachable: {exc}")
    async with store._pool.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("DELETE FROM pmid_evidence")
    yield store
    await store.close()


# ---------------------------------------------------------------------------
# RunStore.get_pmid_evidence_batch / save_pmid_evidence_batch
# ---------------------------------------------------------------------------


async def test_save_and_get_pmid_evidence_batch_round_trip(run_store):
    record = PMIDEvidenceRecord(
        pmid="30902613",
        doi="10.1093/annonc/mdz167",
        title="Alectinib versus crizotinib in untreated ALK-positive NSCLC",
        journal="Annals of Oncology",
        publication_year=2019,
        evidence_type="clinical",
        oncogenic_role="oncogene",
        distilled_takeaway=(
            "ALEX trial: alectinib demonstrated median PFS 34.8 mo vs "
            "crizotinib 10.9 mo in ALK+ NSCLC."
        ),
        supporting_quote="Median progression-free survival was 34.8 months with alectinib.",
    )

    await run_store.save_pmid_evidence_batch([record])
    result = await run_store.get_pmid_evidence_batch(["30902613", "99999999"])

    assert set(result.keys()) == {"30902613"}
    fetched = result["30902613"]
    assert fetched.title == record.title
    assert fetched.distilled_takeaway == record.distilled_takeaway
    assert fetched.evidence_type == "clinical"
    assert fetched.oncogenic_role == "oncogene"
    assert fetched.created_at is not None
    assert fetched.updated_at is not None


async def test_save_pmid_evidence_batch_upserts_existing_pmid(run_store):
    original = PMIDEvidenceRecord(
        pmid="11111111",
        title="Original title",
        journal="Journal A",
        distilled_takeaway="Original takeaway.",
    )
    await run_store.save_pmid_evidence_batch([original])

    updated = PMIDEvidenceRecord(
        pmid="11111111",
        title="Updated title",
        journal="Journal A",
        distilled_takeaway="Updated takeaway.",
    )
    await run_store.save_pmid_evidence_batch([updated])

    result = await run_store.get_pmid_evidence_batch(["11111111"])

    assert len(result) == 1
    assert result["11111111"].title == "Updated title"
    assert result["11111111"].distilled_takeaway == "Updated takeaway."


async def test_get_pmid_evidence_batch_returns_empty_dict_for_empty_input(run_store):
    assert await run_store.get_pmid_evidence_batch([]) == {}


async def test_get_pmid_evidence_batch_omits_pmids_with_no_cached_record(run_store):
    result = await run_store.get_pmid_evidence_batch(["00000000"])

    assert result == {}


async def test_save_pmid_evidence_batch_handles_empty_list(run_store):
    await run_store.save_pmid_evidence_batch([])  # must not raise

    assert await run_store.get_pmid_evidence_batch(["30902613"]) == {}


# ---------------------------------------------------------------------------
# synthesis._build_user_prompt: distilled takeaway vs. raw-abstract fallback
# ---------------------------------------------------------------------------


def _alex_trial_record() -> LiteratureRecord:
    return LiteratureRecord(
        pmid="30902613",
        title="Alectinib versus crizotinib in untreated ALK-positive NSCLC",
        abstract=_RAW_ABSTRACT,
        journal="Annals of Oncology",
        publication_types=["Clinical Trial"],
    )


def _distilled_record() -> PMIDEvidenceRecord:
    return PMIDEvidenceRecord(
        pmid="30902613",
        title="Alectinib versus crizotinib in untreated ALK-positive NSCLC",
        journal="Annals of Oncology",
        distilled_takeaway=(
            "ALEX trial: alectinib demonstrated median PFS 34.8 mo vs "
            "crizotinib 10.9 mo in ALK+ NSCLC."
        ),
    )


def test_build_user_prompt_falls_back_to_raw_abstract_when_not_cached():
    prompt = synthesis._build_user_prompt(
        gene="ALK",
        fusions=[],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=[_alex_trial_record()],
        retrieval_tier=1,
        pmid_evidence=None,
    )

    assert "Abstract:" in prompt
    assert "Summary:" not in prompt
    assert _RAW_ABSTRACT in prompt


def test_build_user_prompt_uses_distilled_takeaway_when_cached():
    prompt = synthesis._build_user_prompt(
        gene="ALK",
        fusions=[],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=[_alex_trial_record()],
        retrieval_tier=1,
        pmid_evidence={"30902613": _distilled_record()},
    )

    assert "Summary: ALEX trial: alectinib demonstrated median PFS 34.8 mo" in prompt
    assert "Abstract:" not in prompt
    assert _RAW_ABSTRACT not in prompt
    assert "PMID: 30902613" in prompt
    assert "[Annals of Oncology]" in prompt


def test_build_user_prompt_reduces_prompt_length_substantially_when_cached():
    uncached_prompt = synthesis._build_user_prompt(
        gene="ALK",
        fusions=[],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=[_alex_trial_record()],
        retrieval_tier=1,
        pmid_evidence=None,
    )
    cached_prompt = synthesis._build_user_prompt(
        gene="ALK",
        fusions=[],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=[_alex_trial_record()],
        retrieval_tier=1,
        pmid_evidence={"30902613": _distilled_record()},
    )

    # The 25-40 word distilled takeaway should cut this paper's contribution
    # to the prompt by at least 70%, matching the 70-80% target reduction.
    assert len(cached_prompt) <= len(uncached_prompt) * 0.3


def test_build_user_prompt_only_uses_cache_for_pmids_with_a_takeaway():
    """A pmid_evidence entry with an empty distilled_takeaway (shouldn't
    happen given the NOT NULL column, but defends against a stray empty
    string) must still fall back to the raw abstract rather than injecting
    an empty summary line."""
    empty_takeaway_record = PMIDEvidenceRecord(
        pmid="30902613",
        title="Alectinib versus crizotinib in untreated ALK-positive NSCLC",
        journal="Annals of Oncology",
        distilled_takeaway="",
    )
    prompt = synthesis._build_user_prompt(
        gene="ALK",
        fusions=[],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=[_alex_trial_record()],
        retrieval_tier=1,
        pmid_evidence={"30902613": empty_takeaway_record},
    )

    assert "Abstract:" in prompt
    assert "Summary:" not in prompt


def test_build_user_prompt_mixes_cached_and_uncached_papers():
    cached_record = _alex_trial_record()
    uncached_record = LiteratureRecord(
        pmid="99999999",
        title="An uncached paper",
        abstract="This abstract has not been distilled yet.",
        journal="Some Journal",
    )
    prompt = synthesis._build_user_prompt(
        gene="ALK",
        fusions=[],
        in_oncokb=True,
        cancer_type_prevalence=None,
        records=[cached_record, uncached_record],
        retrieval_tier=1,
        pmid_evidence={"30902613": _distilled_record()},
    )

    assert "Summary: ALEX trial" in prompt
    assert "Abstract: This abstract has not been distilled yet." in prompt


# ---------------------------------------------------------------------------
# orchestrator._annotate_gene: fire-and-forget pmid_evidence distillation
# ---------------------------------------------------------------------------


class _FakeGeneStoreForDistillation:
    def __init__(self, cached_evidence=None):
        self.cached_evidence = cached_evidence or {}
        self.saved_batches = []

    async def get_pmid_evidence_batch(self, pmids):
        return {pmid: self.cached_evidence[pmid] for pmid in pmids if pmid in self.cached_evidence}

    async def save_pmid_evidence_batch(self, records):
        self.saved_batches.append(records)


def _fake_pipeline_functions(monkeypatch):
    async def fake_check_oncokb_membership(gene, lookup=None):
        return False

    async def fake_retrieve_literature(*args, **kwargs):
        return (
            [
                LiteratureRecord(
                    pmid="30902613", title="Cached paper", abstract="abstract", journal="J"
                ),
                LiteratureRecord(
                    pmid="11111111", title="Uncached paper", abstract="abstract", journal="J"
                ),
            ],
            1,
        )

    async def fake_select_papers(*args, **kwargs):
        return args[1]

    async def fake_synthesize_gene_annotation(*args, **kwargs):
        return {
            "cancer_associated": True,
            "insufficient_evidence": False,
            "cancer_association_rationale": "Retrieved literature supports a cancer association.",
            "gene_summary": "ALK has retrieved cancer evidence.",
            "citations": ["30902613"],
        }

    monkeypatch.setattr(orchestrator, "check_oncokb_membership", fake_check_oncokb_membership)
    monkeypatch.setattr(orchestrator, "retrieve_literature", fake_retrieve_literature)
    monkeypatch.setattr(orchestrator, "select_papers_for_synthesis", fake_select_papers)
    monkeypatch.setattr(
        orchestrator, "synthesize_gene_annotation", fake_synthesize_gene_annotation
    )


async def test_annotate_gene_fires_distillation_only_for_uncached_pmids(monkeypatch):
    _fake_pipeline_functions(monkeypatch)
    calls = []

    async def fake_distill_and_save(run_store, records):
        calls.append([r.pmid for r in records])

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", fake_distill_and_save)
    store = _FakeGeneStoreForDistillation(
        cached_evidence={"30902613": PMIDEvidenceRecord(
            pmid="30902613", title="t", journal="j", distilled_takeaway="already cached"
        )}
    )

    await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
        run_store=store,
    )
    await asyncio.sleep(0)  # let the fire-and-forget task get scheduled

    assert calls == [["11111111"]]


async def test_annotate_gene_skips_distillation_when_all_pmids_cached(monkeypatch):
    _fake_pipeline_functions(monkeypatch)
    calls = []

    async def fake_distill_and_save(run_store, records):
        calls.append(records)

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", fake_distill_and_save)
    store = _FakeGeneStoreForDistillation(
        cached_evidence={
            "30902613": PMIDEvidenceRecord(pmid="30902613", title="t", journal="j", distilled_takeaway="x"),
            "11111111": PMIDEvidenceRecord(pmid="11111111", title="t", journal="j", distilled_takeaway="y"),
        }
    )

    await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
        run_store=store,
    )
    await asyncio.sleep(0)

    assert calls == []


async def test_annotate_gene_skips_distillation_in_local_mode(monkeypatch):
    _fake_pipeline_functions(monkeypatch)

    async def fail_if_called(run_store, records):
        raise AssertionError("distillation should be skipped in local_mode")

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", fail_if_called)
    store = _FakeGeneStoreForDistillation()

    await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
        run_store=store,
        local_mode=True,
    )
    await asyncio.sleep(0)


async def test_annotate_gene_skips_distillation_when_run_store_none(monkeypatch):
    _fake_pipeline_functions(monkeypatch)

    async def fail_if_called(run_store, records):
        raise AssertionError("distillation should be skipped without a run_store")

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", fail_if_called)

    await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
        run_store=None,
    )
    await asyncio.sleep(0)


async def test_annotate_gene_skips_distillation_when_disabled_via_settings(monkeypatch):
    _fake_pipeline_functions(monkeypatch)
    monkeypatch.setattr(orchestrator.settings, "pmid_distillation_enabled", False)

    async def fail_if_called(run_store, records):
        raise AssertionError("distillation should be skipped when disabled")

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", fail_if_called)
    store = _FakeGeneStoreForDistillation()

    await orchestrator._annotate_gene(
        gene="ALK",
        fusions=[],
        resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
        unresolvable=False,
        run_store=store,
    )
    await asyncio.sleep(0)


async def test_annotate_gene_returns_without_waiting_for_distillation_to_finish(monkeypatch):
    """The distillation task must be fire-and-forget: _annotate_gene returns
    even though the fake distillation below never completes on its own."""
    _fake_pipeline_functions(monkeypatch)
    never_finishes = asyncio.Event()

    async def hanging_distill(run_store, records):
        await never_finishes.wait()

    monkeypatch.setattr(orchestrator, "distill_and_save_pmid_evidence", hanging_distill)
    store = _FakeGeneStoreForDistillation()

    annotation = await asyncio.wait_for(
        orchestrator._annotate_gene(
            gene="ALK",
            fusions=[],
            resolved_gene=ResolvedGene(input_symbol="ALK", canonical_symbol="ALK", resolved=True),
            unresolvable=False,
            run_store=store,
        ),
        timeout=2,
    )

    assert annotation.gene == "ALK"
    never_finishes.set()  # let the background task clean up rather than leak
