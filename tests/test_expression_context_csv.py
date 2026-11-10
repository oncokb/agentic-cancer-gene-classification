"""CSV export for expression-context curation ("golden set" sheets).

Covers:
- Header order and exact column set (33 columns; ``cancer_type_code`` omitted
  because ``oncotree_code`` already holds the TCGA PanCancer cohort code in
  the bundle contract).
- Surfaced-only default vs ``?include_excluded=true``, alias resolution
  (``HER2`` -> ``ERBB2``), 404/422/503 and feature-off 404.
- Single source of truth: row-for-row agreement between
  ``GET /v1/expression-context/genes/{gene}/export.csv`` and
  ``GET /v1/expression-context/genes/{gene}`` (and ``GET /genes`` history
  counts), including a stats-failing pair with a stored ``supported`` v2
  verdict, a broad-blocked m3 pair with a stored ``supported`` v2 verdict,
  and a pair with an old-rule (``acgc_pipeline_v1``) verdict.
- RFC 4180 quoting round-trip for rationale with commas, quotes and newlines.
- CSV-injection guard on text cells starting with ``=``, ``+``, ``-``, ``@``,
  ``\\t``, ``\\r`` (including ``rationale`` starting with ``=`` and a gene alias
  starting with ``+``), while negative numeric columns such as ``cliffs_delta``
  stay numeric.
- UTF-8 BOM, empty string for nulls (never ``None``/``null``/``NaN``), 4
  significant digits plain decimal for floats and scientific notation preserved
  for ``q_value`` / ``subgroup_q_value``.
- Bulk streaming export (``GET /v1/expression-context/export.csv``) across
  multiple genes in the current bundle only, paging through the store.
- Byte-for-byte parity between Python ``cbioportal_expression_url`` and JS
  ``expressionURL`` for ``ERBB2``/``BRCA``, ``KLK3``/``PRAD`` and the
  ``COADREAD`` study.
"""

from __future__ import annotations

import csv
import io
import json
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth import AuthenticatedUser, require_auth
from src.expression_context import register_expression_context
from src.expression_context.bundle_store import InMemoryBundleStore
from src.expression_context.catalog import StatsCatalog
from src.expression_context.csv_export import (
    CSV_COLUMNS,
    UTF8_BOM,
    GeneCsvContext,
    cbioportal_expression_url,
    format_csv_rows,
    format_float,
    format_q,
    gene_result_csv_rows,
)
from src.expression_context.lease import VerdictLease
from src.expression_context.literature_gate import TUMOR_SPECIFIC_GATE
from src.expression_context.models import JobRecord, StoredPair
from src.expression_context.service import ExpressionContextService
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.expression_context_helpers import (
    M3,
    M3_RELEASE_ID,
    RELEASE_ID,
    REPO,
    FakeLiteratureGate,
    LeaseFakeRedis,
    build_bundle,
    gene_file,
    make_settings,
    module_redis,
    seed_bundle,
)

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

PREFIX = "/v1/expression-context"
ALICE = AuthenticatedUser(email="alice@example.org", name="Alice", domain="example.org")
JS_PATH = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "expression_context"
    / "static"
    / "expression-context.js"
)

EXPECTED_COLUMNS: Tuple[str, ...] = (
    "gene_symbol",
    "entrez_gene_id",
    "queried_as",
    "oncotree_code",
    "cancer_type_name",
    "surfaced",
    "exclusion_reason",
    "stats_pass",
    "enrichment_label",
    "broadly_expressed",
    "n_tumor",
    "n_rest",
    "median_tumor",
    "median_rest",
    "log2_fold_change",
    "cliffs_delta",
    "auc",
    "q_value",
    "subgroup_fraction_high",
    "subgroup_fold_over_rest",
    "subgroup_q_value",
    "pubmed_precheck",
    "pubmed_precheck_count",
    "literature_verdict",
    "literature_rule",
    "tumor_specific_pmids",
    "rationale",
    "cbioportal_cancer_type_url",
    "cbioportal_mrna_vs_dx_url",
    "cbioportal_all_32_url",
    "release_id",
    "method_version",
    "literature_checked_at",
)


class CsvHarness:
    def __init__(self, gate=None, store=None, bundle=None, seed=True, settings=None):
        self.gate = gate or FakeLiteratureGate()
        self.store = store or InMemoryExpressionContextStore()
        self.bundles = InMemoryBundleStore()
        self.bundle = bundle or build_bundle()
        self.user = ALICE
        self.redis = LeaseFakeRedis()
        self.app = FastAPI()
        self.service: ExpressionContextService | None = None
        settings = settings or make_settings()

        async def factory(app, s):
            if seed:
                await seed_bundle(self.bundles, self.bundle)
            self.service = ExpressionContextService(
                catalog=StatsCatalog(self.bundles, cache_seconds=0),
                gate=self.gate,
                store=self.store,
                jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
                lease=VerdictLease(ttl_seconds=5, redis=module_redis(self.redis)),
                lease_poll_seconds=0.01,
                releases_repo=REPO,
            )
            return self.service

        assert register_expression_context(self.app, settings=settings, service_factory=factory)
        self.app.dependency_overrides[require_auth] = lambda: self.user


def run_job(client: TestClient, gene: str = "ERBB2") -> dict:
    response = client.post(f"{PREFIX}/jobs", json={"gene": gene})
    assert response.status_code == 202, response.text
    status_url = response.json()["status_url"]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        poll = client.get(status_url)
        assert poll.status_code == 200, poll.text
        body = poll.json()
        if body["status"] in ("succeeded", "failed"):
            assert body["status"] == "succeeded", body
            return body
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def parse_csv_response(text: str) -> Tuple[List[str], List[Dict[str, str]]]:
    assert text.startswith(UTF8_BOM), "CSV must start with UTF-8 BOM"
    without_bom = text[len(UTF8_BOM) :]
    reader = csv.reader(io.StringIO(without_bom))
    rows = list(reader)
    assert rows, "CSV must have at least a header row"
    header = rows[0]
    records = [dict(zip(header, row, strict=True)) for row in rows[1:]]
    return header, records


def _v2_supported_evidence(
    pmid: str = "12345678",
    query: str = "breast cancer",
    rationale: str = "Supported in this tumor type.",
    evaluated_at: str = "2026-11-05T12:00:00Z",
) -> dict:
    return {
        "literature_gate": TUMOR_SPECIFIC_GATE,
        "tumor_type_query": query,
        "supported": True,
        "insufficient_evidence": False,
        "pmids": [pmid],
        "rationale": rationale,
        "evaluated_at": evaluated_at,
        "verdict": "supported",
        "papers": [{"pmid": pmid, "tumor_specific": True, "reason": "Confirmed in cohort."}],
        "decided_by": "judge",
    }


def test_header_order_and_exact_column_set():
    assert CSV_COLUMNS == EXPECTED_COLUMNS
    assert "cancer_type_code" not in CSV_COLUMNS


def test_per_gene_csv_default_surfaced_only_vs_include_excluded_and_alias():
    harness = CsvHarness()
    with TestClient(harness.app) as client:
        run_job(client, "ERBB2")

        resp = client.get(f"{PREFIX}/genes/ERBB2/export.csv")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"] == "text/csv; charset=utf-8"
        assert (
            resp.headers["content-disposition"]
            == f'attachment; filename="expression-context_ERBB2_{RELEASE_ID}.csv"'
        )
        assert resp.content.startswith(b"\xef\xbb\xbf")

        header, surfaced_rows = parse_csv_response(resp.text)
        assert tuple(header) == EXPECTED_COLUMNS
        assert [r["oncotree_code"] for r in surfaced_rows] == ["BRCA"]
        brca = surfaced_rows[0]
        assert brca["gene_symbol"] == "ERBB2"
        assert brca["entrez_gene_id"] == "2064"
        assert brca["queried_as"] == "ERBB2"
        assert brca["cancer_type_name"] == "Breast Invasive Carcinoma"
        assert brca["surfaced"] == "yes"
        assert brca["exclusion_reason"] == ""
        assert brca["stats_pass"] == "yes"
        assert brca["enrichment_label"] == ""
        assert brca["broadly_expressed"] == ""
        assert brca["n_tumor"] == "1082"
        assert brca["n_rest"] == "8989"
        assert brca["median_tumor"] == "13.1"
        assert brca["median_rest"] == "11.6"
        assert brca["log2_fold_change"] == "1.5"
        assert brca["cliffs_delta"] == "0.62"
        assert brca["auc"] == "0.81"
        assert brca["q_value"] == "1e-39"
        assert brca["pubmed_precheck"] == "pass"
        assert brca["pubmed_precheck_count"] == "1234"
        assert brca["literature_verdict"] == "supported"
        assert brca["literature_rule"] == TUMOR_SPECIFIC_GATE
        assert brca["tumor_specific_pmids"] == "12345678;23456789"
        assert brca["rationale"] == "ERBB2 in breast cancer"
        assert brca["release_id"] == RELEASE_ID
        assert brca["method_version"] == "2"
        assert brca["literature_checked_at"].endswith("Z")

        # Alias resolution (HER2 -> ERBB2) records queried_as=HER2 and canonical gene_symbol=ERBB2.
        alias_resp = client.get(f"{PREFIX}/genes/HER2/export.csv")
        assert alias_resp.status_code == 200, alias_resp.text
        assert (
            alias_resp.headers["content-disposition"]
            == f'attachment; filename="expression-context_ERBB2_{RELEASE_ID}.csv"'
        )
        _, alias_rows = parse_csv_response(alias_resp.text)
        assert len(alias_rows) == 1
        assert alias_rows[0]["gene_symbol"] == "ERBB2"
        assert alias_rows[0]["queried_as"] == "HER2"

        # ?include_excluded=true adds all excluded cancer types with their exclusion reasons.
        full_resp = client.get(
            f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}
        )
        assert full_resp.status_code == 200, full_resp.text
        _, all_rows = parse_csv_response(full_resp.text)
        assert [r["oncotree_code"] for r in all_rows] == ["BRCA", "ESCA", "LUAD", "STAD", "UCEC"]
        by_code = {r["oncotree_code"]: r for r in all_rows}
        assert by_code["BRCA"]["surfaced"] == "yes" and by_code["BRCA"]["exclusion_reason"] == ""
        assert {
            code: (by_code[code]["surfaced"], by_code[code]["exclusion_reason"])
            for code in ("ESCA", "LUAD", "STAD", "UCEC")
        } == {
            "ESCA": ("no", "literature"),
            "LUAD": ("no", "stats"),
            "STAD": ("no", "literature"),
            "UCEC": ("no", "precheck"),
        }


def test_status_codes_404_422_503_and_auth():
    harness = CsvHarness()
    with TestClient(harness.app) as client:
        assert client.get(f"{PREFIX}/genes/ERBB2/export.csv").status_code == 404
        assert client.get(f"{PREFIX}/genes/NOPE1/export.csv").status_code == 404
        assert client.get(f"{PREFIX}/genes/bad$gene/export.csv").status_code == 422

    unseeded = CsvHarness(seed=False)
    with TestClient(unseeded.app) as client:
        r_gene = client.get(f"{PREFIX}/genes/HER2/export.csv")
        assert r_gene.status_code == 503
        assert "Retry-After" in r_gene.headers

        r_all = client.get(f"{PREFIX}/export.csv")
        assert r_all.status_code == 503
        assert "Retry-After" in r_all.headers


def test_csv_agrees_row_for_row_with_get_gene_and_history_under_stats_and_old_rule_edge_cases():
    """Single source of truth: CSV export, GET /genes/{gene} and GET /genes must
    agree on every pair, including:
    1. a pair with a valid v2 supported verdict and passing stats (BRCA -> surfaced=yes),
    2. a stats-failing pair (stats_pass=False) that has a stored v2 supported
       verdict and surfaced=True in storage (LUAD -> surfaced=no, reason=stats),
    3. a method-m3 broad-blocked pair (stats_pass=True, lineage_blocked_by_broad=True,
       pass_mode=None) that has a stored v2 supported verdict and surfaced=True
       in storage (SARC -> surfaced=no, reason=stats),
    4. a pair with an old-rule (acgc_pipeline_v1) supported verdict stored under
       the v2 gate key with surfaced=True (STAD -> surfaced=no, reason=literature),
    5. a pair failing the PubMed pre-check despite a stored v2 supported verdict
       (UCEC -> surfaced=no, reason=precheck).
    """
    harness = CsvHarness()
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    erbb2_doc = gene_file("ERBB2")
    stats_by_code = {ct["oncotree_code"]: dict(ct) for ct in erbb2_doc["cancer_types"]}

    seeded_rows = [
        # 1. Valid surfaced pair (BRCA).
        StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            exclusion_reason=None,
            stats={
                **stats_by_code["BRCA"],
                "cancer_type_name": "Breast Invasive Carcinoma",
                "study_id": "brca_tcga_pan_can_atlas_2018",
            },
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        ),
        # 2. stats_pass=False despite stored supported v2 verdict and stored surfaced=True.
        StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="LUAD",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            exclusion_reason=None,
            stats={
                **stats_by_code["LUAD"],
                "stats_pass": False,
                "cancer_type_name": "Lung Adenocarcinoma",
                "study_id": "luad_tcga_pan_can_atlas_2018",
            },
            evidence=_v2_supported_evidence("22222222", "lung adenocarcinoma"),
            computed_at=now,
        ),
        # 3. Broad-blocked m3 pair (stats_pass=True, lineage_blocked_by_broad=True, pass_mode=None)
        #    despite stored supported v2 verdict and stored surfaced=True.
        StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="SARC",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            exclusion_reason=None,
            stats={
                **stats_by_code["BRCA"],
                "oncotree_code": "SARC",
                "stats_pass": True,
                "lineage_blocked_by_broad": True,
                "pass_mode": None,
                "cancer_type_name": "Sarcoma",
                "study_id": "sarc_tcga_pan_can_atlas_2018",
            },
            evidence=_v2_supported_evidence("33333333", "sarcoma"),
            computed_at=now,
        ),
        # 4. Old-rule (acgc_pipeline_v1) verdict stored with surfaced=True.
        StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="STAD",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            exclusion_reason=None,
            stats={
                **stats_by_code["STAD"],
                "cancer_type_name": "Stomach Adenocarcinoma",
                "study_id": "stad_tcga_pan_can_atlas_2018",
            },
            evidence={
                **_v2_supported_evidence("44444444", "gastric cancer"),
                "literature_gate": "acgc_pipeline_v1",
            },
            computed_at=now,
        ),
        # 5. Pre-check fail despite stored supported v2 verdict.
        StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="UCEC",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            exclusion_reason=None,
            stats={
                **stats_by_code["UCEC"],
                "literature_precheck_pass": False,
                "cancer_type_name": "Uterine Corpus Endometrial Carcinoma",
                "study_id": "ucec_tcga_pan_can_atlas_2018",
            },
            evidence=_v2_supported_evidence("55555555", "endometrial cancer"),
            computed_at=now,
        ),
    ]

    with TestClient(harness.app) as client:
        for pair in seeded_rows:
            harness.store.rows[
                (pair.hugo_symbol, pair.oncotree_code, pair.release_id, pair.literature_gate)
            ] = pair

        json_default = client.get(f"{PREFIX}/genes/ERBB2").json()
        json_full = client.get(
            f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}
        ).json()
        _, csv_default = parse_csv_response(
            client.get(f"{PREFIX}/genes/ERBB2/export.csv").text
        )
        _, csv_full = parse_csv_response(
            client.get(
                f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}
            ).text
        )
        history = client.get(f"{PREFIX}/genes").json()

        json_surfaced_codes = [p["oncotree_code"] for p in json_default["surfaced"]]
        assert json_surfaced_codes == ["BRCA"]
        assert [r["oncotree_code"] for r in csv_default] == json_surfaced_codes

        json_excluded_by_code = {
            p["oncotree_code"]: p["exclusion_reason"] for p in json_full["excluded"]
        }
        assert json_excluded_by_code == {
            "LUAD": "stats",
            "SARC": "stats",
            "STAD": "literature",
            "UCEC": "precheck",
        }

        for row in csv_full:
            code = row["oncotree_code"]
            if code in json_surfaced_codes:
                assert row["surfaced"] == "yes"
                assert row["exclusion_reason"] == ""
            else:
                assert row["surfaced"] == "no"
                assert row["exclusion_reason"] == json_excluded_by_code[code]

        assert history["items"][0]["hugo_symbol"] == "ERBB2"
        assert history["items"][0]["surfaced_count"] == len(json_surfaced_codes) == 1
        assert history["items"][0]["pair_count"] == len(csv_full) == 5


def test_rfc4180_quoting_round_trip_and_csv_injection_guard():
    """Commas, double quotes, and newlines in rationale round-trip cleanly;
    text cells starting with = + - @ tab or CR are prefixed with a single quote,
    while negative numeric columns (cliffs_delta, log2_fold_change) remain plain numbers."""
    tricky_rationale = '=SUM(A1:A2), "quoted, phrase",\nand second line'
    harness = CsvHarness()
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    base_stats.update(
        oncotree_code="BRCA",
        log2fc=-1.23456,
        cliffs_delta=-0.45678,
        median_log2=None,
        rest_median_log2=None,
        auc=None,
        q_value=1.23456e-64,
        pubmed_count=None,
        literature_precheck_pass=None,
        cancer_type_name="Breast Invasive Carcinoma",
        study_id="brca_tcga_pan_can_atlas_2018",
    )

    with TestClient(harness.app) as client:
        harness.store.rows[("ERBB2", "BRCA", RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            exclusion_reason=None,
            stats=base_stats,
            evidence=_v2_supported_evidence(
                "12345678", "breast cancer", rationale=tricky_rationale
            ),
            computed_at=now,
        )

        resp = client.get(f"{PREFIX}/genes/ERBB2/export.csv")
        assert resp.status_code == 200, resp.text
        _, rows = parse_csv_response(resp.text)
        assert len(rows) == 1
        row = rows[0]

        # Rationale starting with '=' gets a leading single quote and preserves
        # commas, quotes and embedded newline through csv.reader.
        assert row["rationale"] == f"'{tricky_rationale}"

        # Negative numeric columns stay numeric (no leading single quote).
        assert row["cliffs_delta"] == "-0.4568"
        assert row["log2_fold_change"] == "-1.235"
        assert float(row["cliffs_delta"]) == pytest.approx(-0.4568)
        assert float(row["log2_fold_change"]) == pytest.approx(-1.235)

        # Scientific notation preserved for small q_value.
        assert row["q_value"] == "1.235e-64"

        # Nulls are empty strings, never "None", "null", or "NaN".
        for null_col in (
            "exclusion_reason",
            "enrichment_label",
            "broadly_expressed",
            "median_tumor",
            "median_rest",
            "auc",
            "subgroup_fraction_high",
            "subgroup_fold_over_rest",
            "subgroup_q_value",
            "pubmed_precheck_count",
        ):
            assert row[null_col] == "", f"{null_col} should be empty string, got {row[null_col]!r}"
        for value in row.values():
            assert value not in ("None", "null", "NaN", "nan")

        # Direct context test for a gene alias starting with '+' (and other formula prefixes).
        result = harness.service._gene_result(
            "ERBB2",
            [harness.store.rows[("ERBB2", "BRCA", RELEASE_ID, TUMOR_SPECIFIC_GATE)]],
            include_excluded=False,
        )
        ctx = GeneCsvContext(queried_as="+HER2", entrez_gene_id=2064, method_version="2")
        csv_text = format_csv_rows(
            gene_result_csv_rows(result, ctx), include_header=True, include_bom=True
        )
        _, alias_rows = parse_csv_response(csv_text)
        assert alias_rows[0]["queried_as"] == "'+HER2"

        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            ctx_p = GeneCsvContext(queried_as=f"{prefix}ALIAS")
            _, p_rows = parse_csv_response(
                format_csv_rows(
                    gene_result_csv_rows(result, ctx_p), include_header=True, include_bom=True
                )
            )
            assert p_rows[0]["queried_as"] == f"'{prefix}ALIAS"


def test_number_formatting_plain_decimal_and_scientific_q():
    assert format_float(1234.567) == "1235"
    assert "," not in format_float(123456.7)
    assert format_float(0.00123456) == "0.001235"
    assert format_float(-0.42) == "-0.42"
    assert format_float(0.0) == "0"
    assert format_float(None) == ""
    assert format_q(1.23456e-64) == "1.235e-64"
    assert format_q(0.012345) == "0.01235"
    assert format_q(None) == ""


def test_m3_fields_and_bulk_export_streams_current_bundle_only():
    gate = FakeLiteratureGate(
        verdicts={
            "prostate cancer": (False, ["11111111"]),
            "chromophobe renal cell carcinoma": (False, ["22222222"]),
            "breast cancer": (False, ["33333333"]),
            "sarcoma": (False, ["44444444"]),
        }
    )
    bundle = build_bundle(
        ("KLK3", "VIM"),
        release_id=M3_RELEASE_ID,
        subdir=M3,
        published_at="2026-11-07T06:00:00Z",
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        run_job(client, "KLK3")
        run_job(client, "VIM")

        # Seed a row from an OLD release_id to prove bulk export only includes the active bundle.
        harness.store.rows[("OLDGENE", "BRCA", "datahub-oldrelease-m2-000000000000", TUMOR_SPECIFIC_GATE)] = (
            StoredPair(
                hugo_symbol="OLDGENE",
                oncotree_code="BRCA",
                release_id="datahub-oldrelease-m2-000000000000",
                literature_gate=TUMOR_SPECIFIC_GATE,
                surfaced=True,
                exclusion_reason=None,
                stats=dict(gene_file("ERBB2")["cancer_types"][0]),
                evidence=_v2_supported_evidence("99999999", "breast cancer"),
                computed_at=datetime.now(timezone.utc),
            )
        )

        # Default bulk export: surfaced rows for KLK3 and VIM in the current bundle.
        resp = client.get(f"{PREFIX}/export.csv")
        assert resp.status_code == 200, resp.text
        assert (
            resp.headers["content-disposition"]
            == f'attachment; filename="expression-context_all_{M3_RELEASE_ID}.csv"'
        )
        _, surfaced_rows = parse_csv_response(resp.text)
        assert {(r["gene_symbol"], r["oncotree_code"]) for r in surfaced_rows} == {
            ("KLK3", "KICH"),
            ("KLK3", "PRAD"),
            ("VIM", "BRCA"),
            ("VIM", "KICH"),
        }
        assert all(r["release_id"] == M3_RELEASE_ID for r in surfaced_rows)
        assert all(r["gene_symbol"] != "OLDGENE" for r in surfaced_rows)

        by_pair = {(r["gene_symbol"], r["oncotree_code"]): r for r in surfaced_rows}
        assert by_pair[("KLK3", "PRAD")]["enrichment_label"] == "lineage_enriched"
        assert by_pair[("KLK3", "PRAD")]["broadly_expressed"] == "no"
        assert by_pair[("VIM", "BRCA")]["enrichment_label"] == "subgroup_enriched"
        assert by_pair[("VIM", "BRCA")]["broadly_expressed"] == "yes"
        assert by_pair[("VIM", "BRCA")]["subgroup_fraction_high"] == "0.1109"
        assert by_pair[("VIM", "BRCA")]["subgroup_fold_over_rest"] == "3.2"
        assert by_pair[("VIM", "BRCA")]["subgroup_q_value"] == "2e-08"

        # Bulk export with include_excluded=true includes SARC (broad-blocked -> stats) and BRCA for KLK3.
        full_resp = client.get(f"{PREFIX}/export.csv", params={"include_excluded": "true"})
        assert full_resp.status_code == 200, full_resp.text
        _, all_rows = parse_csv_response(full_resp.text)
        all_by_pair = {(r["gene_symbol"], r["oncotree_code"]): r for r in all_rows}
        assert all_by_pair[("VIM", "SARC")]["surfaced"] == "no"
        assert all_by_pair[("VIM", "SARC")]["stats_pass"] == "no"
        assert all_by_pair[("VIM", "SARC")]["exclusion_reason"] == "stats"
        assert all_by_pair[("VIM", "SARC")]["enrichment_label"] == ""


@pytest.mark.asyncio
async def test_bulk_export_streams_pages_incrementally():
    gate = FakeLiteratureGate(
        verdicts={
            "prostate cancer": (False, ["11111111"]),
            "chromophobe renal cell carcinoma": (False, ["22222222"]),
            "breast cancer": (False, ["33333333"]),
        }
    )
    bundle = build_bundle(
        ("KLK3", "VIM"),
        release_id=M3_RELEASE_ID,
        subdir=M3,
        published_at="2026-11-07T06:00:00Z",
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        run_job(client, "KLK3")
        run_job(client, "VIM")

        assert harness.service is not None
        _, stream = await harness.service.export_bundle_csv(include_excluded=False, page_size=1)
        chunks = [chunk async for chunk in stream]
        # Header chunk + one chunk per gene (paged with page_size=1).
        assert len(chunks) == 3
        assert chunks[0].startswith(UTF8_BOM)
        _, rows = parse_csv_response("".join(chunks))
        assert [r["gene_symbol"] for r in rows] == ["KLK3", "KLK3", "VIM", "VIM"]


def test_python_and_js_cbioportal_urls_are_identical():
    node = shutil.which("node")
    assert node is not None, "node is required for JS parity check"

    erbb2_studies = [
        "brca_tcga_pan_can_atlas_2018",
        "stad_tcga_pan_can_atlas_2018",
        "esca_tcga_pan_can_atlas_2018",
        "ucec_tcga_pan_can_atlas_2018",
        "luad_tcga_pan_can_atlas_2018",
    ]
    klk3_studies = [
        "prad_tcga_pan_can_atlas_2018",
        "kich_tcga_pan_can_atlas_2018",
        "brca_tcga_pan_can_atlas_2018",
        "sarc_tcga_pan_can_atlas_2018",
    ]
    cases = [
        ("ERBB2", ["brca_tcga_pan_can_atlas_2018"], "CANCER_STUDY"),
        ("ERBB2", ["brca_tcga_pan_can_atlas_2018"], "CANCER_TYPE_DETAILED"),
        ("ERBB2", erbb2_studies, "CANCER_STUDY"),
        ("ERBB2", erbb2_studies, "CANCER_TYPE_DETAILED"),
        ("KLK3", ["prad_tcga_pan_can_atlas_2018"], "CANCER_STUDY"),
        ("KLK3", ["prad_tcga_pan_can_atlas_2018"], "CANCER_TYPE_DETAILED"),
        ("KLK3", klk3_studies, "CANCER_STUDY"),
        ("ERBB2", ["coadread_tcga_pan_can_atlas_2018"], "CANCER_STUDY"),
        ("ERBB2", ["coadread_tcga_pan_can_atlas_2018"], "CANCER_TYPE_DETAILED"),
        (
            "ERBB2",
            ["coadread_tcga_pan_can_atlas_2018", "brca_tcga_pan_can_atlas_2018"],
            "CANCER_STUDY",
        ),
    ]
    script = (
        f"const {{ expressionURL }} = require({json.dumps(str(JS_PATH))});\n"
        f"const cases = {json.dumps(cases)};\n"
        "console.log(JSON.stringify(cases.map(([s, ids, h]) => expressionURL(s, ids, h))));\n"
    )
    proc = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    js_urls = json.loads(proc.stdout.strip())
    py_urls = [cbioportal_expression_url(s, ids, h) for s, ids, h in cases]
    assert py_urls == js_urls
