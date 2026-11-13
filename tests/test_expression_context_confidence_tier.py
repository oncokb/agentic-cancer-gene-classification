"""Confidence tier (expression_stats contract 1.3): only high-tier pairs surface.

Rule (service._evaluate_gene_view, the single predicate): a pair is surfaced
only if its statistics pass, its effective ``confidence_tier`` is "high", the
effective PubMed pre-check is not a fresh false and a valid tumor-specific v2
verdict for the active release supports it. Candidates (v3 passes below the
high tier) are listed with status "candidate", never surfaced on any read
path, never sent to the literature gate and never exported as golden-set rows.
Bundles without the field (contract <= 1.2) treat every stats pass as high.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, Set

import asyncio
import json
import logging
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.expression_context.literature_gate import TUMOR_SPECIFIC_GATE
from src.expression_context.bundle_loader import BundleContractError, BundleLoader
from src.expression_context.models import (
    BundleManifest,
    CancerTypeStats,
    StoredPair,
    confidence_tier_required,
)
from tests.expression_context_helpers import (
    RELEASE_ID,
    FakeLiteratureGate,
    build_bundle,
    gene_file,
    seed_bundle,
)
from tests.test_expression_context_csv import (
    PREFIX,
    CsvHarness,
    _v2_supported_evidence,
    parse_csv_response,
    run_job,
)

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
# Both passing ERBB2 pairs would surface on literature alone.
SUPPORTING_GATE = {
    "breast cancer": (False, ["12345678"]),
    "gastric cancer": (False, ["23456789"]),
}


def tiered(tiers: Dict[str, str], *, explicit_null_for_failures: bool = True):
    """edit_gene hook: set confidence_tier on ERBB2's cancer types."""

    def edit(doc):
        if doc["gene"]["hugo_symbol"] != "ERBB2":
            return
        for ct in doc["cancer_types"]:
            if ct["oncotree_code"] in tiers:
                ct["confidence_tier"] = tiers[ct["oncotree_code"]]
            elif explicit_null_for_failures and not ct["stats_pass"]:
                ct["confidence_tier"] = None

    return edit


def surfaced_everywhere(client: TestClient, job_id: str | None = None) -> Dict[str, Set[str]]:
    """The surfaced ERBB2 cohort codes as seen by every read path."""
    paths: Dict[str, Set[str]] = {}
    stats = client.get(f"{PREFIX}/stats/ERBB2").json()
    paths["stats"] = {p["oncotree_code"] for p in stats["pairs"] if p["status"] == "surfaced"}
    genes = client.get(f"{PREFIX}/genes/ERBB2").json()
    paths["genes"] = {p["oncotree_code"] for p in genes["surfaced"]}
    genes_all = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}).json()
    paths["genes_all"] = {p["oncotree_code"] for p in genes_all["surfaced"]}
    _, rows = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
    paths["csv"] = {r["oncotree_code"] for r in rows}
    assert all(r["surfaced"] == "yes" for r in rows)
    _, rows = parse_csv_response(
        client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text
    )
    paths["csv_all"] = {r["oncotree_code"] for r in rows if r["surfaced"] == "yes"}
    _, rows = parse_csv_response(client.get(f"{PREFIX}/export.csv").text)
    paths["bulk"] = {r["oncotree_code"] for r in rows if r["gene_symbol"] == "ERBB2"}
    _, rows = parse_csv_response(
        client.get(f"{PREFIX}/export.csv", params={"include_excluded": "true"}).text
    )
    paths["bulk_all"] = {
        r["oncotree_code"] for r in rows if r["gene_symbol"] == "ERBB2" and r["surfaced"] == "yes"
    }
    history = client.get(f"{PREFIX}/genes").json()
    entry = next(h for h in history["items"] if h["hugo_symbol"] == "ERBB2")
    paths["history_count"] = {str(entry["surfaced_count"])}
    if job_id is not None:
        polled = client.get(f"{PREFIX}/jobs/{job_id}").json()
        paths["job"] = {p["oncotree_code"] for p in polled["result"]["surfaced"]}
    return paths


def golden_rows(client: TestClient):
    single = client.get(f"{PREFIX}/golden-set.csv", params={"genes": "ERBB2"})
    assert single.status_code == 200, single.text
    _, rows = parse_csv_response(single.text)
    _, bulk = parse_csv_response(client.get(f"{PREFIX}/golden-set.csv").text)
    return rows + [r for r in bulk if r["Genes"] == "ERBB2"]


def assert_agreement(paths: Dict[str, Set[str]], expected: Set[str]) -> None:
    counts = paths.pop("history_count")
    assert counts == {str(len(expected))}, counts
    assert paths and all(codes == expected for codes in paths.values()), paths


def store_supported(harness: CsvHarness, code: str, query: str, pmid: str) -> None:
    stats = next(ct for ct in harness.bundle.genes[0]["cancer_types"] if ct["oncotree_code"] == code)
    harness.store.rows[("ERBB2", code, RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code=code,
        release_id=RELEASE_ID,
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=dict(stats),
        evidence=_v2_supported_evidence(pmid, query),
        computed_at=NOW,
    )


def test_candidate_with_stored_supported_evidence_is_never_surfaced_on_any_path():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(
        ("ERBB2",), edit_gene=tiered({"BRCA": "high", "STAD": "candidate", "ESCA": "high"})
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        harness.service._clock = lambda: NOW
        # Stored, valid, tumor-specific v2 "supported" evidence for both pairs.
        store_supported(harness, "BRCA", "breast cancer", "12345678")
        store_supported(harness, "STAD", "gastric cancer", "99999999")

        assert_agreement(surfaced_everywhere(client), {"BRCA"})
        for row in golden_rows(client):
            assert row["Evidence-associated PMIDs"] == "12345678"
            assert "Stomach" not in row["Relevant expression-based cancer type(s)"]

        job = run_job(client)
        # Stored BRCA is reused, ESCA (high) is checked, the STAD candidate never is.
        assert gate.calls == [("ERBB2", "esophageal cancer")]
        assert_agreement(surfaced_everywhere(client, job["job_id"]), {"BRCA"})
        for row in golden_rows(client):
            assert "99999999" not in row["Evidence-associated PMIDs"]
            assert "Stomach" not in row["Relevant expression-based cancer type(s)"]

        stats = client.get(f"{PREFIX}/stats/ERBB2").json()
        stad = next(p for p in stats["pairs"] if p["oncotree_code"] == "STAD")
        assert (stad["status"], stad["exclusion_reason"]) == ("candidate", "candidate")
        assert stad["confidence_tier"] == "candidate" and stad["stats_pass"] is True
        assert stad["literature_eligible"] is False and stad["literature"] is None
        assert stats["summary"]["candidate"] == 1 and stats["summary"]["surfaced"] == 1
        genes_all = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}).json()
        excluded = {p["oncotree_code"]: p for p in genes_all["excluded"]}
        assert excluded["STAD"]["exclusion_reason"] == "candidate"

        # Per-pair CSV: candidates only with include_excluded, tier column filled.
        _, shown = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
        assert [r["oncotree_code"] for r in shown] == ["BRCA"]
        assert shown[0]["confidence_tier"] == "high"
        _, every = parse_csv_response(
            client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text
        )
        by_code = {r["oncotree_code"]: r for r in every}
        assert (by_code["STAD"]["surfaced"], by_code["STAD"]["exclusion_reason"]) == (
            "no",
            "candidate",
        )
        assert by_code["STAD"]["confidence_tier"] == "candidate"
        assert by_code["STAD"]["tumor_specific_pmids"] == ""
        assert by_code["LUAD"]["confidence_tier"] == ""  # stats fail: null tier

        # The job stored the candidate as such, without evidence.
        row = harness.store.rows[("ERBB2", "STAD", RELEASE_ID, TUMOR_SPECIFIC_GATE)]
        assert (row.surfaced, row.exclusion_reason, row.evidence) == (False, "candidate", None)


def test_candidates_never_trigger_a_literature_call():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(
        ("ERBB2",),
        edit_gene=tiered({"BRCA": "candidate", "STAD": "candidate", "ESCA": "candidate"}),
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        job = run_job(client)
        assert gate.calls == []
        assert job["literature_evaluated"] == 0 and job["literature_reused"] == 0
        assert job["result"]["surfaced"] == []
        reasons = {p["oncotree_code"]: p["exclusion_reason"] for p in job["result"]["excluded"]}
        assert reasons == {
            "BRCA": "candidate",
            "STAD": "candidate",
            "ESCA": "candidate",
            # UCEC passes v3 (missing tier there = high) but its fresh pre-check fails.
            "UCEC": "precheck",
            "LUAD": "stats",
        }
        assert_agreement(surfaced_everywhere(client, job["job_id"]), set())


def snapshot(bundle):
    """Everything a curator or client can observe for ERBB2 after a job."""
    gate = FakeLiteratureGate()
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        job = run_job(client)
        stats = client.get(f"{PREFIX}/stats/ERBB2").json()
        pairs = [
            {k: v for k, v in p.items() if k not in ("stats", "literature")} for p in stats["pairs"]
        ]
        _, every = parse_csv_response(
            client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text
        )
        return dict(
            calls=sorted(gate.calls),
            summary=stats["summary"],
            pairs=pairs,
            surfaced=surfaced_everywhere(client, job["job_id"]),
            csv=[
                {k: v for k, v in r.items() if k != "literature_checked_at"} for r in every
            ],
            golden=golden_rows(client),
        )


def test_old_bundle_without_the_field_behaves_exactly_as_before():
    old = build_bundle(("ERBB2",))
    assert all("confidence_tier" not in ct for ct in old.genes[0]["cancer_types"])
    passing = {
        ct["oncotree_code"] for ct in gene_file("ERBB2")["cancer_types"] if ct["stats_pass"]
    }
    explicit = build_bundle(("ERBB2",), edit_gene=tiered(dict.fromkeys(passing, "high")))
    before, after = snapshot(old), snapshot(explicit)
    # Same literature calls, statuses, surfaced sets, CSV rows and golden rows.
    assert before == after
    assert before["surfaced"]["stats"] == {"BRCA"}  # the fixture's long-standing result
    assert before["calls"] == [("ERBB2", "breast cancer"), ("ERBB2", "esophageal cancer"),
                               ("ERBB2", "gastric cancer")]  # fmt: skip
    assert before["summary"]["candidate"] == 0
    assert {r["confidence_tier"] for r in before["csv"] if r["stats_pass"] == "yes"} == {"high"}


def stats_row(**fields) -> CancerTypeStats:
    base = dict(oncotree_code="BRCA", n=100, n_rest=9000, stats_pass=True)
    base.update(fields)
    return CancerTypeStats.model_validate(base)


@pytest.mark.parametrize(
    "fields,tier",
    [
        ({}, "high"),  # missing field (bundle before contract 1.3)
        ({"confidence_tier": "high"}, "high"),
        ({"confidence_tier": "candidate"}, "candidate"),
        ({"confidence_tier": None}, None),  # explicit null on a pass: fail closed
        ({"confidence_tier": "medium"}, "medium"),  # unknown value: loads, not high
        ({"stats_pass": False}, None),
        ({"stats_pass": False, "confidence_tier": "high"}, None),
        # Broad-blocked m3 pair: statistics don't pass, so no tier is acted on.
        ({"lineage_blocked_by_broad": True, "pass_mode": None, "confidence_tier": "high"}, None),
    ],
)
def test_effective_tier_before_contract_1_3(fields, tier):
    stats = stats_row(**fields)
    assert stats.effective_confidence_tier(tier_required=False) == tier
    assert stats.high_confidence(tier_required=False) is (tier == "high")


def test_missing_tier_fails_closed_when_the_contract_requires_it():
    missing = stats_row()
    assert missing.confidence_tier_missing
    assert missing.effective_confidence_tier(tier_required=True) is None
    assert missing.high_confidence(tier_required=True) is False
    present = stats_row(confidence_tier="high")
    assert not present.confidence_tier_missing
    assert present.high_confidence(tier_required=True) is True
    assert not stats_row(stats_pass=False).confidence_tier_missing


@pytest.mark.parametrize(
    "version,required",
    [
        (None, False),  # contract < 1.1 (field absent)
        ("1.1", False),
        ("1.2", False),
        ("1.3", True),
        ("1.10", True),
        ("2.0", True),
        (" 1.3 ", True),
        ("1.3.1", True),  # unexpected shape: fail closed
        ("one.three", True),
        (1.2, False),  # loose types still parse by their text
        (1.3, True),
    ],
)
def test_contract_version_gates_the_legacy_fallback(version, required):
    assert confidence_tier_required(version) is required
    data = dict(gene_file("ERBB2") and build_bundle(("ERBB2",)).manifest)
    if version is None:
        data.pop("contract_version", None)
    else:
        data["contract_version"] = version
    assert BundleManifest.model_validate(data).confidence_tier_required is required


def test_explicit_null_or_unknown_tier_on_a_pass_is_not_surfaced():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(("ERBB2",), edit_gene=tiered({"BRCA": None, "STAD": "medium"}))
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        job = run_job(client)
        assert ("ERBB2", "breast cancer") not in gate.calls
        assert ("ERBB2", "gastric cancer") not in gate.calls
        assert_agreement(surfaced_everywhere(client, job["job_id"]), set())


# --------------------------------------------------------------------------
# Contract version gates the "missing => high" fallback
# --------------------------------------------------------------------------


def with_contract(version: str, tiers: Dict[str, str]):
    """ERBB2-only bundle declaring ``contract_version``; only ``tiers`` are set
    (every other pair omits confidence_tier entirely)."""
    return build_bundle(
        ("ERBB2",),
        manifest_overrides={"contract_version": version},
        edit_gene=tiered(tiers, explicit_null_for_failures=False),
    )


@pytest.mark.parametrize(
    "tiers,expected_calls,expected_surfaced",
    [
        # ALL tiers omitted: nothing is high, so no paid call and nothing surfaces.
        ({}, [], set()),
        # SOME omitted: only the explicit high pair is checked (and surfaces).
        ({"BRCA": "high"}, [("ERBB2", "breast cancer")], {"BRCA"}),
    ],
    ids=["all_omitted", "some_omitted"],
)
def test_contract_1_3_bundle_with_omitted_tiers_fails_closed_everywhere(
    tiers, expected_calls, expected_surfaced, caplog
):
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = with_contract("1.3", tiers)
    harness = CsvHarness(gate=gate, bundle=bundle)
    with caplog.at_level(logging.WARNING, logger="src.expression_context.service"):
        with TestClient(harness.app) as client:
            harness.service._clock = lambda: NOW
            # Even stored, valid, supported evidence for an untiered pass never surfaces.
            store_supported(harness, "STAD", "gastric cancer", "99999999")
            assert_agreement(surfaced_everywhere(client), set())  # BRCA not checked yet
            job = run_job(client)
            assert gate.calls == expected_calls
            assert_agreement(surfaced_everywhere(client, job["job_id"]), expected_surfaced)
            for row in golden_rows(client):
                assert "99999999" not in row["Evidence-associated PMIDs"]
                assert "Stomach" not in row["Relevant expression-based cancer type(s)"]
            pairs = {
                p["oncotree_code"]: p for p in client.get(f"{PREFIX}/stats/ERBB2").json()["pairs"]
            }
            for code in ("STAD", "ESCA"):  # untiered passes
                assert pairs[code]["status"] == "candidate"
                assert pairs[code]["confidence_tier"] is None
                assert pairs[code]["literature_eligible"] is False
    warnings = [r for r in caplog.records if "without confidence_tier" in r.getMessage()]
    assert len(warnings) == 1, "warned once per bundle, not per request"
    assert bundle.release_id in warnings[0].getMessage()


def test_contract_1_2_bundle_without_the_field_keeps_legacy_behaviour(caplog):
    passing = {
        ct["oncotree_code"] for ct in gene_file("ERBB2")["cancer_types"] if ct["stats_pass"]
    }
    explicit = build_bundle(("ERBB2",), edit_gene=tiered(dict.fromkeys(passing, "high")))
    with caplog.at_level(logging.WARNING, logger="src.expression_context.service"):
        legacy = snapshot(with_contract("1.2", {}))
    assert legacy == snapshot(explicit)
    assert legacy["surfaced"]["stats"] == {"BRCA"}
    assert len(legacy["calls"]) == 3
    assert not [r for r in caplog.records if "without confidence_tier" in r.getMessage()]


@pytest.mark.parametrize(
    "version,tiers,rejected",
    [
        ("1.3", {}, True),
        ("1.3", {"BRCA": "high"}, True),  # one untiered pass is enough
        ("1.3", {"BRCA": "high", "STAD": "candidate", "ESCA": "high", "UCEC": "high"}, False),
        ("1.2", {}, False),
        (None, {}, False),
    ],
)
def test_loader_rejects_a_1_3_bundle_with_untiered_passes(version, tiers, rejected):
    overrides = {} if version is None else {"contract_version": version}
    bundle = build_bundle(
        ("ERBB2",),
        manifest_overrides=overrides,
        edit_gene=tiered(tiers, explicit_null_for_failures=False),
    )
    manifest = BundleManifest.model_validate(bundle.manifest)
    line = json.dumps(bundle.genes[0]).encode()
    if rejected:
        with pytest.raises(BundleContractError, match="requires confidence_tier"):
            BundleLoader._gene_row(line, manifest, set(), 1)
    else:
        assert BundleLoader._gene_row(line, manifest, set(), 1)[0] == "ERBB2"


# --------------------------------------------------------------------------
# Every literature scheduling path skips candidates
# --------------------------------------------------------------------------


def test_the_gate_is_reachable_only_through_evaluate():
    """Structural guard: the paid gate has one call site, behind evaluate()'s
    candidate filter, so the paths below cover every scheduling route."""
    source = (Path(__file__).parent.parent / "src/expression_context/service.py").read_text()
    assert source.count("self.gate.evaluate(") == 1
    assert source.count("self._evaluate_and_store(") == 2  # both inside _resolve_verdict
    assert source.count("self._single_flight_verdict(") == 1  # only evaluate()'s check()
    assert source.count("self.evaluate(") == 1  # only _run_job


def test_reevaluation_concurrent_direct_and_post_refresh_runs_skip_candidates():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    tiers = {"BRCA": "high", "STAD": "candidate", "ESCA": "candidate"}
    bundle = build_bundle(("ERBB2",), edit_gene=tiered(tiers))
    harness = CsvHarness(gate=gate, bundle=bundle)
    gastric = ("ERBB2", "gastric cancer")
    with TestClient(harness.app) as client:
        # 1. First job, then a re-evaluation (second job) of the same gene.
        run_job(client)
        run_job(client)
        # 2. Concurrent jobs for the same gene.
        urls = [
            client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).json()["status_url"]
            for _ in range(3)
        ]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and any(
            client.get(u).json()["status"] not in ("succeeded", "failed") for u in urls
        ):
            time.sleep(0.01)
        assert all(client.get(u).json()["status"] == "succeeded" for u in urls)
        # 3. Direct service.evaluate (no job wrapper), with stored rows cleared.
        harness.store.rows.clear()
        service = harness.service

        async def direct():
            resolved = await service.resolve("ERBB2")
            return await service.evaluate(resolved, None)

        result, evaluated, _, errors = client.portal.call(direct)
        assert errors == [] and evaluated == 1  # BRCA only
        assert {p.oncotree_code for p in result.surfaced} == {"BRCA"}
        assert gate.calls.count(("ERBB2", "breast cancer")) == 2  # first job + direct
        assert gastric not in gate.calls
        assert ("ERBB2", "esophageal cancer") not in gate.calls

        # 4. Bundle refresh: release B demotes BRCA to a candidate. Its release-A
        # verdict is never reused and BRCA is never re-checked under B.
        release_b = build_bundle(
            ("ERBB2",),
            release_id="datahub-abc1234def56-m2-b7e9f01a2c3e",
            edit_gene=tiered({"BRCA": "candidate", "STAD": "candidate", "ESCA": "candidate"}),
        )
        asyncio.run(seed_bundle(harness.bundles, release_b))
        service.catalog.invalidate()
        before = list(gate.calls)
        job = run_job(client)
        assert gate.calls == before, "no literature call for candidates after a refresh"
        assert job["release_id"] == release_b.release_id
        assert job["result"]["surfaced"] == []
        assert_agreement(surfaced_everywhere(client, job["job_id"]), set())
