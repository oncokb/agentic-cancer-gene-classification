from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from src import main
from src.api import curation_reevaluate_routes as routes
from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import curation_reevaluate as curator
from src.pipeline import synthesis
from src.pipeline.citation_precision import filter_and_rank_citations


def paper(pmid, **kwargs):
    return LiteratureRecord(
        pmid=pmid, title="RP1 cancer study", abstract="RP1 was studied. Further findings", **kwargs
    )


@pytest.fixture
def mocked(monkeypatch):
    records = [paper(str(i)) for i in range(1, 11)]
    fetch = AsyncMock(return_value=records)
    monkeypatch.setattr(curator.literature, "_efetch", fetch)
    monkeypatch.setattr(curator.literature, "_esearch", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        curator,
        "resolve_gene",
        AsyncMock(
            return_value=ResolvedGene(
                input_symbol="RP1",
                canonical_symbol="RP1",
                resolved=True,
                name="RP1 axonemal microtubule associated",
                locus_type="gene with protein product",
            )
        ),
    )
    monkeypatch.setattr(curator, "check_oncokb_membership", AsyncMock(return_value=False))
    monkeypatch.setattr(curator, "get_msk_genie_prevalence", lambda _: "rare")
    llm = AsyncMock(
        return_value={
            "cancer_associated": True,
            "cancer_association_rationale": "Suggested rationale.",
            "citations": ["999", "1"],
            "supporting_quotes": [
                {"pmid": "1", "quote": "Invented quote."},
                {"pmid": "2", "quote": "Further findings"},
            ],
        }
    )
    suggestions = AsyncMock(
        return_value={
            "records": [
                {
                    "pmid": "1",
                    "selected_reason": "Suggested reason",
                    "quote": "Invented again.",
                    "evidence_type": "clinical",
                }
            ],
            "precedent_summary": "Suggested precedent summary.",
        }
    )
    monkeypatch.setattr(synthesis, "complete_with_tool", llm)
    monkeypatch.setattr(curator, "complete_with_tool", suggestions)
    monkeypatch.setattr(synthesis.settings, "synthesis_model_escalation", False)
    monkeypatch.setattr(synthesis.settings, "max_citations_per_annotation", 2)
    monkeypatch.setattr(synthesis.settings, "max_papers_for_synthesis", 2)
    return records, fetch, llm, suggestions


async def test_invalid_pmids_reports_each_reason_before_llm(mocked):
    records, fetch, llm, suggestions = mocked
    fetch.return_value = [
        paper("1", publication_types=["Retracted Publication"]),
        LiteratureRecord(pmid="2", title="No abstract", abstract=""),
    ]
    with pytest.raises(curator.InvalidPMIDs) as error:
        await curator.reevaluate(
            curator.ReevaluateRequest(
                subject_type="gene",
                subject_key="RP1",
                evidence_pmids=["1", "2"],
                precedent_pmids=["3"],
            )
        )
    assert error.value.reasons == {"unknown": ["3"], "retracted": ["1"], "no_abstract": ["2"]}
    assert fetch.call_args.kwargs == {"filter_retracted": False}
    llm.assert_not_awaited()
    suggestions.assert_not_awaited()


@pytest.mark.parametrize("subject_type,subject_key", [("gene", "RP1"), ("fusion", "EML4::ALK")])
async def test_pinned_cards_and_quotes_in_order(mocked, subject_type, subject_key):
    records, fetch, llm, suggestions = mocked
    pmids = [str(i) for i in range(10, 0, -1)]
    result = await curator.reevaluate(
        curator.ReevaluateRequest(
            subject_type=subject_type,
            subject_key=subject_key,
            tumor_type="LUAD",
            evidence_pmids=pmids,
        )
    )
    assert result["citations"] == pmids
    assert [c["pmid"] for c in result["evidence_cards"]] == pmids
    assert len(result["evidence_cards"]) == 10
    assert result["evidence_cards"][-1]["selected_reason"] == "Suggested reason"
    by_pmid = {r.pmid: r for r in records}
    for card, quote in zip(result["evidence_cards"], result["supporting_quotes"]):
        assert card["quote"] in by_pmid[card["pmid"]].abstract
        assert quote["quote"] == card["quote"]
    assert result["evidence_cards"][-2]["quote"] == "Further findings"
    assert all(f"PMID: {p}" in llm.call_args.kwargs["user"] for p in pmids)
    assert result["generated_at"]
    assert result["model_info"]["synthesis_model"]
    if subject_type == "fusion":
        assert result["fusion"] == subject_key
        assert result["pmids"] == pmids
        assert result["interpretation"] == "Suggested rationale."
        assert all(c["fusion"] == subject_key for c in result["evidence_cards"])


async def test_precedents_bypass_threshold_and_use_existing_extraction(mocked):
    records, fetch, llm, suggestions = mocked
    records[1].abstract = "A kinase domain was studied with imatinib."
    result = await curator.reevaluate(
        curator.ReevaluateRequest(
            subject_type="fusion",
            subject_key="EML4::ALK",
            evidence_pmids=["1"],
            precedent_pmids=["2", "3"],
        )
    )
    block = result["clinical_actionability"]
    assert block["confidence_level"] == "high"
    assert block["summary"] == "Suggested precedent summary."
    assert block["pmids"] == ["2", "3"]
    assert [e["pmid"] for e in block["evidence"]] == ["2", "3"]
    assert block["evidence"][0]["therapies"] == ["imatinib"]
    assert block["evidence"][0]["domains"]
    assert all(e["quote"] in e["abstract"] for e in block["evidence"])
    assert result["citations"] == ["1"]


async def test_cache_is_read_only_and_tumor_scoped(mocked):
    store = AsyncMock()
    store.get_gene_annotation.return_value = {
        "annotation": {
            "fusions": ["RP1::ALK"],
            "in_oncokb": True,
            "cancer_type_prevalence": "cached prevalence",
        }
    }
    result = await curator.reevaluate(
        curator.ReevaluateRequest(
            subject_type="gene", subject_key="RP1", tumor_type="LUAD", evidence_pmids=["1"]
        ),
        store,
    )
    store.get_gene_annotation.assert_awaited_once_with("RP1", tumor_type="LUAD")
    assert result["fusions"] == ["RP1::ALK"]
    assert result["in_oncokb"] is True
    assert result["cancer_type_prevalence"] == "cached prevalence"
    assert [call[0] for call in store.mock_calls] == ["get_gene_annotation"]
    curator.check_oncokb_membership.assert_not_awaited()


async def test_suggestion_failure_preserves_exact_first_sentence(mocked):
    records, fetch, llm, suggestions = mocked
    records[0].abstract = "No terminal punctuation"
    suggestions.side_effect = RuntimeError("Unavailable")
    result = await curator.reevaluate(
        curator.ReevaluateRequest(subject_type="gene", subject_key="RP1", evidence_pmids=["1"])
    )
    assert result["evidence_cards"][0]["quote"] == "No terminal punctuation"


def test_pinned_filter_bypasses_mismatch_cap_and_ranking_only_when_enabled():
    records = [
        LiteratureRecord(
            pmid=str(i),
            title="lncRNA RP1 in cancer",
            abstract="lncRNA RP1 increases tumor proliferation.",
        )
        for i in range(1, 11)
    ]
    pmids = [r.pmid for r in records]
    args = dict(
        gene="RP1",
        emitted_citations=pmids + ["999"],
        records=records,
        max_citations=2,
        gene_identity="RP1 axonemal microtubule associated; gene with protein product",
    )
    assert filter_and_rank_citations(**args) == []
    assert filter_and_rank_citations(**args, pinned=True) == pmids
    assert (
        synthesis._verify_citations("RP1", pmids, records, 2, args["gene_identity"], pinned=True)
        == pmids
    )
    assert synthesis._verify_citations("RP1", pmids, records, 2, args["gene_identity"]) == []


async def test_routes_lifecycle_unknown_and_failure(mocked, monkeypatch):
    routes._jobs.clear()
    started, release = asyncio.Event(), asyncio.Event()

    async def paused(request, run_store=None):
        started.set()
        await release.wait()
        return {"citations": request.evidence_pmids}

    monkeypatch.setattr(routes, "reevaluate", paused)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/curation/reevaluate/jobs/unknown")).status_code == 404
        created = await client.post(
            "/v1/curation/reevaluate/jobs",
            json={"subject_type": "gene", "subject_key": "RP1", "evidence_pmids": ["1"]},
        )
        assert created.status_code == 200
        url = created.json()["status_url"]
        await started.wait()
        assert (await client.get(url)).json()["status"] == "running"
        release.set()
        for _ in range(20):
            result = (await client.get(url)).json()
            if result["status"] == "succeeded":
                break
            await asyncio.sleep(0)
        assert result["status"] == "succeeded"
        assert result["draft"] == {"citations": ["1"]}
        monkeypatch.setattr(routes, "reevaluate", curator.reevaluate)
        mocked[1].return_value = []
        created = await client.post(
            "/v1/curation/reevaluate/jobs",
            json={"subject_type": "fusion", "subject_key": "EML4::ALK", "evidence_pmids": ["99"]},
        )
        for _ in range(20):
            result = (await client.get(created.json()["status_url"])).json()
            if result["status"] == "failed":
                break
            await asyncio.sleep(0)
        assert result["status"] == "failed"
        assert result["invalid_pmids"]["unknown"] == ["99"]
        assert result["draft"] is None
    routes._jobs.clear()


@pytest.mark.parametrize(
    "changes",
    [
        {"evidence_pmids": ["1", "1"]},
        {"evidence_pmids": ["abc"]},
        {"subject_key": "RP1::ALK"},
        {"subject_key": " "},
    ],
)
def test_request_validation(changes):
    with pytest.raises(ValueError):
        curator.ReevaluateRequest(
            **({"subject_type": "gene", "subject_key": "RP1", "evidence_pmids": ["1"]} | changes)
        )


async def test_efetch_omitted_no_abstract_is_not_unknown(mocked):
    mocked[1].return_value = []
    curator.literature._esearch.return_value = ["1"]
    with pytest.raises(curator.InvalidPMIDs) as error:
        await curator.validated_records(["1", "2"])
    assert error.value.reasons == {"unknown": ["2"], "retracted": [], "no_abstract": ["1"]}


async def test_route_queued_generic_failure_and_auth(mocked, monkeypatch):
    routes._jobs.clear()
    pending = []
    monkeypatch.setattr(main, "_track_background_task", pending.append)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        created = await client.post(
            "/v1/curation/reevaluate/jobs",
            json={"subject_type": "gene", "subject_key": "RP1", "evidence_pmids": ["1"]},
        )
        url = created.json()["status_url"]
        assert (await client.get(url)).json()["status"] == "queued"
        monkeypatch.setattr(
            routes, "reevaluate", AsyncMock(side_effect=RuntimeError("private upstream detail"))
        )
        await pending.pop()
        result = (await client.get(url)).json()
        assert result["status"] == "failed"
        assert "private" not in result["error"]
        assert result["draft"] is None
        monkeypatch.setattr(main.settings, "auth_enabled", True)
        assert (await client.get(url)).status_code == 401
        assert (
            await client.post(
                "/v1/curation/reevaluate/jobs",
                json={"subject_type": "gene", "subject_key": "RP1", "evidence_pmids": ["1"]},
            )
        ).status_code == 401
    routes._jobs.clear()
