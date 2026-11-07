"""Method-m3 fields: additive parsing (m2 bundles still load), gating of
broad-blocked pairs, and their display in the stats view / API.

Rules: expression_stats/docs/contract.md (method_version "3"): stats_pass ==
(pass_mode != null); pass_mode = "lineage" if the m2 lineage rule passes and
the gene is not broadly expressed, else "subgroup" if the subgroup arm passes;
lineage_blocked_by_broad = the m2 lineage rule passed and the gene is broad
(even when the subgroup arm rescues the pair).

Fixtures: tests/fixtures/expression_context/m3/ (KLK3: lineage passes in PRAD
and KICH; VIM: broadly expressed, a subgroup pass in BRCA, a broad-blocked
lineage signal in SARC, and a broad-blocked lineage rescued by the subgroup
arm in KICH). The m2 fixtures stay as they are.
"""

from __future__ import annotations

import math

import pytest
from fastapi.testclient import TestClient

from src.expression_context.bundle_store import InMemoryBundleStore
from src.expression_context.models import CancerTypeStats, GeneArtifact
from src.expression_context.service import ExpressionContextService
from tests.expression_context_helpers import (
    M3,
    M3_RELEASE_ID,
    FakeLiteratureGate,
    add_bundle_release,
    build_bundle,
    gene_file,
    make_service,
    manifest,
    seed_bundle,
)
from tests.test_expression_context_bundle import Pod

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

M3_FIELDS = (
    "pass_mode", "lineage_blocked_by_broad", "subgroup_high_count", "subgroup_high_fraction",
    "subgroup_high_fraction_ratio", "p95_log2_diff", "subgroup_q",
)
GATE_VERDICTS = {
    "prostate cancer": (False, ["11111111"]),
    "chromophobe renal cell carcinoma": (False, ["22222222"]),
    "breast cancer": (False, ["33333333"]),
    "sarcoma": (False, ["44444444"]),
}


def m3_bundle():
    return build_bundle(
        ("KLK3", "VIM"), release_id=M3_RELEASE_ID, subdir=M3,
        published_at="2026-11-07T06:00:00Z",
    )


# ------------------------------------------------------------------ parsing


def test_m3_fields_parse():
    vim = GeneArtifact.model_validate(gene_file("VIM", M3))
    assert vim.broadly_expressed is True
    brca = next(ct for ct in vim.cancer_types if ct.oncotree_code == "BRCA")
    # Subgroup pass without an m2 lineage pass: not "blocked".
    assert brca.pass_mode == "subgroup" and brca.lineage_blocked_by_broad is False
    assert brca.subgroup_high_count == 120 and brca.subgroup_q == pytest.approx(2e-8)
    assert brca.passes_stats and brca.enrichment_label == "Subgroup enriched"
    sarc = next(ct for ct in vim.cancer_types if ct.oncotree_code == "SARC")
    assert sarc.broad_blocked and not sarc.passes_stats and sarc.enrichment_label is None
    # Rescued: lineage blocked by broad expression, subgroup arm passes.
    kich = next(ct for ct in vim.cancer_types if ct.oncotree_code == "KICH")
    assert kich.lineage_blocked_by_broad is True and kich.pass_mode == "subgroup"
    assert kich.passes_stats and not kich.broad_blocked
    assert kich.enrichment_label == "Subgroup enriched"

    klk3 = GeneArtifact.model_validate(gene_file("KLK3", M3))
    assert klk3.broadly_expressed is False
    prad = klk3.cancer_types[0]
    assert prad.enrichment_label == "Lineage enriched" and not prad.broad_blocked


def test_m2_documents_without_m3_fields_still_parse():
    erbb2 = GeneArtifact.model_validate(gene_file("ERBB2"))
    assert erbb2.broadly_expressed is None
    for ct in erbb2.cancer_types:
        for field in M3_FIELDS:
            assert getattr(ct, field) is None, field
        assert not ct.broad_blocked and ct.passes_stats is ct.stats_pass
        assert ct.enrichment_label is None


def test_m3_nulls_and_unknown_values_are_tolerated():
    record = gene_file("KLK3", M3)["cancer_types"][2]  # BRCA
    nulled = {**record, "subgroup_high_count": None, "subgroup_high_fraction": None,
              "subgroup_high_fraction_ratio": None, "p95_log2_diff": None, "subgroup_q": None}
    parsed = CancerTypeStats.model_validate(nulled)  # n == 0 style: diagnostics undefined
    assert parsed.subgroup_q is None and parsed.pass_mode is None
    # An unknown future pass_mode must not reject the bundle; shown verbatim.
    future = CancerTypeStats.model_validate({**record, "stats_pass": True, "pass_mode": "both"})
    assert future.enrichment_label == "both"
    nulls = CancerTypeStats.model_validate({**record, "lineage_blocked_by_broad": None})
    assert not nulls.broad_blocked


def test_broad_blocked_rule():
    base = gene_file("VIM", M3)["cancer_types"][0]
    cases = [
        # (stats_pass, lineage_blocked_by_broad, pass_mode) -> passes_stats
        ((True, True, "subgroup"), True),  # subgroup pass despite the broad block
        ((True, True, "lineage"), False),  # malformed: blocked lineage can't pass
        ((True, True, None), False),
        ((False, True, None), False),
        ((True, False, "lineage"), True),
        ((True, None, None), True),  # m2-style record
    ]
    for (stats_pass, blocked, mode), expected in cases:
        stats = CancerTypeStats.model_validate(
            {**base, "stats_pass": stats_pass, "lineage_blocked_by_broad": blocked, "pass_mode": mode}
        )
        assert stats.passes_stats is expected, (stats_pass, blocked, mode)


# ------------------------------------------------------ fixture consistency

FLOOR = math.log2(11)
SUBGROUP_FIELDS = ("subgroup_high_count", "subgroup_high_fraction",
                   "subgroup_high_fraction_ratio", "p95_log2_diff", "subgroup_q")


def _lineage_ok(record: dict, params: dict) -> bool:
    """The m2 lineage rule (unchanged in m3)."""
    values = (record["median_log2"], record["log2fc"], record["cliffs_delta"], record["q_value"])
    if None in values:
        return False
    return (
        record["q_value"] < params["fdr"]
        and record["log2fc"] >= params["log2fc"]
        and record["cliffs_delta"] >= params["cliffs_delta"]
        and record["median_log2"] >= params["detection_log2"]
        and record["n"] >= params["min_n"]
    )


def _subgroup_ok(record: dict, params: dict) -> bool:
    """The m3 subgroup arm, all conditions (contract.md, "Subgroup arm")."""
    if any(record[field] is None for field in SUBGROUP_FIELDS):
        return False
    return (
        record["n"] >= params["min_n"] and record["n_rest"] >= params["min_n"]
        and record["subgroup_high_count"] >= params["subgroup_min_count"]
        and record["subgroup_high_fraction"] >= params["subgroup_min_fraction"]
        and record["subgroup_high_fraction_ratio"] >= params["subgroup_min_fraction_ratio"]
        and record["p95_log2_diff"] >= params["subgroup_p95_log2_diff"]
        and record["subgroup_q"] < params["subgroup_fdr"]
    )


def test_m3_fixture_params_are_the_producer_thresholds():
    params = manifest(M3)["params"]
    assert params["subgroup_min_count"] == 10
    assert params["subgroup_min_fraction"] == 0.03  # >=3% above the pan-TCGA 99th pct
    assert params["subgroup_percentile"] == 99
    assert params["subgroup_min_fraction_ratio"] == 3.0
    assert params["subgroup_p95_log2_diff"] == 1.5  # ~2.83-fold
    assert params["subgroup_fdr"] == 0.025
    assert params["detection_log2"] == params["expression_floor_log2"] == pytest.approx(FLOOR)


@pytest.mark.parametrize("symbol", ["KLK3", "VIM"])
def test_m3_fixtures_satisfy_the_rules(symbol):
    data = manifest(M3)
    params = data["params"]
    assert data["method_version"] == "3" and data["release_id"] == M3_RELEASE_ID
    doc = gene_file(symbol, M3)
    assert isinstance(doc["broadly_expressed"], bool)  # never null in m3
    broad = doc["broadly_expressed"]
    for record in doc["cancer_types"]:
        code = record["oncotree_code"]
        assert isinstance(record["lineage_blocked_by_broad"], bool), code  # never null
        lineage, subgroup = _lineage_ok(record, params), _subgroup_ok(record, params)
        # Lineage wins if both arms pass; a broad gene can't pass lineage.
        expected_mode = "lineage" if lineage and not broad else ("subgroup" if subgroup else None)
        assert record["pass_mode"] == expected_mode, code
        assert record["stats_pass"] is (expected_mode is not None), code
        # Blocked = the m2 lineage rule passed and the gene is broad, even if rescued.
        assert record["lineage_blocked_by_broad"] is (lineage and broad), code
        # Subgroup diagnostics are null only without measured samples.
        if record["n"] > 0 and record["n_rest"] > 0:
            assert all(record[field] is not None for field in SUBGROUP_FIELDS), code
            assert record["subgroup_high_fraction"] == pytest.approx(
                record["subgroup_high_count"] / record["n"], abs=1e-4), code
        if record["median_log2"] is not None and record["rest_median_log2"] is not None:
            expected = max(record["median_log2"], FLOOR) - max(record["rest_median_log2"], FLOOR)
            assert record["log2fc"] == pytest.approx(expected, abs=0.01), code
        if not record["stats_pass"]:
            assert record["literature_precheck_pass"] is None, code
        assert CancerTypeStats.model_validate(record).passes_stats is record["stats_pass"], code


def test_m3_fixtures_cover_every_branch():
    seen = set()
    for symbol in ("KLK3", "VIM"):
        doc = gene_file(symbol, M3)
        for record in doc["cancer_types"]:
            seen.add((doc["broadly_expressed"], record["pass_mode"], record["lineage_blocked_by_broad"]))
    assert {
        (False, "lineage", False),  # KLK3/PRAD, KICH
        (False, None, False),  # KLK3/BRCA
        (True, "subgroup", False),  # VIM/BRCA: subgroup only
        (True, "subgroup", True),  # VIM/KICH: rescued
        (True, None, True),  # VIM/SARC: broad-blocked, not passing
    } <= seen


# ------------------------------------------------------------- loading


async def test_m3_bundle_loads_through_the_refresher(httpx_mock):
    bundle = m3_bundle()
    add_bundle_release(httpx_mock, bundle)
    bundles = InMemoryBundleStore()
    pod = Pod(bundles)
    try:
        assert await pod.refresh() == "loaded"
    finally:
        await pod.releases.aclose()
    active = await pod.catalog.active()
    assert active.tag == bundle.tag and active.manifest.method_version == "3"
    vim = await pod.catalog.gene(active, "VIM")
    assert vim.broadly_expressed is True
    assert await pod.catalog.alias(active, "PSA") == "KLK3"


# ------------------------------------------------------------- gating


async def _service(gate=None):
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, m3_bundle())
    gate = gate or FakeLiteratureGate(verdicts=dict(GATE_VERDICTS))
    return make_service(gate=gate, bundles=bundles), gate


async def test_broad_blocked_pairs_are_never_sent_to_the_literature_gate():
    service, gate = await _service()
    result, evaluated, _, errors = await service.evaluate(await service.resolve("VIM"), None)
    assert errors == []
    # BRCA (subgroup pass) and KICH (rescued by the subgroup arm) go to the
    # gate; SARC (broad-blocked, not rescued) never does.
    assert sorted(gate.calls) == [("VIM", "breast cancer"), ("VIM", "chromophobe renal cell carcinoma")]
    assert evaluated == 2
    assert sorted(p.oncotree_code for p in result.surfaced) == ["BRCA", "KICH"]
    excluded = {p.oncotree_code: p.exclusion_reason for p in result.excluded}
    assert excluded == {"SARC": "stats", "LUAD": "stats"}
    assert result.broadly_expressed is True
    sarc = next(p for p in result.excluded if p.oncotree_code == "SARC")
    assert sarc.stats.lineage_blocked_by_broad is True and sarc.literature is None


async def test_malformed_blocked_lineage_pass_is_not_sent():
    """Defensive: stats_pass true but lineage blocked and no subgroup pass."""
    def edit(doc):
        for record in doc["cancer_types"]:
            if record["oncotree_code"] == "SARC":
                record.update(stats_pass=True, pass_mode="lineage", literature_precheck_pass=True)

    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, build_bundle(
        ("VIM",), release_id=M3_RELEASE_ID, subdir=M3, edit_gene=edit,
    ))
    gate = FakeLiteratureGate(verdicts=dict(GATE_VERDICTS))
    service = make_service(gate=gate, bundles=bundles)
    result, _, _, _ = await service.evaluate(await service.resolve("VIM"), None)
    assert ("VIM", "sarcoma") not in gate.calls
    assert {p.oncotree_code: p.exclusion_reason for p in result.excluded}["SARC"] == "stats"


async def test_stats_view_shows_m3_labels_markers_and_subgroup_stats():
    service, _ = await _service()
    await service.evaluate(await service.resolve("VIM"), None)
    view = await service.stats_view("vimentin")  # alias
    assert view.hugo_symbol == "VIM" and view.broadly_expressed is True
    pairs = {p.oncotree_code: p for p in view.pairs}
    assert pairs["BRCA"].enrichment == "Subgroup enriched" and pairs["BRCA"].stats_pass
    assert pairs["BRCA"].status == "surfaced" and pairs["BRCA"].broad_blocked is False
    assert pairs["BRCA"].stats.subgroup_high_fraction_ratio == pytest.approx(3.2)
    assert pairs["SARC"].broad_blocked and not pairs["SARC"].stats_pass
    assert pairs["SARC"].status == "excluded" and pairs["SARC"].exclusion_reason == "stats"
    assert pairs["SARC"].literature_eligible is False and pairs["SARC"].enrichment is None
    assert pairs["LUAD"].broad_blocked is False
    assert pairs["KICH"].enrichment == "Subgroup enriched" and not pairs["KICH"].broad_blocked
    assert pairs["KICH"].stats.lineage_blocked_by_broad is True and pairs["KICH"].status == "surfaced"

    klk3 = await service.stats_view("PSA")
    assert klk3.broadly_expressed is False
    assert {p.oncotree_code: p.enrichment for p in klk3.pairs} == {
        "PRAD": "Lineage enriched", "KICH": "Lineage enriched", "BRCA": None,
    }


async def test_m2_stats_view_has_no_m3_markers():
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, build_bundle())
    service = make_service(gate=FakeLiteratureGate(), bundles=bundles)
    view = await service.stats_view("ERBB2")
    assert view.broadly_expressed is None
    assert all(p.enrichment is None and not p.broad_blocked for p in view.pairs)


async def test_stored_rows_keep_the_m3_snapshot_and_gene_marker():
    service, _ = await _service()
    await service.evaluate(await service.resolve("VIM"), "alice@example.org")
    row = service.store.rows[("VIM", "SARC", M3_RELEASE_ID, FakeLiteratureGate.name)]
    assert row.stats["lineage_blocked_by_broad"] is True
    assert row.stats["gene_broadly_expressed"] is True
    assert row.stats["subgroup_q"] == pytest.approx(0.2)
    latest = await service.latest_result("VIM", include_excluded=True)
    assert latest.broadly_expressed is True
    rows = list(service.store.rows.values())
    assert ExpressionContextService._gene_result("VIM", rows, True).broadly_expressed is True


def test_api_exposes_m3_fields():
    from tests.test_expression_context_api import PREFIX, Harness

    harness = Harness(gate=FakeLiteratureGate(verdicts=dict(GATE_VERDICTS)), bundle=m3_bundle())
    with TestClient(harness.app) as client:
        body = client.get(f"{PREFIX}/stats/VIM").json()
    assert body["broadly_expressed"] is True
    pairs = {p["oncotree_code"]: p for p in body["pairs"]}
    assert pairs["SARC"]["broad_blocked"] is True and pairs["SARC"]["stats_pass"] is False
    assert pairs["BRCA"]["enrichment"] == "Subgroup enriched"
    assert pairs["BRCA"]["stats"]["pass_mode"] == "subgroup"
    for field in M3_FIELDS:
        assert field in pairs["BRCA"]["stats"], field
