"""Confidence tiers label surfaced passes; only purity demotion blocks candidates."""

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
from src.expression_context.bundle_loader import BundleLoader
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


def test_candidate_with_stored_supported_evidence_surfaces_on_every_path():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(
        ("ERBB2",), edit_gene=tiered({"BRCA": "high", "STAD": "candidate", "ESCA": "high"})
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        harness.service._clock = lambda: NOW
        store_supported(harness, "BRCA", "breast cancer", "12345678")
        store_supported(harness, "STAD", "gastric cancer", "99999999")
        assert_agreement(surfaced_everywhere(client), {"BRCA", "STAD"})
        for golden in golden_rows(client):
            assert golden["Evidence-associated PMIDs"] == "12345678; 99999999"
            assert "Stomach" in golden["Relevant expression-based cancer type(s)"]
        job = run_job(client)
        assert gate.calls == [("ERBB2", "esophageal cancer")]
        assert job["literature_reused"] == 2
        assert_agreement(surfaced_everywhere(client, job["job_id"]), {"BRCA", "STAD"})
        for row in golden_rows(client):
            assert row["Evidence-associated PMIDs"] == "12345678; 99999999"
            assert row["Relevant expression-based cancer type(s)"] == "Breast cancer; Stomach adenocarcinoma"
        stats = client.get(f"{PREFIX}/stats/ERBB2").json()
        stad = next(p for p in stats["pairs"] if p["oncotree_code"] == "STAD")
        assert (stad["status"], stad["exclusion_reason"]) == ("surfaced", None)
        assert stad["confidence_tier"] == "candidate" and stad["stats_pass"] is True
        assert stad["literature_eligible"] and stad["literature"]["supported"]
        assert stad["confidence_label"] == "Moderate"
        assert stats["summary"]["candidate"] == 0 and stats["summary"]["surfaced"] == 2
        genes = client.get(f"{PREFIX}/genes/ERBB2?include_excluded=true").json()
        polled = client.get(f"{PREFIX}/jobs/{job['job_id']}").json()["result"]
        for result in (genes, polled):
            assert {p["oncotree_code"]: p["confidence_label"] for p in result["surfaced"]} == {
                "BRCA": "Strong", "STAD": "Moderate",
            }
            assert "STAD" not in {p["oncotree_code"] for p in result["excluded"]}
        _, shown = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
        by_code = {r["oncotree_code"]: r for r in shown}
        assert set(by_code) == {"BRCA", "STAD"}
        assert by_code["BRCA"]["confidence_tier"] == "high"
        assert by_code["BRCA"]["confidence_label"] == "Strong"
        assert by_code["STAD"]["confidence_label"] == "Moderate"
        assert by_code["STAD"]["confidence_tier"] == "candidate"
        assert by_code["STAD"]["tumor_specific_pmids"] == "99999999"
        _, every = parse_csv_response(
            client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text
        )
        all_codes = {r["oncotree_code"]: r for r in every}
        assert (all_codes["STAD"]["surfaced"], all_codes["STAD"]["exclusion_reason"]) == ("yes", "")
        assert all_codes["STAD"]["confidence_tier"] == "candidate"
        assert all_codes["LUAD"]["confidence_tier"] == ""  # stats fail: null tier

        row = harness.store.rows[("ERBB2", "STAD", RELEASE_ID, TUMOR_SPECIFIC_GATE)]
        assert row.surfaced and row.exclusion_reason is None and row.evidence


def test_candidates_trigger_literature_calls_and_surface_when_supported():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(
        ("ERBB2",),
        edit_gene=tiered({"BRCA": "candidate", "STAD": "candidate", "ESCA": "candidate"}),
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        pending = client.get(f"{PREFIX}/stats/ERBB2").json()
        job = run_job(client)
        assert sorted(gate.calls) == [("ERBB2", "breast cancer"), ("ERBB2", "esophageal cancer"), ("ERBB2", "gastric cancer")]
        for p in pending["pairs"]:
            if p["oncotree_code"] in {"BRCA", "STAD", "ESCA"}:
                assert p["status"] == "literature_pending" and p["literature_eligible"]
                assert p["confidence_label"] is None
        assert job["literature_evaluated"] == 3 and job["literature_reused"] == 0
        assert {p["confidence_label"] for p in job["result"]["surfaced"]} == {"Moderate"}
        reasons = {p["oncotree_code"]: p["exclusion_reason"] for p in job["result"]["excluded"]}
        assert reasons == {"ESCA": "literature", "UCEC": "precheck", "LUAD": "stats"}
        assert_agreement(surfaced_everywhere(client, job["job_id"]), {"BRCA", "STAD"})
        for row in golden_rows(client):
            assert row["Relevant expression-based cancer type(s)"] == "Breast cancer; Stomach adenocarcinoma"


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
    assert {r["confidence_label"] for r in before["csv"] if r["surfaced"] == "yes"} == {"Strong"}


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


ABSENT = object()


@pytest.mark.parametrize(
    "version,required",
    [
        # Legacy fallback only for a well-formed version below 1.3.
        ("1.2", False),
        ("1.1", False),
        ("1.2.0", False),
        # Tiers required from 1.3.
        ("1.3", True),
        ("1.10", True),
        ("1.3.1", True),
        ("2.0", True),
        # Missing / empty: fail closed.
        (ABSENT, True),
        (None, True),
        ("", True),
        # Signed, padded or malformed components: fail closed.
        ("1.-1", True),
        ("-1.3", True),
        ("+1.2", True),
        ("1.2.x", True),
        (" 1.2", True),
        ("1.2 ", True),
        ("1.2\n", True),
        ("1", True),
        ("1.2.3.4", True),
        ("one.two", True),
        ("\uff11.\uff12", True),  # full-width digits are not ASCII digits
        (1.2, True),  # non-strings
        (12, True),
    ],
)
def test_contract_version_gates_the_legacy_fallback(version, required):
    data = dict(build_bundle(("ERBB2",)).manifest)
    if version is ABSENT:
        data.pop("contract_version", None)
    else:
        data["contract_version"] = version
        assert confidence_tier_required(version) is required
    assert BundleManifest.model_validate(data).confidence_tier_required is required


@pytest.mark.parametrize("tier", [None, "medium", "garbled"])
def test_explicit_null_or_unknown_tier_on_a_pass_surfaces_as_moderate(tier):
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(("ERBB2",), edit_gene=tiered({"BRCA": tier, "STAD": tier}))
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        job = run_job(client)
        assert ("ERBB2", "breast cancer") in gate.calls
        assert ("ERBB2", "gastric cancer") in gate.calls
        assert {p["confidence_label"] for p in job["result"]["surfaced"]} == {"Moderate"}
        assert_agreement(surfaced_everywhere(client, job["job_id"]), {"BRCA", "STAD"})


# --------------------------------------------------------------------------
# Contract version gates the "missing => high" fallback
# --------------------------------------------------------------------------


def with_contract(version, tiers: Dict[str, str]):
    """ERBB2-only bundle declaring ``contract_version`` (ABSENT: no such key);
    only ``tiers`` are set (every other pair omits confidence_tier entirely)."""
    def edit(doc):
        tiered(tiers, explicit_null_for_failures=False)(doc)
        for row in doc["cancer_types"]:
            if row.get("confidence_tier") == "high":
                row["purity_check"] = "pass"

    bundle = build_bundle(
        ("ERBB2",),
        manifest_overrides={"contract_version": version},
        edit_gene=edit,
    )
    if version is ABSENT:
        bundle.manifest.pop("contract_version")
    return bundle


@pytest.mark.parametrize("tiers", [{}, {"BRCA": "high"}], ids=["all_omitted", "some_omitted"])
@pytest.mark.parametrize(
    "version", ["1.3", ABSENT, "", "+1.2", " 1.2"], ids=["1.3", "absent", "empty", "signed", "padded"]
)
def test_tier_requiring_bundle_with_omitted_tiers_is_eligible_and_warns(version, tiers, caplog):
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = with_contract(version, tiers)
    harness = CsvHarness(gate=gate, bundle=bundle)
    with caplog.at_level(logging.WARNING, logger="src.expression_context.service"):
        with TestClient(harness.app) as client:
            harness.service._clock = lambda: NOW
            store_supported(harness, "STAD", "gastric cancer", "99999999")
            assert_agreement(surfaced_everywhere(client), {"STAD"})
            job = run_job(client)
            assert sorted(gate.calls) == [("ERBB2", "breast cancer"), ("ERBB2", "esophageal cancer")]
            assert_agreement(surfaced_everywhere(client, job["job_id"]), {"BRCA", "STAD"})
            for row in golden_rows(client):
                assert "99999999" in row["Evidence-associated PMIDs"]
                assert "Stomach" in row["Relevant expression-based cancer type(s)"]
            pairs = {
                p["oncotree_code"]: p for p in client.get(f"{PREFIX}/stats/ERBB2").json()["pairs"]
            }
            assert pairs["STAD"]["status"] == "surfaced"
            assert pairs["STAD"]["confidence_label"] == "Moderate"
            assert pairs["ESCA"]["status"] == "excluded"
            assert pairs["ESCA"]["exclusion_reason"] == "literature"
            for code in ("STAD", "ESCA"):
                assert pairs[code]["confidence_tier"] is None
                assert pairs[code]["literature_eligible"] is True
            assert pairs["BRCA"]["confidence_label"] == ("Strong" if tiers else "Moderate")
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
    "version,tiers",
    [
        ("1.3", {}),
        ("1.3", {"BRCA": "high"}),
        ("1.3", {"BRCA": "high", "STAD": "candidate", "ESCA": "high", "UCEC": "high"}),
        ("1.2", {}),
        # Missing or malformed versions do not get the legacy Strong fallback.
        (None, {}),
        (" 1.2", {}),
    ],
)
def test_loader_accepts_untiered_passes_for_moderate_fallback(version, tiers):
    def edit(doc):
        tiered(tiers, explicit_null_for_failures=False)(doc)
        for row in doc["cancer_types"]:
            if row.get("confidence_tier") == "high":
                row["purity_check"] = "pass"

    bundle = build_bundle(
        ("ERBB2",),
        manifest_overrides={"contract_version": version},
        edit_gene=edit,
    )
    if version is None:
        bundle.manifest.pop("contract_version")
    manifest = BundleManifest.model_validate(bundle.manifest)
    line = json.dumps(bundle.genes[0]).encode()
    assert BundleLoader._gene_row(line, manifest, set(), 1)[0] == "ERBB2"


# --------------------------------------------------------------------------
# Every literature scheduling path checks candidates
# --------------------------------------------------------------------------


def test_the_gate_is_reachable_only_through_evaluate():
    """Structural guard: the paid gate has one call site, behind evaluate()'s
    view eligibility filter, so the paths below cover every scheduling route."""
    source = (Path(__file__).parent.parent / "src/expression_context/service.py").read_text()
    assert source.count("self.gate.evaluate(") == 1
    assert source.count("self._evaluate_and_store(") == 2  # both inside _resolve_verdict
    assert source.count("self._single_flight_verdict(") == 1  # only evaluate()'s check()
    assert source.count("self.evaluate(") == 1  # only _run_job


def test_reevaluation_concurrent_direct_and_post_refresh_runs_check_candidates():
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
        assert errors == [] and evaluated == 3  # all eligible tiers
        assert {p.oncotree_code for p in result.surfaced} == {"BRCA", "STAD"}
        assert gate.calls.count(("ERBB2", "breast cancer")) == 2  # first job + direct
        assert gate.calls.count(gastric) == 2
        assert gate.calls.count(("ERBB2", "esophageal cancer")) == 2

        # 4. Bundle refresh: release B demotes BRCA to a candidate. Its release-A
        # verdict is never reused; all eligible pairs are checked under B.
        release_b = build_bundle(
            ("ERBB2",),
            release_id="datahub-abc1234def56-m2-b7e9f01a2c3e",
            edit_gene=tiered({"BRCA": "candidate", "STAD": "candidate", "ESCA": "candidate"}),
        )
        asyncio.run(seed_bundle(harness.bundles, release_b))
        service.catalog.invalidate()
        before = list(gate.calls)
        job = run_job(client)
        assert len(gate.calls) == len(before) + 3, "fresh release needs fresh literature for every tier"
        assert job["release_id"] == release_b.release_id
        assert {p["confidence_label"] for p in job["result"]["surfaced"]} == {"Moderate"}
        assert_agreement(surfaced_everywhere(client, job["job_id"]), {"BRCA", "STAD"})
