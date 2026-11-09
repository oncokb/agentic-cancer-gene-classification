"""Producer nulls (method_version 2): parse success and gating semantics.

Fixture genes/MYCN.json mirrors what the producer emits:

- BRCA: n == 0 -> every statistic null, stats_pass false, pre-check null
- STAD: stats_pass true, pre-check null (not run)  -> goes to literature gate
- ESCA: stats_pass true, pre-check explicitly false -> excluded "precheck"
- UCEC: stats_pass false, pre-check null           -> excluded "stats"
- LUAD: stats_pass true, pre-check true            -> goes to literature gate
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.expression_context.catalog import StatsCatalog
from src.expression_context.models import CancerTypeStats, GeneArtifact, StoredPair
from src.expression_context.service import ExpressionContextService
from tests.expression_context_helpers import (
    RELEASE_ID,
    FakeLiteratureGate,
    gene_file,
    load_fixture,
    seeded_bundles,
)
from tests.test_expression_context_api import PREFIX, Harness, run_job

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

NULL_STATS = (
    "median_log2", "p90_log2", "log2fc", "log2fc_vs_median_of_type_medians",
    "cliffs_delta", "auc", "p_value", "q_value",
)


def _by_code(pairs):
    return {pair["oncotree_code"]: pair for pair in pairs}


def test_gene_file_with_nulls_parses():
    parsed = GeneArtifact.model_validate(gene_file("MYCN"))
    rows = {ct.oncotree_code: ct for ct in parsed.cancer_types}
    brca = rows["BRCA"]
    assert brca.n == 0 and brca.stats_pass is False
    for field in NULL_STATS:
        assert getattr(brca, field) is None, field
    assert brca.rest_median_log2 == 5.2 and brca.rest_p90_log2 == 7.9
    assert brca.pubmed_count is None and brca.literature_precheck_pass is None
    assert rows["STAD"].literature_precheck_pass is None
    assert rows["ESCA"].literature_precheck_pass is False and rows["ESCA"].pubmed_count == 0
    assert rows["STAD"].p90_log2 == 11.2 and rows["STAD"].rest_p90_log2 == 7.1


def test_missing_precheck_fields_default_to_not_run():
    minimal = {"oncotree_code": "GBM", "n": 0, "n_rest": 10, "stats_pass": False}
    stats = CancerTypeStats.model_validate(minimal)
    assert stats.literature_precheck_pass is None and stats.pubmed_count is None
    assert stats.q_value is None and stats.flags == []


async def test_bundle_gene_with_nulls_parses_from_the_store():
    catalog = StatsCatalog(seeded_bundles(), cache_seconds=0)
    bundle = await catalog.active()
    parsed = await catalog.gene(bundle, "MYCN")
    assert parsed.gene.hugo_symbol == "MYCN"
    assert parsed.cancer_types[0].q_value is None


def test_gating_semantics_with_nulls():
    gate = FakeLiteratureGate({
        "gastric cancer": (False, ["11111111"]),
        "lung adenocarcinoma": (False, ["22222222"]),
    })
    harness = Harness(gate=gate)
    with TestClient(harness.app) as client:
        job = run_job(client, "MYCN")
        assert job["status"] == "succeeded", job
        stored_view = client.get(f"{PREFIX}/genes/MYCN", params={"include_excluded": "true"})

    # Null pre-check is "not run" -> still allowed through to the gate;
    # explicit false is not.
    assert sorted(q for _, q in gate.calls) == ["gastric cancer", "lung adenocarcinoma"]
    result = job["result"]
    assert [p["oncotree_code"] for p in result["surfaced"]] == ["STAD", "LUAD"]  # by q-value (1e-29 < 1e-19)
    excluded = _by_code(result["excluded"])
    assert {code: p["exclusion_reason"] for code, p in excluded.items()} == {
        "BRCA": "stats",     # n == 0, all statistics null
        "UCEC": "stats",     # stats_pass false, pre-check null
        "ESCA": "precheck",  # pre-check explicitly false
    }
    # Nulls and the new p90 fields serialize and survive the store round trip.
    brca = excluded["BRCA"]["stats"]
    assert all(brca[field] is None for field in NULL_STATS)
    assert brca["literature_precheck_pass"] is None and brca["pubmed_count"] is None
    stad = _by_code(result["surfaced"])["STAD"]["stats"]
    assert stad["p90_log2"] == 11.2 and stad["rest_p90_log2"] == 7.1
    assert stad["literature_precheck_pass"] is None
    stored_row = harness.store.rows[("MYCN", "BRCA", RELEASE_ID, "acgc_tumor_specific_v2")]
    assert stored_row.stats["q_value"] is None and stored_row.stats["p90_log2"] is None

    assert stored_view.status_code == 200
    view = stored_view.json()
    assert [p["oncotree_code"] for p in view["surfaced"]] == ["STAD", "LUAD"]
    assert _by_code(view["excluded"])["BRCA"]["stats"]["q_value"] is None


def test_surfaced_sorting_tolerates_null_q_values():
    base = gene_file("MYCN")["cancer_types"][1]
    now = datetime.now(timezone.utc)
    evidence = {
        "literature_gate": "acgc_tumor_specific_v2", "tumor_type_query": "breast cancer",
        "supported": True, "insufficient_evidence": False, "pmids": ["1"],
        "verdict": "supported", "papers": [{"pmid": "1", "tumor_specific": True}],
        "evaluated_at": now.isoformat(),
    }
    rows = [
        StoredPair(
            hugo_symbol="MYCN", oncotree_code=code, release_id=RELEASE_ID,
            literature_gate="acgc_tumor_specific_v2", surfaced=True,
            stats={**base, "oncotree_code": code, "q_value": q}, evidence=evidence,
            computed_at=now,
        )
        for code, q in (("STAD", None), ("LUAD", 1e-3), ("BRCA", 1e-9), ("GBM", None))
    ]
    result = ExpressionContextService._gene_result("MYCN", rows, include_excluded=True)
    assert [p.oncotree_code for p in result.surfaced] == ["BRCA", "LUAD", "GBM", "STAD"]


def test_stored_row_without_evidence_is_never_surfaced():
    base = gene_file("MYCN")["cancer_types"][1]
    now = datetime.now(timezone.utc)
    rows = [
        StoredPair(
            hugo_symbol="MYCN", oncotree_code=code, release_id=RELEASE_ID,
            literature_gate="acgc_tumor_specific_v2", surfaced=True,
            stats={**base, "oncotree_code": code}, computed_at=now,
        )
        for code in ("STAD", "LUAD")
    ]
    result = ExpressionContextService._gene_result("MYCN", rows, include_excluded=True)
    assert result.surfaced == []
    assert {p.oncotree_code for p in result.excluded} == {"STAD", "LUAD"}
    assert all(p.exclusion_reason == "literature" for p in result.excluded)
    assert all(p.literature is None for p in result.excluded)


def test_stored_verdict_from_another_gate_is_never_surfaced():
    base = gene_file("MYCN")["cancer_types"][1]
    now = datetime.now(timezone.utc)
    evidence = {
        "literature_gate": "acgc_pipeline_v1", "tumor_type_query": "breast cancer",
        "supported": True, "insufficient_evidence": False, "pmids": ["1"],
        "verdict": "supported", "papers": [{"pmid": "1", "tumor_specific": True}],
        "evaluated_at": now.isoformat(),
    }
    row = StoredPair(
        hugo_symbol="MYCN", oncotree_code="STAD", release_id=RELEASE_ID,
        literature_gate="acgc_tumor_specific_v2", surfaced=True,
        stats={**base, "oncotree_code": "STAD"}, evidence=evidence, computed_at=now,
    )
    result = ExpressionContextService._gene_result("MYCN", [row], include_excluded=True)
    assert result.surfaced == []
    assert result.excluded[0].exclusion_reason == "literature"
    assert result.excluded[0].literature is not None
    assert result.excluded[0].literature.supported is False


def test_matching_v1_row_and_evidence_is_never_surfaced():
    """A legacy row whose evidence is from the same legacy gate is still not
    a v2 row: only acgc_tumor_specific_v2 may surface."""
    base = gene_file("MYCN")["cancer_types"][1]
    now = datetime.now(timezone.utc)
    evidence = {
        "literature_gate": "acgc_pipeline_v1", "tumor_type_query": "breast cancer",
        "supported": True, "insufficient_evidence": False, "pmids": ["1"],
        "evaluated_at": now.isoformat(),
    }
    row = StoredPair(
        hugo_symbol="MYCN", oncotree_code="STAD", release_id=RELEASE_ID,
        literature_gate="acgc_pipeline_v1", surfaced=True,
        stats={**base, "oncotree_code": "STAD"}, evidence=evidence, computed_at=now,
    )
    result = ExpressionContextService._gene_result("MYCN", [row], include_excluded=True)
    assert result.surfaced == []
    assert result.excluded[0].exclusion_reason == "literature"
    assert result.excluded[0].literature is not None
    assert result.excluded[0].literature.supported is False


FLOOR = math.log2(11)  # method_version 2: RSEM 10


def _v2_stats_pass(record: dict, params: dict) -> bool:
    """Recompute the method_version 2 stats gate from a record's own fields."""
    median, rest = record["median_log2"], record["rest_median_log2"]
    if None in (median, rest, record["cliffs_delta"], record["q_value"]):
        return False
    return (
        record["q_value"] < params["fdr"]
        and record["log2fc"] >= params["log2fc"]
        and record["cliffs_delta"] >= params["cliffs_delta"]
        and median >= params["detection_log2"]
        and record["n"] >= params["min_n"]
    )


@pytest.mark.parametrize("symbol", ["ERBB2", "MYCN"])
def test_fixtures_are_consistent_with_method_v2(symbol):
    """Fixture records must be what the v2 producer would emit: stats_pass
    matches the gates, log2fc is the floor-clamped median difference, and a
    pre-check is only recorded for stats-passing pairs."""
    params = load_fixture("manifest.json")["params"]
    assert load_fixture("manifest.json")["method_version"] == "2"
    assert params["expression_floor_log2"] == pytest.approx(FLOOR)
    assert params["detection_log2"] == params["expression_floor_log2"]
    assert "floor_log2" not in params  # producer name is expression_floor_log2
    for record in gene_file(symbol)["cancer_types"]:
        code = record["oncotree_code"]
        assert record["stats_pass"] is _v2_stats_pass(record, params), code
        if record["median_log2"] is not None and record["rest_median_log2"] is not None:
            expected = max(record["median_log2"], FLOOR) - max(record["rest_median_log2"], FLOOR)
            assert record["log2fc"] == pytest.approx(expected, abs=0.01), code
        if not record["stats_pass"]:
            assert record["literature_precheck_pass"] in (None, False), code


RELEASE_ID_FORMAT = re.compile(r"datahub-[0-9a-f]{12}-m\d+-[0-9a-f]{12}\Z")
BUNDLE_CONTRACT_LINK = "](../expression_stats/docs/contract.md)"
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fixture_release_ids_use_producer_format():
    manifest = load_fixture("manifest.json")
    assert RELEASE_ID_FORMAT.fullmatch(manifest["release_id"])
    assert manifest["release_id"].split("-")[2] == f"m{manifest['method_version']}"
    for symbol in ("ERBB2", "MYCN"):
        assert gene_file(symbol)["release_id"] == manifest["release_id"]


def test_fixture_bundle_tag_format():
    from tests.expression_context_helpers import build_bundle

    bundle = build_bundle()
    assert bundle.tag == f"expression-stats-{bundle.release_id}-pubmed-20261102"
    assert bundle.manifest["bundle_tag"] == bundle.tag
    assert bundle.manifest["pubmed_snapshot_date"] == "2026-11-02"


def test_design_doc_defers_to_the_bundle_contract():
    """The bundle contract lives in expression_stats/docs/contract.md only: the
    design doc links it by relative path, states the supported versions and
    the consumer semantics, and carries no copy of its examples or method."""
    from src.expression_context.models import SUPPORTED_METHOD_VERSIONS, SUPPORTED_SCHEMA_VERSION

    doc = (REPO_ROOT / "docs/expression-context.md").read_text()
    assert BUNDLE_CONTRACT_LINK in doc
    assert f'| `schema_version` | `"{SUPPORTED_SCHEMA_VERSION}"`' in doc
    versions = ", ".join(f'`"{v}"`' for v in SUPPORTED_METHOD_VERSIONS)
    assert f"| `method_version` | {versions} " in doc
    assert "## Consumer semantics" in doc and "## Release bundle transport" in doc
    assert "datahub-<12hex>-m<method>-<build_sha12>" in doc
    assert "expression-stats-<release_id>-pubmed-<YYYYMMDD>" in doc
    assert "```json" not in doc  # no duplicated bundle examples
    assert "## Statistics" not in doc and "Mann-Whitney" not in doc
    assert "floor_log2" not in doc.replace("expression_floor_log2", "")
    # The old transport is gone.
    for gone in ("ARTIFACT_BASE_URL", "gene-expression-relevance", "GitHub Pages", "v1/genes/"):
        assert gone not in doc, gone
    for section in ("## Removability requirements", "## ACGC API and page", "## Tables", "## Settings"):
        assert section in doc


def test_bundle_contract_names_the_fields_this_module_reads():
    """Runs once the producer PR lands expression_stats/docs/contract.md."""
    contract = REPO_ROOT / "expression_stats/docs/contract.md"
    if not contract.exists():
        pytest.skip("expression_stats/docs/contract.md lands with the producer PR")
    text = contract.read_text()
    for field in ("bundle_tag", "pubmed_snapshot_date", "gene_count", "sha256", "size_bytes",
                  "genes.jsonl.gz", "aliases.json", "expression-stats-",
                  # method m3 fields this module reads
                  "broadly_expressed", "pass_mode", "lineage_blocked_by_broad",
                  "subgroup_high_count", "subgroup_high_fraction",
                  "subgroup_high_fraction_ratio", "p95_log2_diff", "subgroup_q"):
        assert field in text, field
