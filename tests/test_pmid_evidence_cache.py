"""Tests for the permanent MySQL-backed pmid_evidence cache (RunStore) and
its integration into the synthesis prompt.

The RunStore round-trip tests run against a real MySQL instance (same
pattern as tests/test_run_store.py) and are skipped cleanly if none is
reachable. The prompt-integration tests are pure unit tests against
synthesis._build_user_prompt and need no external services.
"""

from __future__ import annotations

import pytest

from src.models.schema import LiteratureRecord, PMIDEvidenceRecord
from src.pipeline import synthesis
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
