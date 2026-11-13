"""Purity demotion remains authoritative over stored literature on every path."""

import json

import pytest
from fastapi.testclient import TestClient

from src.expression_context.bundle_loader import BundleContractError, BundleLoader
from src.expression_context.models import BundleManifest, CancerTypeStats, purity_check_required
from tests.expression_context_helpers import FakeLiteratureGate, build_bundle
from tests.test_expression_context_confidence_tier import (
    NOW,
    SUPPORTING_GATE,
    assert_agreement,
    golden_rows,
    store_supported,
    surfaced_everywhere,
)
from tests.test_expression_context_csv import PREFIX, CsvHarness, parse_csv_response, run_job

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]


@pytest.mark.parametrize("tier", ["high", "candidate"])
def test_demoted_never_surfaces_or_calls_literature(tier):
    def edit(doc):
        for row in doc["cancer_types"]:
            row["confidence_tier"] = tier if row["stats_pass"] else None
            row["purity_check"] = "demoted" if row["stats_pass"] else None

    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(
        ("ERBB2",), manifest_overrides={"contract_version": "1.4"}, edit_gene=edit
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        harness.service._clock = lambda: NOW
        store_supported(harness, "BRCA", "breast cancer", "12345678")
        store_supported(harness, "STAD", "gastric cancer", "23456789")
        assert_agreement(surfaced_everywhere(client), set())
        assert all(not row["Relevant expression-based cancer type(s)"]
                   and not row["Evidence-associated PMIDs"] for row in golden_rows(client))
        job = run_job(client)
        assert_agreement(surfaced_everywhere(client, job["job_id"]), set())
        assert gate.calls == []
        _, rows = parse_csv_response(
            client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text
        )
        passing = [r for r in rows if r["stats_pass"] == "yes"]
        assert passing and all(r["purity_check"] == "demoted" for r in passing)
        assert all(r["confidence_tier"] == "candidate" for r in passing)


@pytest.mark.parametrize("version,expected", [("1.3", {"BRCA"}), ("1.4", set())])
def test_missing_purity_versioned_fail_closed(version, expected):
    def edit(doc):
        for row in doc["cancer_types"]:
            row["confidence_tier"] = "high" if row["stats_pass"] else None

    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(
        ("ERBB2",), manifest_overrides={"contract_version": version}, edit_gene=edit
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        harness.service._clock = lambda: NOW
        store_supported(harness, "BRCA", "breast cancer", "12345678")
        assert_agreement(surfaced_everywhere(client), expected)
        job = run_job(client)
        assert_agreement(
            surfaced_everywhere(client, job["job_id"]),
            expected | ({"STAD"} if version == "1.3" else set()),
        )
        if version == "1.4":
            assert gate.calls == []


@pytest.mark.parametrize(
    "version,required",
    [
        ("1.3", False),
        ("1.3.9", False),
        ("1.4", True),
        ("2.0", True),
        (None, True),
        ("", True),
        (" 1.3", True),
        ("+1.3", True),
    ],
)
def test_purity_contract_versions(version, required):
    assert purity_check_required(version) is required


@pytest.mark.parametrize(
    "state,high",
    [
        ("pass", True),
        ("unassessed", True),
        ("not_applicable", True),
        ("demoted", False),
        (None, False),
        ("unknown", False),
    ],
)
def test_only_known_retaining_states_allow_high(state, high):
    stats = CancerTypeStats(
        oncotree_code="BRCA",
        n=30,
        n_rest=100,
        stats_pass=True,
        confidence_tier="high",
        purity_check=state,
    )
    assert stats.high_confidence(tier_required=True, purity_required=True) is high


@pytest.mark.parametrize("version,rejected", [("1.3", False), ("1.4", True)])
def test_loader_missing_purity_on_high_pair(version, rejected):
    def edit(doc):
        for row in doc["cancer_types"]:
            row["confidence_tier"] = "high" if row["stats_pass"] else None

    bundle = build_bundle(("ERBB2",), manifest_overrides={"contract_version": version}, edit_gene=edit)
    manifest = BundleManifest.model_validate(bundle.manifest)
    line = json.dumps(bundle.genes[0]).encode()
    if rejected:
        with pytest.raises(BundleContractError, match="requires purity_check"):
            BundleLoader._gene_row(line, manifest, set(), 1)
    else:
        assert BundleLoader._gene_row(line, manifest, set(), 1)[0] == "ERBB2"
