"""Producer nulls (method_version 2): parse success and gating semantics.

Fixture genes/MYCN.json mirrors what the producer emits:

- BRCA: n == 0 -> every statistic null, stats_pass false, pre-check null
- STAD: stats_pass true, pre-check null (not run)  -> goes to literature gate
- ESCA: stats_pass true, pre-check explicitly false -> excluded "precheck"
- UCEC: stats_pass false, pre-check null           -> excluded "stats"
- LUAD: stats_pass true, pre-check true            -> goes to literature gate
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.expression_context.artifact_client import ArtifactClient
from src.expression_context.models import CancerTypeStats, GeneArtifact, StoredPair
from src.expression_context.service import ExpressionContextService
from src.expression_context.settings import ExpressionContextSettings
from tests.expression_context_helpers import (
    BASE_URL,
    RELEASE_ID,
    FakeLiteratureGate,
    add_artifacts,
    gene_file,
    load_fixture,
    module_redis,
    url,
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


async def test_artifact_client_parses_null_gene_file(httpx_mock, fake_redis):
    settings = ExpressionContextSettings(enabled=True, artifact_base_url=BASE_URL)
    client = ArtifactClient(settings, redis=module_redis(fake_redis))
    httpx_mock.add_response(url=url("genes/MYCN.json"), json=gene_file("MYCN"))
    try:
        parsed = await client.get_gene("MYCN", RELEASE_ID)
        assert parsed.gene.hugo_symbol == "MYCN"
        assert parsed.cancer_types[0].q_value is None
    finally:
        await client.aclose()


def test_gating_semantics_with_nulls(httpx_mock):
    add_artifacts(httpx_mock, genes=("ERBB2", "MYCN"))
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
    stored_row = harness.store.rows[("MYCN", "BRCA", RELEASE_ID, "acgc_pipeline_v1")]
    assert stored_row.stats["q_value"] is None and stored_row.stats["p90_log2"] is None

    assert stored_view.status_code == 200
    view = stored_view.json()
    assert [p["oncotree_code"] for p in view["surfaced"]] == ["STAD", "LUAD"]
    assert _by_code(view["excluded"])["BRCA"]["stats"]["q_value"] is None


def test_surfaced_sorting_tolerates_null_q_values():
    base = gene_file("MYCN")["cancer_types"][1]
    now = datetime.now(timezone.utc)
    rows = [
        StoredPair(
            hugo_symbol="MYCN", oncotree_code=code, release_id=RELEASE_ID,
            literature_gate="acgc_pipeline_v1", surfaced=True,
            stats={**base, "oncotree_code": code, "q_value": q}, computed_at=now,
        )
        for code, q in (("STAD", None), ("LUAD", 1e-3), ("BRCA", 1e-9), ("GBM", None))
    ]
    result = ExpressionContextService._gene_result("MYCN", rows, include_excluded=True)
    assert [p.oncotree_code for p in result.surfaced] == ["BRCA", "LUAD", "GBM", "STAD"]


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
    assert params["floor_log2"] == pytest.approx(FLOOR)
    for record in gene_file(symbol)["cancer_types"]:
        code = record["oncotree_code"]
        assert record["stats_pass"] is _v2_stats_pass(record, params), code
        if record["median_log2"] is not None and record["rest_median_log2"] is not None:
            expected = max(record["median_log2"], FLOOR) - max(record["rest_median_log2"], FLOOR)
            assert record["log2fc"] == pytest.approx(expected, abs=0.01), code
        if not record["stats_pass"]:
            assert record["literature_precheck_pass"] in (None, False), code


def test_contract_doc_example_satisfies_method_v2():
    doc = (Path(__file__).resolve().parents[1] / "docs/expression-context.md").read_text()
    blocks = [b.split("```", 1)[0] for b in doc.split("```json")[1:]]
    gene_example = json.loads(next(b for b in blocks if '"gene":' in b).replace("…", "x"))
    manifest_example = json.loads(next(b for b in blocks if '"params"' in b))
    record = gene_example["cancer_types"][0]
    assert manifest_example["method_version"] == "2"
    assert record["stats_pass"] is True and _v2_stats_pass(record, manifest_example["params"])
    expected = max(record["median_log2"], FLOOR) - max(record["rest_median_log2"], FLOOR)
    assert record["log2fc"] == pytest.approx(expected, abs=0.01)
    CancerTypeStats.model_validate(record)
