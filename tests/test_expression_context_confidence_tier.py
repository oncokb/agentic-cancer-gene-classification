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

import pytest
from fastapi.testclient import TestClient

from src.expression_context.literature_gate import TUMOR_SPECIFIC_GATE
from src.expression_context.models import CancerTypeStats, StoredPair
from tests.expression_context_helpers import (
    RELEASE_ID,
    FakeLiteratureGate,
    build_bundle,
    gene_file,
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
def test_effective_tier(fields, tier):
    stats = stats_row(**fields)
    assert stats.effective_confidence_tier == tier
    assert stats.high_confidence is (tier == "high")


def test_explicit_null_or_unknown_tier_on_a_pass_is_not_surfaced():
    gate = FakeLiteratureGate(verdicts=dict(SUPPORTING_GATE))
    bundle = build_bundle(("ERBB2",), edit_gene=tiered({"BRCA": None, "STAD": "medium"}))
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        job = run_job(client)
        assert ("ERBB2", "breast cancer") not in gate.calls
        assert ("ERBB2", "gastric cancer") not in gate.calls
        assert_agreement(surfaced_everywhere(client, job["job_id"]), set())
