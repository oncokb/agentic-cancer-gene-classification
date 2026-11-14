"""CSV export for expression-context curation ("golden set" sheets).

Covers:
- Header order and exact column set (36 columns; ``cancer_type_code`` omitted
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

import asyncio
import csv
from dataclasses import replace
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
from src.expression_context.catalog import ActiveBundle, BundleGone, BundleNotLoaded, StatsCatalog
from src.expression_context.csv_export import (
    ALLOWED_ALTERATION_TYPES,
    CSV_COLUMNS,
    GOLDEN_SET_COLUMNS,
    UTF8_BOM,
    alteration_types_for_pair,
    cbioportal_expression_url,
    format_csv_rows,
    format_float,
    format_golden_set_rows,
    format_q,
    gene_view_csv_rows,
    golden_set_filename,
    golden_set_row_from_view,
    load_sheet_cancer_names,
)
from src.expression_context.lease import VerdictLease
from src.expression_context.literature_gate import TUMOR_SPECIFIC_GATE
from src.expression_context.models import (
    BundleManifest,
    GeneResult,
    JobRecord,
    LiteratureVerdict,
    PairResult,
    PairStatsView,
    StoredPair,
)
from src.expression_context.service import ExpressionContextService
from src.expression_context.store import InMemoryExpressionContextStore, MySQLExpressionContextStore
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
    "alteration_types_enriched",
    "confidence_tier",
    "purity_check",
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

    bundle = build_bundle(
        edit_gene=lambda doc: doc.update(
            {"cancer_types": [dict(p.stats) for p in seeded_rows]}
        )
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None
    )
    harness = CsvHarness(bundle=bundle)

    with TestClient(harness.app) as client:
        for pair in seeded_rows:
            harness.store.rows[
                (pair.hugo_symbol, pair.oncotree_code, pair.release_id, pair.literature_gate)
            ] = pair

        stats_resp = client.get(f"{PREFIX}/stats/ERBB2").json()
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

        stats_surfaced = [p["oncotree_code"] for p in stats_resp["pairs"] if p["status"] == "surfaced"]
        json_surfaced_codes = [p["oncotree_code"] for p in json_default["surfaced"]]
        assert stats_surfaced == json_surfaced_codes == ["BRCA"]
        assert [r["oncotree_code"] for r in csv_default] == json_surfaced_codes

        json_excluded_by_code = {
            p["oncotree_code"]: p["exclusion_reason"] for p in json_full["excluded"]
        }
        assert json_excluded_by_code == {
            "LUAD": "stats",
            "SARC": "stats",
            "STAD": "pending",
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

        # Also cover history after a mid-request BundleGone (retries and recovers)
        orig_gene = harness.service.catalog.gene
        gone_called = False

        async def gene_with_single_gone(*args, **kwargs):
            nonlocal gone_called
            if not gone_called:
                gone_called = True
                raise BundleGone("simulated-mid-request-gone")
            return await orig_gene(*args, **kwargs)

        harness.service.catalog.gene = gene_with_single_gone
        history_after_gone = client.get(f"{PREFIX}/genes").json()
        assert gone_called is True
        assert history_after_gone["items"][0]["surfaced_count"] == 1
        harness.service.catalog.gene = orig_gene


def test_rfc4180_quoting_round_trip_and_csv_injection_guard():
    """Commas, double quotes, and newlines in rationale round-trip cleanly;
    text cells starting with = + - @ tab or CR are prefixed with a single quote,
    while negative numeric columns (cliffs_delta, log2_fold_change) remain plain numbers."""
    tricky_rationale = '=SUM(A1:A2), "quoted, phrase",\nand second line'
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
    bundle = build_bundle(
        edit_gene=lambda doc: doc.update({"cancer_types": [base_stats]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None
    )
    harness = CsvHarness(bundle=bundle)

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
        import asyncio
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        view_plus = view.model_copy(update={"query": "+HER2"})
        csv_text = format_csv_rows(
            gene_view_csv_rows(view_plus, include_excluded=False),
            include_header=True,
            include_bom=True,
        )
        _, alias_rows = parse_csv_response(csv_text)
        assert alias_rows[0]["queried_as"] == "'+HER2"

        for prefix in ("=", "+", "-", "@", "\t", "\r"):
            view_p = view.model_copy(update={"query": f"{prefix}ALIAS"})
            _, p_rows = parse_csv_response(
                format_csv_rows(
                    gene_view_csv_rows(view_p, include_excluded=False),
                    include_header=True,
                    include_bom=True,
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


def test_release_a_surfaces_release_b_fails_it_not_surfaced_anywhere():
    bundle_a = build_bundle(release_id="release-a")
    erbb2_b = dict(gene_file("ERBB2"))
    brca_failing = dict(erbb2_b["cancer_types"][0])
    brca_failing["stats_pass"] = False
    brca_failing["q_value"] = 0.5
    bundle_b = build_bundle(
        release_id="release-b",
        edit_gene=lambda doc: doc.update({"cancer_types": [brca_failing]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )

    harness = CsvHarness(bundle=bundle_a)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    with TestClient(harness.app) as client:
        # Seed evidence under Release A where it surfaces:
        harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="release-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=erbb2_b["cancer_types"][0],
            evidence={
                "literature_gate": TUMOR_SPECIFIC_GATE,
                "tumor_type_query": "breast cancer",
                "supported": True,
                "insufficient_evidence": False,
                "pmids": ["12345678"],
                "verdict": "supported",
                "papers": [{"pmid": "12345678", "tumor_specific": True}],
                "evaluated_at": now.isoformat(),
            },
            computed_at=now,
        )
        job_a = JobRecord(
            job_id="job-release-a",
            status="succeeded",
            gene="ERBB2",
            hugo_symbol="ERBB2",
            release_id="release-a",
            bundle_tag=bundle_a.tag,
            created_at=now,
            owner=ALICE.email,
            result=GeneResult(
                hugo_symbol="ERBB2",
                release_id="release-a",
                literature_gate=TUMOR_SPECIFIC_GATE,
                computed_at=now,
                surfaced=[
                    PairResult(
                        oncotree_code="BRCA",
                        surfaced=True,
                        stats=erbb2_b["cancer_types"][0],
                        computed_at=now,
                    )
                ],
            ),
        )
        import asyncio
        asyncio.run(harness.service.jobs.put(job_a))

        stats_a = client.get(f"{PREFIX}/stats/ERBB2").json()
        assert any(p["oncotree_code"] == "BRCA" and p["status"] == "surfaced" for p in stats_a["pairs"])

        # Activate Bundle B (where BRCA fails stats)
        asyncio.run(seed_bundle(harness.bundles, bundle_b))
        harness.service.catalog.invalidate()

        # Under Release B:
        # 1. /stats/ERBB2: BRCA is excluded with stats
        stats_b = client.get(f"{PREFIX}/stats/ERBB2").json()
        brca_stats = next(p for p in stats_b["pairs"] if p["oncotree_code"] == "BRCA")
        assert brca_stats["status"] == "excluded"
        assert brca_stats["exclusion_reason"] == "stats"

        # 2. /genes/ERBB2 (default: surfaced only)
        genes_def = client.get(f"{PREFIX}/genes/ERBB2").json()
        assert not any(p["oncotree_code"] == "BRCA" for p in genes_def["surfaced"])

        # 3. /genes/ERBB2?include_excluded=true
        genes_full = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}).json()
        assert not any(p["oncotree_code"] == "BRCA" for p in genes_full["surfaced"])
        brca_ex = next(p for p in genes_full["excluded"] if p["oncotree_code"] == "BRCA")
        assert brca_ex["exclusion_reason"] == "stats"

        # 4. per-gene CSV: export.csv
        _, csv_def = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
        assert not any(r["oncotree_code"] == "BRCA" for r in csv_def)

        # 5. per-gene CSV with include_excluded=true
        _, csv_full = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text)
        brca_csv = next(r for r in csv_full if r["oncotree_code"] == "BRCA")
        assert brca_csv["surfaced"] == "no"
        assert brca_csv["exclusion_reason"] == "stats"

        # 6. Bulk CSV (export.csv)
        _, bulk_def = parse_csv_response(client.get(f"{PREFIX}/export.csv").text)
        assert not any(r["gene_symbol"] == "ERBB2" and r["oncotree_code"] == "BRCA" for r in bulk_def)

        # 7. History (/genes)
        hist = client.get(f"{PREFIX}/genes").json()
        erbb2_hist = [h for h in hist["items"] if h["hugo_symbol"] == "ERBB2"]
        assert all(h["surfaced_count"] == 0 for h in erbb2_hist)

        # 8. Polled job (/jobs/{job_id})
        poll_job = client.get(f"{PREFIX}/jobs/{job_a.job_id}").json()
        assert poll_job["status"] == "succeeded"
        assert not any(p["oncotree_code"] == "BRCA" for p in poll_job["result"]["surfaced"])


def test_active_release_passes_evidence_only_older_release_pending_everywhere():
    harness = CsvHarness()
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    with TestClient(harness.app) as client:
        # Seed evidence only under an older release
        harness.store.rows[("ERBB2", "BRCA", "older-release", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="older-release",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=gene_file("ERBB2")["cancer_types"][0],
            evidence={
                "literature_gate": TUMOR_SPECIFIC_GATE,
                "tumor_type_query": "breast cancer",
                "supported": True,
                "insufficient_evidence": False,
                "pmids": ["12345678"],
                "verdict": "supported",
                "papers": [{"pmid": "12345678", "tumor_specific": True}],
                "evaluated_at": now.isoformat(),
            },
            computed_at=now,
        )

        stats = client.get(f"{PREFIX}/stats/ERBB2").json()
        brca = next(p for p in stats["pairs"] if p["oncotree_code"] == "BRCA")
        assert brca["status"] == "literature_pending"
        assert brca["literature"] is None

        genes_def = client.get(f"{PREFIX}/genes/ERBB2").json()
        assert len(genes_def["surfaced"]) == 0

        genes_full = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}).json()
        assert len(genes_full["surfaced"]) == 0
        brca_ex = next(p for p in genes_full["excluded"] if p["oncotree_code"] == "BRCA")
        assert brca_ex["exclusion_reason"] == "pending"

        _, csv_def = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
        assert len(csv_def) == 0

        _, csv_full = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text)
        brca_csv = next(r for r in csv_full if r["oncotree_code"] == "BRCA")
        assert brca_csv["surfaced"] == "no"
        assert brca_csv["exclusion_reason"] == "pending"

        _, bulk_def = parse_csv_response(client.get(f"{PREFIX}/export.csv").text)
        assert len(bulk_def) == 0

        hist = client.get(f"{PREFIX}/genes").json()
        erbb2_hist = next(h for h in hist["items"] if h["hugo_symbol"] == "ERBB2")
        assert erbb2_hist["surfaced_count"] == 0


def test_stored_false_precheck_expired_no_longer_excludes_and_csv_columns_agree():
    now = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    base_stats["literature_precheck_pass"] = False
    base_stats["precheck_expired"] = False

    manifest_overrides = {
        "source": {
            "precheck_expires_at": "2026-11-01T00:00:00Z"
        }
    }
    bundle = build_bundle(
        manifest_overrides=manifest_overrides,
        edit_gene=lambda doc: doc.update({"cancer_types": [base_stats]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app) as client:
        harness.service._clock = lambda: now

        # Case A: with supported literature evidence stored under active release
        harness.store.rows[("ERBB2", "BRCA", bundle.manifest["release_id"], TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=bundle.manifest["release_id"],
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=base_stats,
            evidence={
                "literature_gate": TUMOR_SPECIFIC_GATE,
                "tumor_type_query": "breast cancer",
                "supported": True,
                "insufficient_evidence": False,
                "pmids": ["12345678"],
                "verdict": "supported",
                "papers": [{"pmid": "12345678", "tumor_specific": True}],
                "evaluated_at": now.isoformat(),
            },
            computed_at=now,
        )

        stats = client.get(f"{PREFIX}/stats/ERBB2").json()
        brca_stats = next(p for p in stats["pairs"] if p["oncotree_code"] == "BRCA")
        assert brca_stats["precheck"] == "expired"
        assert brca_stats["status"] == "surfaced"

        genes = client.get(f"{PREFIX}/genes/ERBB2").json()
        assert any(p["oncotree_code"] == "BRCA" for p in genes["surfaced"])

        _, csv_full = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text)
        brca_csv = next(r for r in csv_full if r["oncotree_code"] == "BRCA")
        assert brca_csv["pubmed_precheck"] == "expired"
        assert brca_csv["surfaced"] == "yes"
        assert brca_csv["exclusion_reason"] == ""

        history = client.get(f"{PREFIX}/genes").json()
        assert history["items"][0]["surfaced_count"] == 1

        # Case B: when literature rejected:
        harness.store.rows[("ERBB2", "BRCA", bundle.manifest["release_id"], TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=bundle.manifest["release_id"],
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=False,
            exclusion_reason="literature",
            stats=base_stats,
            evidence={
                "literature_gate": TUMOR_SPECIFIC_GATE,
                "tumor_type_query": "breast cancer",
                "supported": False,
                "insufficient_evidence": False,
                "pmids": ["12345678"],
                "verdict": "not_supported",
                "papers": [{"pmid": "12345678", "tumor_specific": False}],
                "evaluated_at": now.isoformat(),
            },
            computed_at=now,
        )

        _, csv_full_rej = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text)
        brca_rej = next(r for r in csv_full_rej if r["oncotree_code"] == "BRCA")
        assert brca_rej["pubmed_precheck"] == "expired"
        assert brca_rej["surfaced"] == "no"
        assert brca_rej["exclusion_reason"] == "literature"


def test_fresh_false_precheck_with_supported_evidence_excluded_everywhere():
    now = datetime(2026, 11, 1, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    base_stats["literature_precheck_pass"] = False

    manifest_overrides = {
        "source": {
            "precheck_expires_at": "2026-11-15T00:00:00Z"
        }
    }
    bundle = build_bundle(
        manifest_overrides=manifest_overrides,
        edit_gene=lambda doc: doc.update({"cancer_types": [base_stats]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app) as client:
        harness.service._clock = lambda: now
        harness.store.rows[("ERBB2", "BRCA", bundle.manifest["release_id"], TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=bundle.manifest["release_id"],
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=base_stats,
            evidence={
                "literature_gate": TUMOR_SPECIFIC_GATE,
                "tumor_type_query": "breast cancer",
                "supported": True,
                "insufficient_evidence": False,
                "pmids": ["12345678"],
                "verdict": "supported",
                "papers": [{"pmid": "12345678", "tumor_specific": True}],
                "evaluated_at": now.isoformat(),
            },
            computed_at=now,
        )

        stats = client.get(f"{PREFIX}/stats/ERBB2").json()
        brca_stats = next(p for p in stats["pairs"] if p["oncotree_code"] == "BRCA")
        assert brca_stats["status"] == "excluded"
        assert brca_stats["exclusion_reason"] == "precheck"

        genes = client.get(f"{PREFIX}/genes/ERBB2").json()
        assert not any(p["oncotree_code"] == "BRCA" for p in genes["surfaced"])

        genes_full = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}).json()
        brca_ex = next(p for p in genes_full["excluded"] if p["oncotree_code"] == "BRCA")
        assert brca_ex["exclusion_reason"] == "precheck"

        _, csv_full = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text)
        brca_csv = next(r for r in csv_full if r["oncotree_code"] == "BRCA")
        assert brca_csv["surfaced"] == "no"
        assert brca_csv["exclusion_reason"] == "precheck"
        assert brca_csv["pubmed_precheck"] == "fail"


def test_stats_failing_and_broad_blocked_lineage_excluded_broad_subgroup_pass_surfaced():
    bundle = build_bundle(
        ("KLK3", "VIM"),
        release_id=M3_RELEASE_ID,
        subdir=M3,
        published_at="2026-11-07T06:00:00Z",
    )
    harness = CsvHarness(bundle=bundle)
    now = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    with TestClient(harness.app) as client:
        vim_doc = gene_file("VIM", subdir=M3)
        brca_stats = next(ct for ct in vim_doc["cancer_types"] if ct["oncotree_code"] == "BRCA")
        sarc_stats = next(ct for ct in vim_doc["cancer_types"] if ct["oncotree_code"] == "SARC")

        supported_ev = {
            "literature_gate": TUMOR_SPECIFIC_GATE,
            "tumor_type_query": "cancer",
            "supported": True,
            "insufficient_evidence": False,
            "pmids": ["12345678"],
            "verdict": "supported",
            "papers": [{"pmid": "12345678", "tumor_specific": True}],
            "evaluated_at": now.isoformat(),
        }

        harness.store.rows[("VIM", "BRCA", M3_RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="VIM",
            oncotree_code="BRCA",
            release_id=M3_RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=brca_stats,
            evidence=supported_ev,
            computed_at=now,
        )
        harness.store.rows[("VIM", "SARC", M3_RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="VIM",
            oncotree_code="SARC",
            release_id=M3_RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=sarc_stats,
            evidence=supported_ev,
            computed_at=now,
        )

        stats = client.get(f"{PREFIX}/stats/VIM").json()
        brca = next(p for p in stats["pairs"] if p["oncotree_code"] == "BRCA")
        sarc = next(p for p in stats["pairs"] if p["oncotree_code"] == "SARC")
        assert brca["status"] == "surfaced"
        assert sarc["status"] == "excluded"
        assert sarc["exclusion_reason"] == "stats"

        genes = client.get(f"{PREFIX}/genes/VIM", params={"include_excluded": "true"}).json()
        assert any(p["oncotree_code"] == "BRCA" for p in genes["surfaced"])
        assert not any(p["oncotree_code"] == "SARC" for p in genes["surfaced"])
        sarc_ex = next(p for p in genes["excluded"] if p["oncotree_code"] == "SARC")
        assert sarc_ex["exclusion_reason"] == "stats"


def test_agreement_across_all_read_paths():
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (False, ["23456789"]),
        }
    )
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        job = run_job(client, "ERBB2")

        stats_resp = client.get(f"{PREFIX}/stats/ERBB2").json()
        stats_surfaced = {p["oncotree_code"] for p in stats_resp["pairs"] if p["status"] == "surfaced"}

        genes_resp = client.get(f"{PREFIX}/genes/ERBB2").json()
        genes_surfaced = {p["oncotree_code"] for p in genes_resp["surfaced"]}

        genes_full_resp = client.get(f"{PREFIX}/genes/ERBB2", params={"include_excluded": "true"}).json()
        genes_full_surfaced = {p["oncotree_code"] for p in genes_full_resp["surfaced"]}

        _, csv_rows = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
        csv_surfaced = {r["oncotree_code"] for r in csv_rows}

        _, csv_full_rows = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv", params={"include_excluded": "true"}).text)
        csv_full_surfaced = {r["oncotree_code"] for r in csv_full_rows if r["surfaced"] == "yes"}

        _, bulk_rows = parse_csv_response(client.get(f"{PREFIX}/export.csv").text)
        bulk_surfaced = {r["oncotree_code"] for r in bulk_rows if r["gene_symbol"] == "ERBB2"}

        history = client.get(f"{PREFIX}/genes").json()
        hist_entry = next(h for h in history["items"] if h["hugo_symbol"] == "ERBB2")
        assert hist_entry["surfaced_count"] == len(stats_surfaced)

        polled = client.get(f"{PREFIX}/jobs/{job['job_id']}").json()
        polled_surfaced = {p["oncotree_code"] for p in polled["result"]["surfaced"]}

        assert len(stats_surfaced) > 0
        assert stats_surfaced == {"BRCA", "STAD"}
        assert (
            stats_surfaced
            == genes_surfaced
            == genes_full_surfaced
            == csv_surfaced
            == csv_full_surfaced
            == bulk_surfaced
            == polled_surfaced
        )


def test_bulk_include_excluded_exports_bundle_gene_without_literature_job():
    gate = FakeLiteratureGate(verdicts={"breast cancer": (True, ["12345678"])})
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        run_job(client, "ERBB2")

        resp = client.get(f"{PREFIX}/export.csv", params={"include_excluded": "true"})
        assert resp.status_code == 200
        _, rows = parse_csv_response(resp.text)

        genes_in_csv = {r["gene_symbol"] for r in rows}
        assert "ERBB2" in genes_in_csv
        assert "MYCN" in genes_in_csv

        mycn_rows = [r for r in rows if r["gene_symbol"] == "MYCN"]
        assert len(mycn_rows) > 0
        for r in mycn_rows:
            assert r["surfaced"] == "no"
            assert r["exclusion_reason"] in ("pending", "stats", "precheck")


def test_no_active_bundle_503_for_genes_and_per_gene_csv():
    harness = CsvHarness(seed=False)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    harness.store.rows[("ERBB2", "BRCA", RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id=RELEASE_ID,
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=gene_file("ERBB2")["cancer_types"][0],
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )
    with TestClient(harness.app) as client:
        r1 = client.get(f"{PREFIX}/genes/ERBB2")
        assert r1.status_code == 503
        assert r1.headers["retry-after"] == "60"

        r2 = client.get(f"{PREFIX}/genes/ERBB2/export.csv")
        assert r2.status_code == 503
        assert r2.headers["retry-after"] == "60"

        r3 = client.get(f"{PREFIX}/stats/ERBB2")
        assert r3.status_code == 503

        r4 = client.get(f"{PREFIX}/genes")
        assert r4.status_code == 503
        assert r4.headers["retry-after"] == "60"

        job = JobRecord(
            job_id="job-test-503",
            status="succeeded",
            gene="ERBB2",
            hugo_symbol="ERBB2",
            release_id=RELEASE_ID,
            bundle_tag="tag-test",
            created_at=now,
            owner=ALICE.email,
            result=GeneResult(
                hugo_symbol="ERBB2",
                release_id=RELEASE_ID,
                literature_gate=TUMOR_SPECIFIC_GATE,
                computed_at=now,
                surfaced=[
                    PairResult(
                        oncotree_code="BRCA",
                        surfaced=True,
                        stats=gene_file("ERBB2")["cancer_types"][0],
                        computed_at=now,
                    )
                ],
            ),
        )
        import asyncio
        asyncio.run(harness.service.jobs.put(job))
        r_job = client.get(f"{PREFIX}/jobs/{job.job_id}")
        assert r_job.status_code == 503
        assert r_job.headers["retry-after"] == "60"


@pytest.mark.asyncio
async def test_early_query_failure_in_bulk_export_returns_non_200_and_midstream_bundle_gone():
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app) as client:
        run_job(client, "ERBB2")

        # 1. Early query failure: simulate store query failure on initial page
        async def failing_list_release_genes(*args, **kwargs):
            raise RuntimeError("Database connection refused during initial page query")

        orig_method = harness.store.list_release_genes
        harness.store.list_release_genes = failing_list_release_genes

        with TestClient(harness.app, raise_server_exceptions=False) as client_no_raise:
            resp = client_no_raise.get(f"{PREFIX}/export.csv")
            assert resp.status_code == 500, "Early failure must return non-200"

        harness.store.list_release_genes = orig_method

        # 2. Mid-stream bundle gone: active bundle superseded mid-stream
        assert harness.service is not None
        _, stream = await harness.service.export_bundle_csv(include_excluded=True, page_size=1)
        first_chunk = await anext(stream)
        assert first_chunk.startswith(UTF8_BOM)

        # Supersede active bundle pointer mid-stream
        harness.bundles.state = replace(harness.bundles.state, active_bundle_tag="another-tag")
        harness.service.catalog.invalidate()

        with pytest.raises(BundleGone):
            async for _ in stream:
                pass


@pytest.mark.asyncio
async def test_deadlock_concurrent_history_requests_complete_with_bounded_pool():
    from contextlib import asynccontextmanager

    N = 2
    pool_sem = asyncio.Semaphore(N)
    barrier = asyncio.Barrier(N)

    class FakePoolConn:
        @asynccontextmanager
        async def cursor(self):
            yield FakeDbCursor()

    class FakeDbCursor:
        def __init__(self):
            self._query = ""

        async def execute(self, query, params=None):
            self._query = query

        async def fetchall(self):
            # Await the barrier while holding this connection.
            # Guarantees that all N concurrent history requests hold every connection
            # in the pool simultaneously before any evaluation runs.
            await barrier.wait()
            now = datetime(2026, 11, 6, 12, 0)
            if "GROUP BY" in self._query:
                return [("ERBB2", "release-a", now, 1)]
            return []

    class BoundedPool:
        @asynccontextmanager
        async def acquire(self):
            await pool_sem.acquire()
            try:
                yield FakePoolConn()
            finally:
                pool_sem.release()

        def close(self):
            pass

        async def wait_closed(self):
            pass

    pool = BoundedPool()
    store = MySQLExpressionContextStore(pool)
    bundle = build_bundle(("ERBB2",), release_id="release-a")
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app):
        harness.service.store = store

        orig_gene = harness.service.catalog.gene

        async def gene_acquiring_pool(*args, **kwargs):
            async with pool.acquire():
                return await orig_gene(*args, **kwargs)

        harness.service.catalog.gene = gene_acquiring_pool

        async def fetch_history():
            return await harness.service.history(limit=10, cursor=None)

        # N concurrent requests (= pool size N) hold every connection at once at the barrier,
        # then release them before evaluating gene_view, completing without deadlocking.
        results = await asyncio.wait_for(
            asyncio.gather(*(fetch_history() for _ in range(N))),
            timeout=2.0,
        )
        assert len(results) == N
        for r in results:
            assert len(r.items) == 1
            assert r.items[0].hugo_symbol == "ERBB2"


@pytest.mark.asyncio
async def test_history_active_stats_bundle_gone_never_surfaces_from_stored_stats():
    # Stored pair has surfaced=True in stored stats, but active bundle B fails stats.
    # When active stats lookup raises BundleGone, history must NEVER fall back to stored stats.
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    bundle_a = build_bundle(("ERBB2",), release_id="release-a")

    erbb2_b = dict(gene_file("ERBB2"))
    brca_failing = dict(erbb2_b["cancer_types"][0])
    brca_failing["stats_pass"] = False
    brca_failing["q_value"] = 0.5
    bundle_b = build_bundle(
        release_id="release-b",
        edit_gene=lambda doc: doc.update({"cancer_types": [brca_failing]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )

    harness = CsvHarness(bundle=bundle_a)
    with TestClient(harness.app):
        harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="release-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats={"stats_pass": True, "cancer_type_name": "Breast Cancer"},
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )

        orig_gene = harness.service.catalog.gene
        gone_raised = False

        async def gene_gone_then_b(bundle_arg, symbol):
            nonlocal gone_raised
            if not gone_raised:
                gone_raised = True
                await seed_bundle(harness.bundles, bundle_b)
                harness.service.catalog.invalidate()
                raise BundleGone("simulated-gone")
            return await orig_gene(bundle_arg, symbol)

        harness.service.catalog.gene = gene_gone_then_b
        hist = await harness.service.history(limit=10, cursor=None)
        assert gone_raised is True
        assert hist.items[0].hugo_symbol == "ERBB2"
        assert hist.items[0].surfaced_count == 0

        harness.bundles.state = replace(harness.bundles.state, active_bundle_tag="nonexistent")
        harness.service.catalog.invalidate()
        with pytest.raises(BundleNotLoaded):
            await harness.service.history(limit=10, cursor=None)


@pytest.mark.asyncio
async def test_job_submitted_on_release_a_and_b_activates_before_completion():
    bundle_a = build_bundle(("ERBB2",), release_id="release-a")
    erbb2_b = dict(gene_file("ERBB2"))
    brca_failing = dict(erbb2_b["cancer_types"][0])
    brca_failing["stats_pass"] = False
    brca_failing["q_value"] = 0.5
    bundle_b = build_bundle(
        release_id="release-b",
        edit_gene=lambda doc: doc.update({"cancer_types": [brca_failing]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )

    harness = CsvHarness(bundle=bundle_a)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    with TestClient(harness.app) as client:
        resolved = await harness.service.resolve("ERBB2")
        assert resolved.manifest.release_id == "release-a"

        await seed_bundle(harness.bundles, bundle_b)
        harness.service.catalog.invalidate()

        result, evaluated, reused, errors = await harness.service.evaluate(resolved, ALICE.email)
        assert not any(p.oncotree_code == "BRCA" for p in result.surfaced)

        job = JobRecord(
            job_id="job-ab-test",
            status="succeeded",
            gene="ERBB2",
            hugo_symbol="ERBB2",
            release_id="release-a",
            bundle_tag=bundle_a.tag,
            created_at=now,
            owner=ALICE.email,
            result=result,
        )
        await harness.service.jobs.put(job)
        polled = client.get(f"{PREFIX}/jobs/{job.job_id}").json()
        assert not any(p["oncotree_code"] == "BRCA" for p in polled["result"]["surfaced"])


@pytest.mark.asyncio
async def test_gene_view_bundle_none_re_resolves_against_active_bundle():
    # Resolve under passing release A, activate stats-failing B.
    # Calling gene_view with resolved=replace(resolved_a, bundle=None) must re-resolve
    # against active bundle B, returning release B with no surfaced pair (not one).
    # And bundle=None with no active bundle must raise BundleNotLoaded.
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    bundle_a = build_bundle(("ERBB2",), release_id="release-a")

    erbb2_b = dict(gene_file("ERBB2"))
    brca_failing = dict(erbb2_b["cancer_types"][0])
    brca_failing["stats_pass"] = False
    brca_failing["q_value"] = 0.5
    bundle_b = build_bundle(
        release_id="release-b",
        edit_gene=lambda doc: doc.update({"cancer_types": [brca_failing]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )

    harness = CsvHarness(bundle=bundle_a)
    with TestClient(harness.app):
        # Stored pair under release-a with valid tumor-specific evidence
        harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="release-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=dict(gene_file("ERBB2")["cancer_types"][0]),
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )

        resolved_a = await harness.service.resolve("ERBB2")
        assert resolved_a.bundle.release_id == "release-a"

        # Activate stats-failing release B
        await seed_bundle(harness.bundles, bundle_b)
        harness.service.catalog.invalidate()

        resolved_no_bundle = replace(resolved_a, bundle=None)
        view, _ = await harness.service.gene_view("ERBB2", resolved=resolved_no_bundle)
        assert view.bundle.release_id == "release-b"
        assert view.summary.surfaced == 0
        assert not any(p.status == "surfaced" for p in view.pairs)

        # Disagreement guard: if a ResolvedGene has mismatched bundle and manifest, raise ValueError
        mismatched = replace(resolved_a, bundle=await harness.service.catalog.active())
        with pytest.raises(ValueError, match="manifest release_id"):
            harness.service._evaluate_gene_view(mismatched, {})

        # bundle=None with no active bundle raises BundleNotLoaded
        harness.bundles.state = replace(harness.bundles.state, active_bundle_tag="nonexistent")
        harness.service.catalog.invalidate()
        with pytest.raises(BundleNotLoaded):
            await harness.service.gene_view("ERBB2", resolved=resolved_no_bundle)


@pytest.mark.asyncio
async def test_bulk_stream_supersession_single_gene_and_multi_page():
    bundle_a = build_bundle(("ERBB2",), release_id="release-a")
    bundle_b = build_bundle(("ERBB2",), release_id="release-b")
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    harness = CsvHarness(bundle=bundle_a)
    harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id="release-a",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=dict(gene_file("ERBB2")["cancer_types"][0]),
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )

    # 1. Single-gene, default page size (25)
    with TestClient(harness.app):
        _, stream = await harness.service.export_bundle_csv(include_excluded=True, page_size=25)
        header = await anext(stream)
        assert header.startswith(UTF8_BOM)

        await seed_bundle(harness.bundles, bundle_b)
        harness.service.catalog.invalidate()

        emitted_chunks = []
        with pytest.raises(BundleGone):
            async for chunk in stream:
                emitted_chunks.append(chunk)

        for chunk in emitted_chunks:
            assert ",yes," not in chunk

    # 2. Multi-page case: page_size=1 with 2 genes
    harness2 = CsvHarness(bundle=build_bundle(("ERBB2", "MYCN"), release_id="release-a2"))
    with TestClient(harness2.app):
        _, stream2 = await harness2.service.export_bundle_csv(include_excluded=True, page_size=1)
        header2 = await anext(stream2)
        assert header2.startswith(UTF8_BOM)
        first_page = await anext(stream2)
        assert len(first_page) > 0

        await seed_bundle(harness2.bundles, bundle_b)
        harness2.service.catalog.invalidate()

        with pytest.raises(BundleGone):
            async for _ in stream2:
                pass


# ----------------------------------------------------------- Golden Set CSV Export


def test_golden_set_headers_exact_4_and_filled_columns():
    assert ALLOWED_ALTERATION_TYPES == (
        "Point Mutation",
        "Structural Variant",
        "Expression",
        "Copy Number Alteration",
    )

    bundle = build_bundle(("ERBB2",), release_id="rel-123")
    harness = CsvHarness(bundle=bundle)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])

    with TestClient(harness.app) as client:
        harness.store.rows[("ERBB2", "BRCA", "rel-123", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="rel-123",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=base_stats,
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )

        resp = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp.status_code == 200
        assert golden_set_filename("rel-123") == "expression-context_golden-set_rel-123.csv"
        assert resp.headers["Content-Disposition"] == f'attachment; filename="{golden_set_filename("rel-123")}"'
        header, rows = parse_csv_response(resp.text)
        assert len(header) == 4
        assert header == list(GOLDEN_SET_COLUMNS)
        assert header == [
            "Genes",
            "Relevant expression-based cancer type(s)",
            "Relevant alteration type(s)",
            "Evidence-associated PMIDs",
        ]
        assert len(rows) == 1
        row = rows[0]
        assert len(row) == 4

        # Verify format_golden_set_rows direct output
        direct_csv = format_golden_set_rows([list(row.values())], include_header=True, include_bom=True)
        assert direct_csv.startswith(UTF8_BOM)
        d_header, d_rows = parse_csv_response(direct_csv)
        assert d_header == header and len(d_rows) == 1

        # Filled columns
        assert row["Genes"] == "ERBB2"
        assert row["Relevant expression-based cancer type(s)"] == "Breast cancer"
        assert row["Relevant alteration type(s)"] == "Expression"
        assert row["Evidence-associated PMIDs"] == "12345678"

        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        assert alteration_types_for_pair(view.pairs[0]) == ["Expression"]

        # Never null, None, [], Unknown, FALSE or "No identified alterations"
        for k, v in row.items():
            assert v not in ("None", "null", "NaN", "nan", "[]", "Unknown", "FALSE", "No identified alterations")


def test_golden_set_alteration_types_stub_ordering_dedup_and_unknown_raises(monkeypatch):
    from src.expression_context import csv_export

    bundle = build_bundle(("ERBB2",))
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        pair_brca = view.pairs[0].model_copy(update={"status": "surfaced", "oncotree_code": "BRCA"})
        pair_stad = view.pairs[1].model_copy(update={"status": "surfaced", "oncotree_code": "STAD"})
        two_pair_view = view.model_copy(update={"pairs": [pair_brca, pair_stad]})

        # Test that stubs alteration_types_for_pair to return
        # ["Copy Number Alteration", "Expression", "Expression"] for one pair and
        # ["Point Mutation"] for another gives "Point Mutation, Expression, Copy Number Alteration"
        def stub_categories(pair: PairStatsView) -> List[str]:
            if pair.oncotree_code == "BRCA":
                return ["Copy Number Alteration", "Expression", "Expression"]
            if pair.oncotree_code == "STAD":
                return ["Point Mutation"]
            return ["Expression"]

        monkeypatch.setattr(csv_export, "alteration_types_for_pair", stub_categories)
        row = csv_export.golden_set_row_from_view(two_pair_view)
        assert len(row) == 4
        assert row[2] == "Point Mutation, Expression, Copy Number Alteration"

        # An unknown category raises
        def stub_unknown(pair: PairStatsView) -> List[str]:
            return ["Unknown Category"]

        monkeypatch.setattr(csv_export, "alteration_types_for_pair", stub_unknown)
        with pytest.raises(ValueError, match="Unknown alteration type"):
            csv_export.golden_set_row_from_view(two_pair_view)


def test_golden_set_alteration_types_detail_only_never_gating_on_unsurfaced_pairs(monkeypatch):
    from src.expression_context import csv_export

    bundle = build_bundle(("ERBB2",))
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        pair_surfaced = view.pairs[0].model_copy(update={"status": "surfaced", "oncotree_code": "BRCA"})
        pair_unsurfaced = view.pairs[1].model_copy(update={"status": "stats_excluded", "oncotree_code": "LUAD"})
        mixed_view = view.model_copy(update={"pairs": [pair_surfaced, pair_unsurfaced]})

        def stub_categories(pair: PairStatsView) -> List[str]:
            if pair.oncotree_code == "BRCA":
                return ["Expression"]
            # Non-surfaced pair returns alteration categories that must NOT affect the row
            return ["Point Mutation", "Copy Number Alteration"]

        monkeypatch.setattr(csv_export, "alteration_types_for_pair", stub_categories)
        row = csv_export.golden_set_row_from_view(mixed_view)
        assert len(row) == 4
        assert row[2] == "Expression"
        assert "Point Mutation" not in row[2]
        assert "Copy Number Alteration" not in row[2]

        # Cancer types and PMIDs must also not be affected
        name_map = load_sheet_cancer_names()
        assert row[1] == name_map["BRCA"]

        # If zero surfaced pairs exist, alteration cell is empty even if unsurfaced pair returns categories
        zero_surfaced_view = view.model_copy(update={"pairs": [pair_unsurfaced]})
        row_zero = csv_export.golden_set_row_from_view(zero_surfaced_view)
        assert len(row_zero) == 4
        assert row_zero[1] == ""
        assert row_zero[2] == ""
        assert row_zero[3] == ""


def test_golden_set_manifest_32_cohort_codes_and_missing_code_raises():
    from src.expression_context.tumor_aliases import COHORT_NAMES

    name_map = load_sheet_cancer_names()
    assert len(COHORT_NAMES) == 32
    assert len(name_map) == 32
    for code in COHORT_NAMES:
        assert code in name_map, f"Cohort code {code!r} missing in sheet_cancer_names.json"

    # Runtime raise in golden_set_row_from_view as a backstop
    bundle = build_bundle(("ERBB2",))
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        surfaced_pair = view.pairs[0].model_copy(update={"status": "surfaced", "oncotree_code": "UNKNOWN_CODE"})
        modified_view = view.model_copy(update={"pairs": [surfaced_pair]})
        with pytest.raises(KeyError, match="UNKNOWN_CODE"):
            golden_set_row_from_view(modified_view)

    # Missing code in bundle manifest cancer_types fails requests with 500 clean error
    bundle_unmapped = build_bundle(
        ("ERBB2",),
        manifest_overrides={
            "cancer_types": [
                {
                    "oncotree_code": "UNKNOWN_CODE",
                    "study_id": "unknown_tcga",
                    "cancer_type_name": "Unknown Cancer",
                    "name": "Unknown Cancer",
                    "tumor_type_query": "unknown cancer",
                }
            ]
        },
    )
    harness_unmapped = CsvHarness(bundle=bundle_unmapped)
    with TestClient(harness_unmapped.app) as client:
        resp_bulk = client.get(f"{PREFIX}/golden-set.csv")
        assert resp_bulk.status_code == 500
        assert "UNKNOWN_CODE" in resp_bulk.json()["detail"]

        resp_genes = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp_genes.status_code == 500
        assert "UNKNOWN_CODE" in resp_genes.json()["detail"]


def test_golden_set_zero_surfaced_gene_only_genes_filled():
    bundle = build_bundle(("MYCN",), release_id="rel-zero")
    harness = CsvHarness(bundle=bundle)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("MYCN")["cancer_types"][0])
    # Store a checked-but-zero-surfaced pair for rel-zero
    harness.store.rows[("MYCN", base_stats["oncotree_code"], "rel-zero", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="MYCN",
        oncotree_code=base_stats["oncotree_code"],
        release_id="rel-zero",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=False,
        exclusion_reason="literature",
        stats=base_stats,
        evidence={
            "literature_gate": TUMOR_SPECIFIC_GATE,
            "tumor_type_query": "neuroblastoma",
            "supported": False,
            "insufficient_evidence": False,
            "pmids": [],
            "rationale": "No evidence.",
            "evaluated_at": "2026-11-05T12:00:00Z",
            "verdict": "insufficient_evidence",
            "papers": [],
            "decided_by": "judge",
        },
        computed_at=now,
    )
    with TestClient(harness.app) as client:
        resp = client.get(f"{PREFIX}/golden-set.csv?genes=MYCN")
        assert resp.status_code == 200
        header, rows = parse_csv_response(resp.text)
        assert len(rows) == 1
        row = rows[0]
        assert row["Genes"] == "MYCN"
        for col in header:
            if col != "Genes":
                assert row[col] == "", f"Zero-surfaced gene column {col!r} should be empty string, got {row[col]!r}"


def test_golden_set_unchecked_gene_returns_422():
    bundle = build_bundle(("ERBB2", "MYCN"), release_id="rel-a")
    harness = CsvHarness(bundle=bundle)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    # Only ERBB2 has stored result
    harness.store.rows[("ERBB2", "BRCA", "rel-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id="rel-a",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=base_stats,
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )
    with TestClient(harness.app) as client:
        # MYCN is not yet checked
        resp = client.get(f"{PREFIX}/golden-set.csv?genes=MYCN")
        assert resp.status_code == 422
        assert resp.json()["detail"] == {"not_yet_checked": ["MYCN"]}

        # Both ERBB2 and MYCN: fails with 422, emitting no file
        resp_both = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2,MYCN")
        assert resp_both.status_code == 422
        assert resp_both.json()["detail"] == {"not_yet_checked": ["MYCN"]}


def test_golden_set_unsurfaced_pairs_never_appear_in_cancer_types_or_pmids():
    def _edit_esca(doc):
        if doc["gene"]["hugo_symbol"] == "ERBB2":
            for ct in doc["cancer_types"]:
                if ct["oncotree_code"] == "ESCA":
                    ct["literature_precheck_pass"] = False

    bundle_a = build_bundle(("ERBB2",), release_id="release-a", edit_gene=_edit_esca)
    erbb2_doc = dict(gene_file("ERBB2"))
    # Edit BRCA to fail stats in bundle B
    brca_failing = dict(erbb2_doc["cancer_types"][0])
    brca_failing["stats_pass"] = False
    brca_failing["q_value"] = 0.5
    bundle_b = build_bundle(
        release_id="release-b",
        edit_gene=lambda doc: doc.update({"cancer_types": [brca_failing]})
        if doc["gene"]["hugo_symbol"] == "ERBB2"
        else None,
    )
    harness = CsvHarness(bundle=bundle_a)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])

    with TestClient(harness.app) as client:
        # 1. Stored supported evidence for release-a
        harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="release-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=base_stats,
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )

        # 2. Broad-blocked lineage pair with stored supported evidence
        harness.store.rows[("ERBB2", "LUAD", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="LUAD",
            release_id="release-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=False,
            exclusion_reason="stats",
            stats={**base_stats, "oncotree_code": "LUAD"},
            evidence=_v2_supported_evidence("22222222", "lung cancer"),
            computed_at=now,
        )

        # 3. Old-rule (acgc_pipeline_v1) verdict
        harness.store.rows[("ERBB2", "STAD", "release-a", "acgc_pipeline_v1")] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="STAD",
            release_id="release-a",
            literature_gate="acgc_pipeline_v1",
            surfaced=True,
            stats={**base_stats, "oncotree_code": "STAD"},
            evidence={"literature_gate": "acgc_pipeline_v1", "pmids": ["33333333"], "supported": True},
            computed_at=now,
        )

        # 4. Fresh false precheck pair with stored supported evidence
        harness.store.rows[("ERBB2", "ESCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="ESCA",
            release_id="release-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=False,
            exclusion_reason="precheck",
            stats={**base_stats, "oncotree_code": "ESCA", "literature_precheck_pass": False},
            evidence=_v2_supported_evidence("44444444", "esophageal cancer"),
            computed_at=now,
        )

        # On release-a: only BRCA is surfaced
        resp = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp.status_code == 200
        _, rows = parse_csv_response(resp.text)
        assert len(rows) == 1
        row = rows[0]
        assert row["Relevant expression-based cancer type(s)"] == "Breast cancer"
        assert row["Relevant alteration type(s)"] == "Expression"
        assert row["Evidence-associated PMIDs"] == "12345678"
        for bad in ("LUAD", "Lung", "22222222", "STAD", "Stomach", "33333333", "ESCA", "Esophageal", "44444444"):
            assert bad not in row["Relevant expression-based cancer type(s)"]
            assert bad not in row["Evidence-associated PMIDs"]

        # Now activate stats-failing release B: release A evidence must NEVER surface under B
        harness.store.rows[("ERBB2", "BRCA", "release-b", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id="release-b",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=False,
            exclusion_reason="stats",
            stats=brca_failing,
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )
        asyncio.run(seed_bundle(harness.bundles, bundle_b))
        harness.service.catalog.invalidate()

        resp_b = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp_b.status_code == 200
        _, rows_b = parse_csv_response(resp_b.text)
        assert len(rows_b) == 1
        row_b = rows_b[0]
        assert row_b["Genes"] == "ERBB2"
        assert row_b["Relevant expression-based cancer type(s)"] == ""
        assert row_b["Relevant alteration type(s)"] == ""
        assert row_b["Evidence-associated PMIDs"] == ""


def test_golden_set_agreement_with_all_read_paths():
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (False, ["23456789"]),
        }
    )
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(gate=gate, bundle=bundle)
    with TestClient(harness.app) as client:
        # Run job to evaluate ERBB2
        run_job(client, "ERBB2")

        # Per-pair CSV surfaced set
        _, pair_rows = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
        pair_surfaced_codes = {r["oncotree_code"] for r in pair_rows if r["surfaced"] == "yes"}
        pair_pmids = set()
        for r in pair_rows:
            if r["surfaced"] == "yes" and r["tumor_specific_pmids"]:
                for p in r["tumor_specific_pmids"].split(";"):
                    if p.strip():
                        pair_pmids.add(p.strip())

        assert pair_surfaced_codes == {"BRCA", "STAD"}

        # Golden set row
        resp = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp.status_code == 200
        _, golden_rows = parse_csv_response(resp.text)
        assert len(golden_rows) == 1
        golden_row = golden_rows[0]

        name_map = load_sheet_cancer_names()
        # Mapped names in order of cohort code
        expected_cancer_names = f"{name_map['BRCA']}; {name_map['STAD']}"
        assert golden_row["Relevant expression-based cancer type(s)"] == expected_cancer_names
        assert golden_row["Relevant alteration type(s)"] == "Expression"
        assert golden_row["Evidence-associated PMIDs"] == "12345678; 23456789"
        assert set(golden_row["Evidence-associated PMIDs"].split("; ")) == pair_pmids


def test_golden_set_multi_cohort_coadread_pmid_numeric_sorting_and_dedupe():
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    coadread_stats = dict(base_stats)
    coadread_stats["oncotree_code"] = "COADREAD"
    bundle = build_bundle(
        ("ERBB2",),
        edit_gene=lambda doc: doc.update({
            "cancer_types": doc["cancer_types"] + [coadread_stats]
        }) if doc["gene"]["hugo_symbol"] == "ERBB2" else None,
    )
    harness = CsvHarness(bundle=bundle)

    with TestClient(harness.app) as client:
        # Surface BRCA with PMIDs 30000002 and 12345678
        ev_brca = _v2_supported_evidence("30000002", "breast cancer")
        ev_brca["pmids"] = ["30000002", "12345678"]
        ev_brca["papers"] = [
            {"pmid": "30000002", "tumor_specific": True, "reason": "Confirmed in cohort."},
            {"pmid": "12345678", "tumor_specific": True, "reason": "Confirmed in cohort."},
        ]
        harness.store.rows[("ERBB2", "BRCA", harness.bundle.release_id, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=harness.bundle.release_id,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats={**base_stats, "oncotree_code": "BRCA"},
            evidence=ev_brca,
            computed_at=now,
        )
        # Surface COADREAD with PMID 2 and duplicate 12345678
        ev_coad = _v2_supported_evidence("2", "colorectal cancer")
        ev_coad["pmids"] = ["2", "12345678"]
        ev_coad["papers"] = [
            {"pmid": "2", "tumor_specific": True, "reason": "Confirmed in cohort."},
            {"pmid": "12345678", "tumor_specific": True, "reason": "Confirmed in cohort."},
        ]
        harness.store.rows[("ERBB2", "COADREAD", harness.bundle.release_id, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="COADREAD",
            release_id=harness.bundle.release_id,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats={**base_stats, "oncotree_code": "COADREAD"},
            evidence=ev_coad,
            computed_at=now,
        )
        # Surface STAD with PMID 30000001
        harness.store.rows[("ERBB2", "STAD", harness.bundle.release_id, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="STAD",
            release_id=harness.bundle.release_id,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats={**base_stats, "oncotree_code": "STAD"},
            evidence=_v2_supported_evidence("30000001", "stomach cancer"),
            computed_at=now,
        )

        resp = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp.status_code == 200
        _, rows = parse_csv_response(resp.text)
        assert len(rows) == 1
        row = rows[0]

        # COADREAD must map to "Colorectal cancer" and be ordered by cohort code (BRCA -> COADREAD -> STAD)
        assert row["Relevant expression-based cancer type(s)"] == "Breast cancer; Colorectal cancer; Stomach adenocarcinoma"
        # PMIDs must be deduped (12345678 appears once) and numerically sorted: 2, 12345678, 30000001, 30000002
        assert row["Evidence-associated PMIDs"] == "2; 12345678; 30000001; 30000002"


def test_golden_set_alias_resolution_and_validation_errors():
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(bundle=bundle)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    # Store results for ERBB2 and MYCN
    harness.store.rows[("ERBB2", "BRCA", harness.bundle.release_id, TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id=harness.bundle.release_id,
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=base_stats,
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )
    harness.store.rows[("MYCN", base_stats["oncotree_code"], harness.bundle.release_id, TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="MYCN",
        oncotree_code=base_stats["oncotree_code"],
        release_id=harness.bundle.release_id,
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=False,
        exclusion_reason="literature",
        stats=base_stats,
        evidence={"literature_gate": TUMOR_SPECIFIC_GATE, "pmids": [], "supported": False, "verdict": "insufficient_evidence", "papers": []},
        computed_at=now,
    )
    with TestClient(harness.app) as client:
        # HER2 resolves to ERBB2
        resp = client.get(f"{PREFIX}/golden-set.csv?genes=HER2")
        assert resp.status_code == 200
        _, rows = parse_csv_response(resp.text)
        assert len(rows) == 1
        assert rows[0]["Genes"] == "ERBB2"

        # Deduplication case 1: HER2,ERBB2 (alias duplicate) -> 1 row
        resp_dedupe_alias = client.get(f"{PREFIX}/golden-set.csv?genes=HER2,ERBB2")
        assert resp_dedupe_alias.status_code == 200
        _, rows_alias = parse_csv_response(resp_dedupe_alias.text)
        assert len(rows_alias) == 1
        assert rows_alias[0]["Genes"] == "ERBB2"

        # Deduplication case 2: ERBB2,ERBB2 (exact symbol duplicate) -> 1 row
        resp_dedupe_exact = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2,ERBB2")
        assert resp_dedupe_exact.status_code == 200
        _, rows_exact = parse_csv_response(resp_dedupe_exact.text)
        assert len(rows_exact) == 1
        assert rows_exact[0]["Genes"] == "ERBB2"

        # Deduplication preserves first-seen order: MYCN,HER2,ERBB2 -> MYCN, ERBB2
        resp_order = client.get(f"{PREFIX}/golden-set.csv?genes=MYCN,HER2,ERBB2")
        assert resp_order.status_code == 200
        _, rows_order = parse_csv_response(resp_order.text)
        assert [r["Genes"] for r in rows_order] == ["MYCN", "ERBB2"]

        # Unknown gene -> 422 listing the bad name
        resp_unk = client.get(f"{PREFIX}/golden-set.csv?genes=HER2,UNKNOWN1")
        assert resp_unk.status_code == 422
        assert "UNKNOWN1" in resp_unk.json()["detail"]

        # Multiple unknown genes -> 422 listing all bad names
        resp_multi_unk = client.get(f"{PREFIX}/golden-set.csv?genes=FOO1,BAR2")
        assert resp_multi_unk.status_code == 422
        detail = resp_multi_unk.json()["detail"]
        assert "FOO1" in detail and "BAR2" in detail

        # More than 500 genes -> 422 with raw genes count message
        many_genes = ",".join([f"GENE{i}" for i in range(501)])
        resp_too_many = client.get(f"{PREFIX}/golden-set.csv?genes={many_genes}")
        assert resp_too_many.status_code == 422
        assert "500" in resp_too_many.json()["detail"]
        assert "raw" in resp_too_many.json()["detail"].lower()

        # Empty genes string -> 422
        resp_empty = client.get(f"{PREFIX}/golden-set.csv?genes=   ")
        assert resp_empty.status_code == 422

        # No active bundle -> 503 with Retry-After: 60
        harness.bundles.state = replace(harness.bundles.state, active_bundle_tag="nonexistent")
        harness.service.catalog.invalidate()
        resp_503 = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2")
        assert resp_503.status_code == 503
        assert resp_503.headers.get("Retry-After") == "60"

        resp_503_bulk = client.get(f"{PREFIX}/golden-set.csv")
        assert resp_503_bulk.status_code == 503
        assert resp_503_bulk.headers.get("Retry-After") == "60"


@pytest.mark.asyncio
async def test_golden_set_bulk_stream_and_supersession():
    bundle_a = build_bundle(("ERBB2",), release_id="release-a")
    bundle_b = build_bundle(("ERBB2",), release_id="release-b")
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)

    harness = CsvHarness(bundle=bundle_a)
    harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id="release-a",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=dict(gene_file("ERBB2")["cancer_types"][0]),
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )

    with TestClient(harness.app) as client:
        # Normal bulk export
        resp = client.get(f"{PREFIX}/golden-set.csv")
        assert resp.status_code == 200
        assert resp.headers["Content-Disposition"] == 'attachment; filename="expression-context_golden-set_release-a.csv"'
        header, rows = parse_csv_response(resp.text)
        assert len(header) == 4
        assert header == list(GOLDEN_SET_COLUMNS)
        assert len(rows) == 1
        assert rows[0]["Genes"] == "ERBB2"
        assert rows[0]["Relevant expression-based cancer type(s)"] == "Breast cancer"
        assert rows[0]["Relevant alteration type(s)"] == "Expression"
        assert rows[0]["Evidence-associated PMIDs"] == "12345678"

        # Supersession before first page
        _, stream = await harness.service.export_golden_set_bundle_csv(page_size=25)
        header_chunk = await anext(stream)
        assert header_chunk.startswith(UTF8_BOM)

        await seed_bundle(harness.bundles, bundle_b)
        harness.service.catalog.invalidate()

        emitted = []
        with pytest.raises(BundleGone):
            async for chunk in stream:
                emitted.append(chunk)
        for chunk in emitted:
            assert "Breast cancer" not in chunk

    # Supersession before final page (multi-page case: page_size=1 with 2 genes)
    harness2 = CsvHarness(bundle=build_bundle(("ERBB2", "MYCN"), release_id="release-a2"))
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    harness2.store.rows[("ERBB2", "BRCA", "release-a2", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id="release-a2",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=base_stats,
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )
    harness2.store.rows[("MYCN", "BRCA", "release-a2", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="MYCN",
        oncotree_code="BRCA",
        release_id="release-a2",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=base_stats,
        evidence=_v2_supported_evidence("87654321", "breast cancer"),
        computed_at=now,
    )
    with TestClient(harness2.app):
        _, stream2 = await harness2.service.export_golden_set_bundle_csv(page_size=1)
        header2 = await anext(stream2)
        assert header2.startswith(UTF8_BOM)
        first_page = await anext(stream2)
        assert len(first_page) > 0

        # Supersede before the final page
        await seed_bundle(harness2.bundles, bundle_b)
        harness2.service.catalog.invalidate()

        with pytest.raises(BundleGone):
            async for _ in stream2:
                pass


def test_golden_set_injection_guard():
    bundle = build_bundle(("ERBB2",))
    harness = CsvHarness(bundle=bundle)
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        # Test dangerous prefixes in hugo_symbol
        view_dangerous = view.model_copy(update={"hugo_symbol": "=HER2"})
        row = golden_set_row_from_view(view_dangerous)
        assert len(row) == 4
        assert row[0] == "'=HER2"

        view_plus = view.model_copy(update={"hugo_symbol": "+HER2"})
        row_plus = golden_set_row_from_view(view_plus)
        assert len(row_plus) == 4
        assert row_plus[0] == "'+HER2"

        # Injection guard on cancer-name cell
        surfaced_pair = view.pairs[0].model_copy(update={"status": "surfaced", "oncotree_code": "BRCA"})
        view_surfaced = view.model_copy(update={"pairs": [surfaced_pair]})
        name_map_injected = {"BRCA": "=Breast cancer"}
        row_cancer = golden_set_row_from_view(view_surfaced, name_map=name_map_injected)
        assert len(row_cancer) == 4
        assert row_cancer[1] == "'=Breast cancer"

        # Injection guard on alteration-type cell
        row_alteration = golden_set_row_from_view(view_surfaced)
        assert len(row_alteration) == 4
        assert row_alteration[2] == "Expression"

        # Injection guard on PMID cell
        ev_injected = LiteratureVerdict.model_validate(_v2_supported_evidence("=12345678", "breast cancer"))
        pair_injected_pmid = view.pairs[0].model_copy(update={"status": "surfaced", "oncotree_code": "BRCA", "literature": ev_injected})
        view_injected_pmid = view.model_copy(update={"pairs": [pair_injected_pmid]})
        row_pmid = golden_set_row_from_view(view_injected_pmid)
        assert len(row_pmid) == 4
        assert row_pmid[3] == "'=12345678"


def test_golden_set_genes_supersession_between_genes_returns_503():
    bundle_a = build_bundle(("ERBB2", "MYCN"), release_id="rel-a")
    bundle_b = build_bundle(("ERBB2", "MYCN"), release_id="rel-b")
    harness = CsvHarness(bundle=bundle_a)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    for g in ("ERBB2", "MYCN"):
        harness.store.rows[(g, "BRCA", "rel-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol=g,
            oncotree_code="BRCA",
            release_id="rel-a",
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=base_stats,
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )

    with TestClient(harness.app) as client:
        orig_gene_view = harness.service.gene_view
        first_call = True

        async def side_effect_gene_view(gene, *, resolved=None):
            nonlocal first_call
            result = await orig_gene_view(gene, resolved=resolved)
            if first_call:
                first_call = False
                # Supersede bundle between genes
                await seed_bundle(harness.bundles, bundle_b)
                harness.service.catalog.invalidate()
            return result

        harness.service.gene_view = side_effect_gene_view

        resp = client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2,MYCN")
        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") == "60"
        # No mixed or partial output emitted
        assert "text/csv" not in resp.headers.get("Content-Type", "")


def test_bulk_export_pins_single_bundle_no_mix_on_race():
    bundle_a = build_bundle(
        ("ERBB2",),
        release_id="release-a",
        manifest_overrides={
            "cancer_types": [
                {
                    "oncotree_code": "BRCA",
                    "study_id": "study_a_brca",
                    "cancer_type_name": "Breast Cancer",
                    "name": "Breast Cancer",
                    "tumor_type_query": "breast cancer",
                },
            ]
        },
    )
    bundle_b = build_bundle(
        ("ERBB2",),
        release_id="release-b",
        manifest_overrides={
            "cancer_types": [
                {
                    "oncotree_code": "BRCA",
                    "study_id": "study_b_brca",
                    "cancer_type_name": "Breast Cancer",
                    "name": "Breast Cancer",
                    "tumor_type_query": "breast cancer",
                },
            ]
        },
    )
    harness = CsvHarness(bundle=bundle_a)
    now = datetime(2026, 11, 6, 12, 0, tzinfo=timezone.utc)
    base_stats = dict(gene_file("ERBB2")["cancer_types"][0])
    harness.store.rows[("ERBB2", "BRCA", "release-a", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id="release-a",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=base_stats,
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )
    harness.store.rows[("ERBB2", "BRCA", "release-b", TUMOR_SPECIFIC_GATE)] = StoredPair(
        hugo_symbol="ERBB2",
        oncotree_code="BRCA",
        release_id="release-b",
        literature_gate=TUMOR_SPECIFIC_GATE,
        surfaced=True,
        stats=base_stats,
        evidence=_v2_supported_evidence("12345678", "breast cancer"),
        computed_at=now,
    )

    with TestClient(harness.app) as client:
        asyncio.run(seed_bundle(harness.bundles, bundle_b))
        record_a = asyncio.run(harness.bundles.get_bundle(bundle_a.tag))
        record_b = asyncio.run(harness.bundles.get_bundle(bundle_b.tag))
        active_a = ActiveBundle(record_a, BundleManifest.model_validate(bundle_a.manifest))
        active_b = ActiveBundle(record_b, BundleManifest.model_validate(bundle_b.manifest))

        calls = 0

        async def switching_active():
            nonlocal calls
            calls += 1
            if calls == 1:
                return active_a
            return active_b

        harness.service.catalog.active = switching_active

        resp = client.get(f"{PREFIX}/export.csv")
        assert resp.status_code == 503
