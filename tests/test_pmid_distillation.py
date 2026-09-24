"""Tests for the background pmid_evidence distillation step
(src.pipeline.pmid_distillation) — the follow-up that actually populates the
pmid_evidence cache added in tests/test_pmid_evidence_cache.py, closing the
gap where the prompt-injection path existed but nothing wrote takeaways.
"""

from __future__ import annotations

from src.models.schema import LiteratureRecord
from src.pipeline import pmid_distillation


def _alk_record() -> LiteratureRecord:
    return LiteratureRecord(
        pmid="30902613",
        title="Alectinib versus crizotinib in untreated ALK-positive NSCLC",
        abstract="Alectinib demonstrated superior median PFS versus crizotinib.",
        journal="Annals of Oncology",
        publication_year=2019,
    )


# ---------------------------------------------------------------------------
# distill_pmid_abstracts
# ---------------------------------------------------------------------------


async def test_distill_pmid_abstracts_maps_llm_output_to_records(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        assert kwargs["model"] == pmid_distillation.settings.pmid_distillation_model
        assert "30902613" in kwargs["user"]
        return {
            "records": [
                {
                    "pmid": "30902613",
                    "distilled_takeaway": (
                        "ALEX trial: alectinib demonstrated median PFS 34.8 mo vs "
                        "crizotinib 10.9 mo in ALK+ NSCLC."
                    ),
                    "evidence_type": "clinical",
                    "oncogenic_role": "oncogene",
                    "supporting_quote": "Alectinib demonstrated superior median PFS.",
                }
            ]
        }

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)

    result = await pmid_distillation.distill_pmid_abstracts([_alk_record()])

    assert len(result) == 1
    record = result[0]
    assert record.pmid == "30902613"
    assert record.title == "Alectinib versus crizotinib in untreated ALK-positive NSCLC"
    assert record.journal == "Annals of Oncology"
    assert record.publication_year == 2019
    assert record.evidence_type == "clinical"
    assert record.oncogenic_role == "oncogene"
    assert "ALEX trial" in record.distilled_takeaway


async def test_distill_pmid_abstracts_returns_empty_for_empty_input():
    assert await pmid_distillation.distill_pmid_abstracts([]) == []


async def test_distill_pmid_abstracts_swallows_llm_failure(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)

    result = await pmid_distillation.distill_pmid_abstracts([_alk_record()])

    assert result == []


async def test_distill_pmid_abstracts_skips_entries_missing_takeaway_or_unknown_pmid(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {
            "records": [
                {"pmid": "30902613", "distilled_takeaway": ""},  # empty takeaway
                {"pmid": "99999999", "distilled_takeaway": "Not one of the requested PMIDs."},
                "not-a-dict",
            ]
        }

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)

    result = await pmid_distillation.distill_pmid_abstracts([_alk_record()])

    assert result == []


async def test_distill_pmid_abstracts_defaults_missing_type_and_role_fields(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"records": [{"pmid": "30902613", "distilled_takeaway": "A takeaway."}]}

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)

    result = await pmid_distillation.distill_pmid_abstracts([_alk_record()])

    assert result[0].evidence_type == "other"
    assert result[0].oncogenic_role == "unknown"
    assert result[0].supporting_quote is None


# ---------------------------------------------------------------------------
# distill_and_save_pmid_evidence
# ---------------------------------------------------------------------------


class _FakeRunStore:
    def __init__(self):
        self.saved_batches = []

    async def save_pmid_evidence_batch(self, records):
        self.saved_batches.append(records)


async def test_distill_and_save_pmid_evidence_persists_distilled_records(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"records": [{"pmid": "30902613", "distilled_takeaway": "A takeaway."}]}

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)
    run_store = _FakeRunStore()

    await pmid_distillation.distill_and_save_pmid_evidence(run_store, [_alk_record()])

    assert len(run_store.saved_batches) == 1
    assert run_store.saved_batches[0][0].pmid == "30902613"


async def test_distill_and_save_pmid_evidence_noop_when_run_store_none():
    await pmid_distillation.distill_and_save_pmid_evidence(None, [_alk_record()])  # must not raise


async def test_distill_and_save_pmid_evidence_noop_when_no_records():
    run_store = _FakeRunStore()

    await pmid_distillation.distill_and_save_pmid_evidence(run_store, [])

    assert run_store.saved_batches == []


async def test_distill_and_save_pmid_evidence_does_not_save_when_nothing_distilled(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"records": []}

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)
    run_store = _FakeRunStore()

    await pmid_distillation.distill_and_save_pmid_evidence(run_store, [_alk_record()])

    assert run_store.saved_batches == []


async def test_distill_and_save_pmid_evidence_swallows_save_failure(monkeypatch):
    async def fake_complete_with_tool(**kwargs):
        return {"records": [{"pmid": "30902613", "distilled_takeaway": "A takeaway."}]}

    monkeypatch.setattr(pmid_distillation, "complete_with_tool", fake_complete_with_tool)

    class _FailingRunStore:
        async def save_pmid_evidence_batch(self, records):
            raise RuntimeError("MySQL unreachable")

    await pmid_distillation.distill_and_save_pmid_evidence(
        _FailingRunStore(), [_alk_record()]
    )  # must not raise
