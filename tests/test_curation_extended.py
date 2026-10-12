# ruff: noqa: F811 - pytest fixtures imported from the base curation suites
"""Curator-set cancer-associated call, fusion-partner precedent overrides,
exact tumor-type scoping (no tumor-agnostic fallback), and race-safe startup
migrations."""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from benchmarks.run_benchmark import _BenchmarkRunStore
from src import main
from src.api import curation_routes
from src.models.schema import (
    EvidenceCard,
    FusionEvidenceResult,
    FusionPartnerEvidenceResult,
    GeneAnnotation,
)
from src.pipeline import curation
from src.pipeline import curation_reevaluate as curator
from src.pipeline.curation import (
    CURATED_RETRACTED_FLAG,
    CURATION_UNAVAILABLE_FLAG,
    CURATOR_CALL_FLAG,
    NEWER_EVIDENCE_FLAG,
    apply_current_fusion_overrides,
    apply_current_gene_overrides,
    apply_current_partner_override,
    apply_override,
    apply_partner_override,
)
from src.pipeline.results_export import annotation_to_csv_row
from tests.test_curation import (  # noqa: F401 - pytest fixtures
    FakeCurationStore,
    _fake_pipeline,
    _override,
    _pipeline_annotation,
    _poll,
    client,
    store,
)
from tests.test_curation_reevaluate import mocked, paper  # noqa: F401 - pytest fixture


def _codes(item) -> List[str]:
    flags = item["quality_flags"] if isinstance(item, dict) else item.quality_flags
    return [flag["code"] if isinstance(flag, dict) else flag.code for flag in flags]


# ---------------------------------------------------------------------------
# Payload validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "payload"),
    [
        ("/v1/curation/gene/ALK", {"cancer_associated": "maybe"}),
        ("/v1/curation/gene/ALK", {"cancer_associated": None}),  # sets no section
        ("/v1/curation/fusion_partner/ALK", {"unknown_section": "x"}),
        ("/v1/curation/fusion_partner/ALK", {"therapeutic_precedent": {"precedents": []}}),
        ("/v1/curation/fusion_partner/ALK", {"pmids": ["not-a-pmid"]}),
        ("/v1/curation/fusion_partner/EML4::ALK", {"pmids": ["111"]}),
        ("/v1/curation/fusion/EML4::ALK", {"cancer_associated": True}),
    ],
)
def test_extended_payload_validation(client, url, payload):
    assert client.put(url, json={"payload": payload}).status_code == 422


def test_cancer_associated_only_override_saves_and_is_in_history(client, store):
    resp = client.put("/v1/curation/gene/alk?tumor_type=LUAD",
                      json={"payload": {"cancer_associated": False}, "note": "not a driver here"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["override"]["payload"] == {"cancer_associated": False}
    history = client.get("/v1/curation/gene/ALK/history?tumor_type=LUAD").json()["entries"]
    assert history[0]["payload"] == {"cancer_associated": False}
    assert history[0]["note"] == "not a driver here"


# ---------------------------------------------------------------------------
# Cancer-associated call: apply + every serving path
# ---------------------------------------------------------------------------


def test_curated_call_wins_marks_field_and_resolves_insufficient_evidence():
    pipeline = _pipeline_annotation(cancer_associated=None, insufficient_evidence=True)
    curated = apply_override(pipeline, _override({"cancer_associated": True}))
    assert curated.cancer_associated is True
    assert curated.insufficient_evidence is False
    assert curated.curated_fields == ["cancer_associated"]
    flag = next(f for f in curated.quality_flags if f.code == CURATOR_CALL_FLAG)
    assert flag.severity == "info" and "Yes" in flag.detail and "undetermined" in flag.detail
    # Untouched pipeline-derived values stay pipeline values.
    assert curated.citations == pipeline.citations
    assert curated.evidence_support_score == pipeline.evidence_support_score
    assert pipeline.cancer_associated is None and pipeline.insufficient_evidence is True


def test_curated_call_survives_empty_and_retracted_citations():
    # An empty citations override alone would make the gene insufficient_evidence;
    # the curator's explicit call keeps it decided.
    curated = apply_override(_pipeline_annotation(), _override({"citations": [], "cancer_associated": False}))
    assert curated.cancer_associated is False and curated.insufficient_evidence is False

    # Every curated citation retracted: flags explain, the call stands.
    retracted = apply_override(
        _pipeline_annotation(), _override({"citations": ["222"], "cancer_associated": True}), {"222"}
    )
    assert retracted.citations == [] and retracted.insufficient_evidence is False
    assert retracted.cancer_associated is True
    assert {CURATED_RETRACTED_FLAG, CURATOR_CALL_FLAG} <= set(_codes(retracted))


def test_without_curated_call_pipeline_call_is_served():
    curated = apply_override(_pipeline_annotation(cancer_associated=False), _override({"citations": ["222"]}))
    assert curated.cancer_associated is False
    assert CURATOR_CALL_FLAG not in _codes(curated)


def test_curated_call_served_on_runs_jobs_gene_query_and_csv(client, store, monkeypatch):
    monkeypatch.setattr(main, "run_pipeline", _fake_pipeline("LUAD"))
    resp = client.put("/v1/curation/gene/ALK?tumor_type=LUAD", json={"payload": {"cancer_associated": False}})
    assert resp.status_code == 200

    # Annotation job poll (pipeline said True).
    url = client.post("/v1/annotate/jobs", json={"fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}]}
                      ).json()["status_url"]
    job = _poll(client, url)
    annotation = job["result"]["annotations"][0]
    assert annotation["cancer_associated"] is False
    assert "cancer_associated" in annotation["curated_fields"]
    assert CURATOR_CALL_FLAG in _codes(annotation)

    # Shared run link; the stored run stays pipeline-pure.
    run_id = job["result"]["run_id"]
    assert store.runs[run_id]["annotations"][0]["cancer_associated"] is True
    served = client.get(f"/v1/annotate/{run_id}").json()["annotations"][0]
    assert served["cancer_associated"] is False

    # Slim gene query job response (the sync query serves run_pipeline's own overlay).
    query_url = client.post("/v1/genes/query/jobs", json={"genes": [{"gene": "ALK", "tumor_type": "LUAD"}]}
                            ).json()["status_url"]
    slim = _poll(client, query_url)["results"][0]
    assert slim["cancer_associated"] is False
    assert "cancer_associated" in slim["curated_fields"] and CURATOR_CALL_FLAG in slim["quality_flags"]

    # CSV export of a served annotation.
    row = annotation_to_csv_row(GeneAnnotation(**served))
    assert row["cancer_associated"] == "FALSE"
    assert CURATOR_CALL_FLAG in row["quality_flags"]

    # Revert -> pipeline call again.
    client.delete("/v1/curation/gene/ALK?tumor_type=LUAD")
    assert client.get(f"/v1/annotate/{run_id}").json()["annotations"][0]["cancer_associated"] is True


def test_sole_call_removal_after_concurrent_save_is_409_and_retry_keeps_section(client, store):
    url = "/v1/curation/gene/ALK?tumor_type=LUAD"
    assert client.put(url, json={"payload": {"cancer_associated": False}}).json()["version"] == 1
    # Curator A reads v1 (only the call) and will revert; curator B adds a section first.
    seen = client.get(url).json()
    saved = client.put(url, json={"expected_version": 1, "payload": {"cancer_associated": False, "citations": ["222"]}})
    assert saved.status_code == 200 and saved.json()["version"] == 2

    stale = client.delete(f"{url}&expected_version={seen['version']}")
    assert stale.status_code == 409 and stale.json()["detail"]["current_version"] == 2
    assert store.overrides[("gene", "ALK", "luad")].payload == {"cancer_associated": False, "citations": ["222"]}

    # A's retry merges the call removal into the CURRENT override: B's section survives.
    current = client.get(url).json()
    payload = {k: v for k, v in current["override"]["payload"].items() if k != "cancer_associated"}
    retried = client.put(url, json={"expected_version": current["version"], "payload": payload})
    assert retried.status_code == 200 and retried.json()["version"] == 3
    assert store.overrides[("gene", "ALK", "luad")].payload == {"citations": ["222"]}


async def test_benchmarks_score_pipeline_calls_not_curated_ones():
    # The benchmark store has no curation support, so overrides never leak into
    # model-accuracy metrics (the curator call is closer to ground truth).
    pipeline = _pipeline_annotation()
    applied = await apply_current_gene_overrides([pipeline], _BenchmarkRunStore(), ["LUAD"])
    assert applied[0] is pipeline


def test_reevaluate_gene_draft_carries_model_suggested_call(mocked):
    import asyncio

    draft = asyncio.run(curator.reevaluate(curator.ReevaluateRequest(
        subject_type="gene", subject_key="RP1", evidence_pmids=["1", "2"],
    )))
    assert draft["cancer_associated"] is True  # the model's suggestion, for the curator to keep or flip


# ---------------------------------------------------------------------------
# No tumor-agnostic fallback
# ---------------------------------------------------------------------------


async def test_untyped_override_applies_only_to_untyped_requests():
    store = FakeCurationStore()
    store.overrides[("gene", "ALK", "")] = _override({"cancer_associated": False}, tumor_type="")
    store.overrides[("fusion", "EML4::ALK", "")] = _override(
        {"interpretation": "Curated."}, subject_type="fusion", key="EML4::ALK", tumor_type=""
    )
    store.overrides[("fusion_partner", "ALK", "")] = _override(
        {"interpretation": "Curated."}, subject_type="fusion_partner", tumor_type=""
    )
    typed, untyped = await apply_current_gene_overrides(
        [_pipeline_annotation(), _pipeline_annotation()], store, ["LUAD", None]
    )
    assert typed.curated is False and typed.cancer_associated is True
    assert untyped.curated is True and untyped.cancer_associated is False

    fusions = await apply_current_fusion_overrides(
        [FusionEvidenceResult(fusion="EML4::ALK", tumor_type="NSCLC"), FusionEvidenceResult(fusion="EML4::ALK")],
        store,
    )
    assert [f.curated for f in fusions] == [False, True]

    scoped = FusionPartnerEvidenceResult(gene="ALK", tumor_type="NSCLC", scope="tumor_type_scoped")
    assert (await apply_current_partner_override(scoped, store)).curated is False
    agnostic = FusionPartnerEvidenceResult(gene="ALK", tumor_type="NSCLC", scope="all_tumor_types")
    assert (await apply_current_partner_override(agnostic, store)).curated is True


async def test_typed_override_never_applies_to_untyped_or_other_tumor_types():
    store = FakeCurationStore()
    store.overrides[("gene", "ALK", "luad")] = _override({"cancer_associated": False})
    applied = await apply_current_gene_overrides([_pipeline_annotation()] * 3, store, ["LUAD", "BRCA", None])
    assert [a.curated for a in applied] == [True, False, False]


# ---------------------------------------------------------------------------
# Fusion-partner precedent overrides
# ---------------------------------------------------------------------------


def _partner(pmids=("111", "222"), **kwargs) -> FusionPartnerEvidenceResult:
    values: Dict[str, Any] = dict(
        gene="ALK", tumor_type="NSCLC", scope="tumor_type_scoped", has_precedent=bool(pmids),
        retrieved_count=len(pmids), pmids=list(pmids), interpretation="Pipeline interpretation.",
        evidence_cards=[EvidenceCard(pmid=p, title=f"Pipeline {p}") for p in pmids],
    )
    values.update(kwargs)
    return FusionPartnerEvidenceResult(**values)


def _partner_override(payload, *, tumor_type="nsclc", snapshot=None, version=1):
    return _override(payload, subject_type="fusion_partner", key="ALK", tumor_type=tumor_type,
                     snapshot=snapshot, version=version)


def test_apply_partner_override_retraction_newer_evidence_and_exclusions():
    override = _partner_override(
        {"interpretation": "Curated.", "pmids": ["333", "444", "666"],
         "evidence_cards": [{"pmid": "333", "quote": "Curated quote.", "selected_reason": "Partner in X::ALK"},
                            {"pmid": "666"}]},
        snapshot=["111"],
    )
    pipeline = _partner(("111", "222"))
    curated = apply_partner_override(pipeline, override, {"666"}, {"444"})
    assert curated.interpretation == "Curated."
    assert curated.pmids == ["333"] and curated.retrieved_count == 1 and curated.has_precedent is True
    assert [c.pmid for c in curated.evidence_cards] == ["333"]
    assert curated.evidence_cards[0].quote == "Curated quote."
    assert (curated.curated, curated.curation_version) == (True, 1)
    assert curated.curated_fields == ["interpretation", "pmids", "evidence_cards"]
    assert CURATED_RETRACTED_FLAG in _codes(curated)
    newer = next(f for f in curated.quality_flags if f.code == NEWER_EVIDENCE_FLAG)
    assert "222" in newer.detail and "111" not in newer.detail
    assert pipeline.curated is False and pipeline.pmids == ["111", "222"]


def test_partner_has_precedent_follows_curated_pmids_unless_set():
    removed_all = apply_partner_override(_partner(), _partner_override({"pmids": []}))
    assert removed_all.has_precedent is False and removed_all.pmids == []
    explicit = apply_partner_override(_partner(()), _partner_override({"has_precedent": True}))
    assert explicit.has_precedent is True and explicit.pmids == []


def test_partner_put_fills_cards_and_snapshots_same_search(client, store, monkeypatch):
    calls = []

    async def fake_partner(gene, tumor_type=None, agnostic=False, **_kwargs):
        calls.append((gene, tumor_type, agnostic))
        return _partner(("111",), tumor_type=tumor_type)

    monkeypatch.setattr(curation_routes.literature, "retrieve_fusion_partner_evidence", fake_partner)
    body = {"payload": {"interpretation": "Curated.", "pmids": ["222"],
                        "evidence_cards": [{"pmid": "222", "title": "ignored", "quote": "q"}]}}
    resp = client.put("/v1/curation/fusion_partner/alk?tumor_type=NSCLC", json=body)
    assert resp.status_code == 200, resp.text
    override = resp.json()["override"]
    assert resp.json()["subject_key"] == "ALK" and resp.json()["tumor_type"] == "nsclc"
    assert override["payload"]["evidence_cards"][0]["title"] == "Title 222"
    assert override["pipeline_pmids_snapshot"] == ["111"]
    client.put("/v1/curation/fusion_partner/ALK", json=body)
    assert calls == [("ALK", "nsclc", False), ("ALK", None, True)]

    bad = client.put("/v1/curation/fusion_partner/ALK", json={"payload": {"pmids": ["666"]}})
    assert bad.status_code == 422 and bad.json()["detail"]["invalid_pmids"][0]["reason"] == "retracted"
    history = client.get("/v1/curation/fusion_partner/ALK/history").json()["entries"]
    assert [e["version"] for e in history] == [1]


def test_partner_route_serves_live_override_per_search_scope(client, store, monkeypatch):
    async def fake_partner(gene, tumor_type=None, agnostic=False, exclude_pmids=None, **_kwargs):
        pmids = [p for p in ("111", "222") if p not in (exclude_pmids or set())]
        scope = "tumor_type_scoped" if tumor_type and not agnostic else "all_tumor_types"
        return _partner(tuple(pmids), tumor_type=tumor_type, scope=scope)

    monkeypatch.setattr(main, "retrieve_fusion_partner_evidence", fake_partner)
    monkeypatch.setattr(curation_routes.literature, "retrieve_fusion_partner_evidence", fake_partner)

    def lookup(**body):
        resp = client.post("/v1/fusion-partner-evidence", json={"gene": "alk", **body})
        assert resp.status_code == 200, resp.text
        return resp.json()

    assert lookup(tumor_type="NSCLC")["curated"] is False
    client.put("/v1/curation/fusion_partner/ALK?tumor_type=nsclc",
               json={"payload": {"pmids": ["333"], "interpretation": "Scoped curated."}})
    scoped = lookup(tumor_type="NSCLC")
    assert scoped["curated"] is True and scoped["pmids"] == ["333"] and scoped["interpretation"] == "Scoped curated."
    # The agnostic follow-up is a different search: no scoped override, no fallback.
    assert lookup(tumor_type="NSCLC", agnostic=True, exclude_pmids=["111"])["curated"] is False

    client.put("/v1/curation/fusion_partner/ALK",
               json={"payload": {"pmids": ["333", "444"], "interpretation": "All tumor types."}})
    follow_up = lookup(tumor_type="NSCLC", agnostic=True, exclude_pmids=["333"])
    assert follow_up["interpretation"] == "All tumor types." and follow_up["pmids"] == ["444"]
    assert lookup()["pmids"] == ["333", "444"]

    client.delete("/v1/curation/fusion_partner/ALK?tumor_type=nsclc")
    assert lookup(tumor_type="NSCLC")["curated"] is False

    store.fail_curation = True
    unavailable = lookup(tumor_type="NSCLC")
    assert unavailable["curated"] is False and CURATION_UNAVAILABLE_FLAG in _codes(unavailable)


async def test_partner_override_retraction_checked_live(monkeypatch):
    store = FakeCurationStore()
    store.overrides[("fusion_partner", "ALK", "nsclc")] = _partner_override({"pmids": ["333", "666"]})

    async def fake_retracted(pmids):
        assert set(pmids) == {"333", "666"}
        return {"666"}

    monkeypatch.setattr(curation, "find_retracted_pmids", fake_retracted)
    served = await apply_current_partner_override(_partner(), store)
    assert served.pmids == ["333"] and CURATED_RETRACTED_FLAG in _codes(served)


# ---------------------------------------------------------------------------
# Re-evaluation drafts for fusion_partner
# ---------------------------------------------------------------------------


async def test_reevaluate_partner_draft_has_verbatim_entries_in_order(mocked):
    _records, fetch, llm, suggestions = mocked
    draft = await curator.reevaluate(curator.ReevaluateRequest(
        subject_type="fusion_partner", subject_key="rp1", tumor_type="NSCLC", evidence_pmids=["2", "1"],
    ))
    llm.assert_not_awaited()  # no cancer-association synthesis for a partner lookup
    prompt = suggestions.call_args.kwargs["user"]
    assert "RP1 as a partner gene in oncogenic fusions in NSCLC" in prompt
    assert draft["gene"] == "RP1" and draft["scope"] == "tumor_type_scoped"
    assert draft["pmids"] == ["2", "1"] and draft["has_precedent"] is True
    assert draft["interpretation"] == "Suggested precedent summary."
    cards = draft["evidence_cards"]
    assert [c["pmid"] for c in cards] == ["2", "1"]
    for card in cards:
        assert card["quote"] in paper(card["pmid"]).abstract  # never the invented quote
    assert cards[1]["selected_reason"] == "Suggested reason" and cards[1]["evidence_type"] == "clinical"
    assert "curator_pinned_draft" in _codes(draft)
    # The draft is a valid partner payload once trimmed to curator-editable fields.
    curation.parse_payload("fusion_partner", {
        "interpretation": draft["interpretation"], "has_precedent": draft["has_precedent"],
        "pmids": draft["pmids"],
        "evidence_cards": [{k: c[k] for k in ("pmid", "quote", "selected_reason", "evidence_type")}
                           for c in cards],
    })


@pytest.mark.parametrize(
    "changes",
    [
        {"precedent_pmids": ["5"]},
        {"subject_key": "EML4::ALK"},
    ],
)
def test_partner_request_validation(changes):
    values = {"subject_type": "fusion_partner", "subject_key": "ALK", "evidence_pmids": ["1"], **changes}
    with pytest.raises(ValueError):
        curator.ReevaluateRequest(**values)


PARTNER_JOB = {"subject_type": "fusion_partner", "subject_key": "RP1", "evidence_pmids": ["1"]}


@pytest.mark.parametrize("case", ["viewer", "other_users_job", "too_many_pmids", "third_inflight_job"])
async def test_partner_job_rejections(case, monkeypatch):
    import httpx

    from src import auth
    from src.api import curation_reevaluate_routes as routes
    from src.auth import AuthenticatedUser

    def user(email, role="curator"):
        return AuthenticatedUser(email=email, name=email, domain="test", role=role)

    routes._jobs.clear()
    pending = []
    monkeypatch.setattr(main, "_track_background_task", pending.append)
    monkeypatch.setattr(routes, "reevaluate", AsyncMock(return_value={"pmids": ["1"]}))
    identity = {"user": user("owner@test")}
    if case == "viewer":
        # The real require_curator, with an authenticated viewer.
        async def viewer_auth(_request):
            return user("viewer@test", role="viewer")

        monkeypatch.setattr(auth, "require_auth", viewer_auth)
        monkeypatch.setattr(auth.settings, "auth_enabled", True)
    else:
        main.app.dependency_overrides[routes._curator_dependency] = lambda: identity["user"]
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            async def create(body=PARTNER_JOB):
                return await client.post("/v1/curation/reevaluate/jobs", json=body)

            if case == "viewer":
                resp = await create()
                assert resp.status_code == 403
                assert resp.json()["detail"] == "Curator privileges required for this action."
            elif case == "other_users_job":
                url = (await create()).json()["status_url"]
                identity["user"] = user("someone-else@test")
                assert (await client.get(url)).status_code == 404
            elif case == "too_many_pmids":
                too_many = {**PARTNER_JOB, "evidence_pmids": [str(i) for i in range(1, 52)]}
                resp = await create(too_many)
                assert resp.status_code == 422
                assert "at most 50" in resp.text
            else:
                assert (await create()).status_code == 200
                assert (await create()).status_code == 200
                assert (await create()).status_code == 429
            if case != "third_inflight_job":
                assert all(job.status == "queued" for job in routes._jobs.values())
                assert len(routes._jobs) == (1 if case == "other_users_job" else 0)
    finally:
        for coro in pending:
            coro.close()
        main.app.dependency_overrides.pop(routes._curator_dependency, None)
        routes._jobs.clear()


async def test_partner_job_route_lifecycle(mocked, monkeypatch):
    import asyncio

    import httpx

    from src.api import curation_reevaluate_routes as routes
    from src.auth import AuthenticatedUser

    routes._jobs.clear()
    tasks = []
    original_track = main._track_background_task
    monkeypatch.setattr(main, "_track_background_task", lambda coro: tasks.append(original_track(coro)) or tasks[-1])
    identity = AuthenticatedUser(email="c@mskcc.org", name="c", domain="test", role="curator")
    main.app.dependency_overrides[routes._curator_dependency] = lambda: identity
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
            created = await client.post("/v1/curation/reevaluate/jobs", json={
                "subject_type": "fusion_partner", "subject_key": "RP1", "evidence_pmids": ["1"],
            })
            assert created.status_code == 200, created.text
            await asyncio.gather(*tasks)
            status = (await client.get(created.json()["status_url"])).json()
            assert status["status"] == "succeeded", status
            assert status["draft"]["scope"] == "all_tumor_types" and status["draft"]["pmids"] == ["1"]
            rejected = await client.post("/v1/curation/reevaluate/jobs", json={
                "subject_type": "fusion_partner", "subject_key": "RP1", "evidence_pmids": ["1"],
                "precedent_pmids": ["2"],
            })
            assert rejected.status_code == 422
    finally:
        main.app.dependency_overrides.pop(routes._curator_dependency, None)
