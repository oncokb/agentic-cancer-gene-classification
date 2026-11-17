"""Tests for expression-context batches, gene-grouped exports, curator sheet, and CLI."""

from __future__ import annotations

import asyncio
import copy
import csv
import io
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Tuple

import httpx
import pytest
from fastapi.testclient import TestClient

from scripts.expression_context_batch import main as cli_main
from scripts.expression_context_batch import run_batch_cli
from src.auth import AuthenticatedUser
from src.expression_context.bundle_store import BundleState, InMemoryBundleStore
from src.expression_context.catalog import StatsCatalog
from src.expression_context.csv_export import (
    CURATOR_SHEET_COLUMNS,
    GOLDEN_SET_COLUMNS,
    UTF8_BOM,
    msk_target_cancer_types,
)
from src.expression_context.lease import VerdictLease
from src.expression_context.literature_gate import TUMOR_SPECIFIC_GATE
from src.expression_context.models import (
    BatchCounts,
    BatchGeneRecord,
    BatchRecord,
    JobRecord,
    LiteratureVerdict,
    StoredPair,
    TumorContext,
)
from src.expression_context.service import BundleGone, ExpressionContextService
from src.expression_context.store import InMemoryExpressionContextStore
from src.job_store import JobStore
from tests.expression_context_helpers import (
    RELEASE_ID,
    REPO,
    FakeLiteratureGate,
    LeaseFakeRedis,
    build_bundle,
    gene_file,
    module_redis,
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

BOB = AuthenticatedUser(email="bob@example.org", name="Bob", domain="example.org")
SAME_ORIGIN = {"sec-fetch-site": "same-origin"}
ALTERATIONS_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/expression_context/alterations.json").read_text()
)


def wait_for_batch(client: TestClient, batch_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"{PREFIX}/batches/{batch_id}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        if data["status"] in ("done", "failed", "cancelled"):
            return data
        time.sleep(0.01)
    raise AssertionError(f"Batch {batch_id} did not finish within {timeout}s")


def parse_raw_csv_rows(text: str) -> Tuple[List[str], List[List[str]]]:
    assert text.startswith(UTF8_BOM), "CSV must start with UTF-8 BOM"
    reader = csv.reader(io.StringIO(text[len(UTF8_BOM) :]))
    rows = list(reader)
    assert rows, "CSV must have at least a header row"
    return rows[0], rows[1:]


# ---------------------------------------------------------------------------
# 1. Batch lifecycle tests
# ---------------------------------------------------------------------------


def test_batch_submit_dedupe_alias_not_found_skip_no_stats_skip_fresh_and_run():
    def edit_genes(doc):
        sym = doc["gene"]["hugo_symbol"]
        if sym == "MYCN":
            # Make all MYCN pairs fail stats so MYCN is skipped_no_stats
            for ct in doc["cancer_types"]:
                ct["stats_pass"] = False
                ct["q_value"] = 0.8

    bundle = build_bundle(("ERBB2", "MYCN"), edit_gene=edit_genes)
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (True, []),
            "esophageal cancer": (True, []),
        }
    )
    harness = CsvHarness(gate=gate, bundle=bundle)

    with TestClient(harness.app) as client:
        # Submit batch: HER2 (alias -> ERBB2), ERBB2 (duplicate of row 1), MYCN (no stats), UNKNOWN1 (not_found)
        resp = client.post(
            f"{PREFIX}/batches",
            json={"genes": ["HER2", "ERBB2", "MYCN", "UNKNOWN1", "UNKNOWN1"]},
            headers=SAME_ORIGIN,
        )
        assert resp.status_code == 202, resp.text
        accepted = resp.json()
        batch_id = accepted["batch_id"]
        assert accepted["status_url"] == f"{PREFIX}/batches/{batch_id}"

        detail = wait_for_batch(client, batch_id)
        assert detail["status"] == "done"
        assert detail["release_id"] == RELEASE_ID
        assert detail["counts"] == {
            "total": 3,
            "queued": 0,
            "running": 0,
            "done": 1,
            "skipped_no_stats": 1,
            "skipped_fresh": 0,
            "failed": 0,
            "not_found": 1,
        }
        genes = detail["genes"]
        assert [g["position"] for g in genes] == [0, 1, 2]
        assert genes[0]["input_symbol"] == "HER2"
        assert genes[0]["hugo_symbol"] == "ERBB2"
        assert genes[0]["status"] == "done"
        assert "duplicate of row 1" in (genes[0]["notes"] or "")
        assert genes[1]["input_symbol"] == "MYCN"
        assert genes[1]["status"] == "skipped_no_stats"
        assert genes[2]["input_symbol"] == "UNKNOWN1"
        assert genes[2]["hugo_symbol"] is None
        assert genes[2]["status"] == "not_found"
        assert "duplicate of row 3" in (genes[2]["notes"] or "")

        # MYCN made zero gate calls; ERBB2 made gate calls
        assert all(sym == "ERBB2" for sym, _ in gate.calls)
        calls_count = len(gate.calls)
        assert calls_count == 3

        # Submitting a second batch with ERBB2 reuses fresh verdicts -> skipped_fresh, 0 new model calls
        resp2 = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2"]}, headers=SAME_ORIGIN)
        assert resp2.status_code == 202
        detail2 = wait_for_batch(client, resp2.json()["batch_id"])
        assert detail2["status"] == "done"
        assert detail2["counts"]["skipped_fresh"] == 1
        assert detail2["genes"][0]["status"] == "skipped_fresh"
        assert len(gate.calls) == calls_count, "skipped_fresh must make no model calls"

        # Listing recent batches returns caller's batches newest first
        recent = client.get(f"{PREFIX}/batches").json()
        assert [b["batch_id"] for b in recent["items"]] == [
            detail2["batch_id"],
            batch_id,
        ]

        # Pagination on GET /batches/{id}
        page1 = client.get(
            f"{PREFIX}/batches/{batch_id}", params={"offset": 1, "limit": 1}
        ).json()
        assert page1["total_genes"] == 3
        assert len(page1["genes"]) == 1
        assert page1["genes"][0]["input_symbol"] == "MYCN"


def test_batch_failure_recorded_per_gene_and_batch_continues():
    def add_egfr(doc):
        pass

    erbb2_doc = gene_file("ERBB2")
    egfr_doc = copy.deepcopy(erbb2_doc)
    egfr_doc["gene"] = {"hugo_symbol": "EGFR", "entrez_gene_id": 1956}

    bundle = build_bundle(("ERBB2", "MYCN"))
    # Replace MYCN with a copy of ERBB2 named EGFR so both have eligible stats_pass pairs
    bundle.genes[1] = egfr_doc
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (False, ["23456789"]),
            "esophageal cancer": (False, ["34567890"]),
        }
    )

    class FailOnFirstGeneGate(FakeLiteratureGate):
        async def evaluate(self, hugo_symbol: str, tumor: TumorContext) -> LiteratureVerdict:
            if hugo_symbol == "ERBB2":
                raise RuntimeError("transient LLM failure on ERBB2")
            return await super().evaluate(hugo_symbol, tumor)

    fail_gate = FailOnFirstGeneGate(verdicts=gate.verdicts)
    harness = CsvHarness(gate=fail_gate, bundle=bundle)

    with TestClient(harness.app) as client:
        resp = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2", "EGFR"]}, headers=SAME_ORIGIN)
        assert resp.status_code == 202
        detail = wait_for_batch(client, resp.json()["batch_id"])
        assert detail["status"] == "done"
        assert detail["counts"]["failed"] == 1
        assert detail["counts"]["done"] == 1
        assert detail["genes"][0]["hugo_symbol"] == "ERBB2"
        assert detail["genes"][0]["status"] == "failed"
        assert "transient LLM failure" in (detail["genes"][0]["error"] or "")
        assert detail["genes"][1]["hugo_symbol"] == "EGFR"
        assert detail["genes"][1]["status"] == "done"


@pytest.mark.asyncio
async def test_batch_cancel_resume_single_pod_lease_and_global_concurrency_limit():
    erbb2_doc = gene_file("ERBB2")
    genes_list = []
    for idx, sym in enumerate(("ERBB2", "EGFR", "KRAS", "MET"), start=1):
        d = copy.deepcopy(erbb2_doc)
        d["gene"] = {"hugo_symbol": sym, "entrez_gene_id": 1000 + idx}
        d["cancer_types"] = [dict(erbb2_doc["cancer_types"][0])]  # 1 eligible pair (BRCA) each
        genes_list.append(d)

    base_bundle = build_bundle(("ERBB2",))
    base_bundle.genes.clear()
    base_bundle.genes.extend(genes_list)
    base_bundle.manifest["gene_count"] = len(genes_list)

    active_evaluations = 0
    max_observed_concurrency = 0
    gate_barrier = asyncio.Event()

    class SlowConcurrencyGate(FakeLiteratureGate):
        async def evaluate(self, hugo_symbol: str, tumor: TumorContext) -> LiteratureVerdict:
            nonlocal active_evaluations, max_observed_concurrency
            active_evaluations += 1
            max_observed_concurrency = max(max_observed_concurrency, active_evaluations)
            try:
                await gate_barrier.wait()
                await asyncio.sleep(0.02)
                return await super().evaluate(hugo_symbol, tumor)
            finally:
                active_evaluations -= 1

    store = InMemoryExpressionContextStore()
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, base_bundle)
    redis = LeaseFakeRedis()
    gate = SlowConcurrencyGate(verdicts={"breast cancer": (False, ["12345678"])})

    # Pod 1 with literature_concurrency=2
    pod1 = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        literature_concurrency=2,
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
        lease_poll_seconds=0.01,
        batch_lock_ttl_seconds=5.0,
    )
    # Pod 2 sharing the same store and redis
    pod2 = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        literature_concurrency=2,
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
        lease_poll_seconds=0.01,
        batch_lock_ttl_seconds=5.0,
    )

    try:
        # 1. Submit batch on pod1 while gate_barrier is closed
        batch = await pod1.submit_batch(["ERBB2", "EGFR"], owner="alice@example.org")
        await asyncio.sleep(0.03)

        # Pod 2 attempts to run the same batch -> blocked by single-pod batch lease
        await pod2._run_batch(batch.batch_id)
        detail_mid = await pod1.get_batch_detail(batch.batch_id)
        assert detail_mid.status == "running"

        # Interactive single-gene lookup for KRAS and MET concurrently with batch
        gate_barrier.set()
        job1 = await pod1.submit("KRAS", owner="alice@example.org")
        job2 = await pod1.submit("MET", owner="alice@example.org")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            d = await pod1.get_batch_detail(batch.batch_id)
            j1 = await pod1.jobs.get(job1.job_id)
            j2 = await pod1.jobs.get(job2.job_id)
            if (
                d.status == "done"
                and j1 is not None
                and j1.status == "succeeded"
                and j2 is not None
                and j2.status == "succeeded"
            ):
                break
            await asyncio.sleep(0.01)

        assert (await pod1.get_batch_detail(batch.batch_id)).status == "done"
        assert max_observed_concurrency <= 2, f"Concurrency exceeded limit 2: {max_observed_concurrency}"

        # 2. Test cancellation
        gate_barrier.clear()
        store2 = InMemoryExpressionContextStore()
        pod_cancel = ExpressionContextService(
            catalog=StatsCatalog(bundles, cache_seconds=0),
            gate=gate,
            store=store2,
            jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
            literature_concurrency=1,
        )
        b_cancel = await pod_cancel.submit_batch(["ERBB2", "EGFR"], owner="alice@example.org")
        await asyncio.sleep(0.02)
        cancelled = await pod_cancel.cancel_batch(b_cancel.batch_id)
        assert cancelled.status == "cancelled"
        assert cancelled.counts.queued == 0 and cancelled.counts.running == 0
        await pod_cancel.aclose()

        # 3. Test resume after simulated restart
        gate_barrier.set()
        now = datetime.now(timezone.utc)
        unfinished = BatchRecord(
            batch_id="resume-batch-1",
            created_by="alice@example.org",
            created_at=now,
            release_id=RELEASE_ID,
            status="running",
            counts=BatchCounts(total=2, done=1, queued=1),
        )
        await store2.create_batch(
            unfinished,
            [
                BatchGeneRecord(
                    batch_id="resume-batch-1",
                    position=0,
                    input_symbol="ERBB2",
                    hugo_symbol="ERBB2",
                    status="done",
                    updated_at=now,
                ),
                BatchGeneRecord(
                    batch_id="resume-batch-1",
                    position=1,
                    input_symbol="EGFR",
                    hugo_symbol="EGFR",
                    status="queued",
                    updated_at=now,
                ),
            ],
        )
        pod_resumed = ExpressionContextService(
            catalog=StatsCatalog(bundles, cache_seconds=0),
            gate=gate,
            store=store2,
            jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
            literature_concurrency=2,
        )
        pod_resumed.start()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            d_res = await pod_resumed.get_batch_detail("resume-batch-1")
            if d_res.status == "done":
                break
            await asyncio.sleep(0.01)
        d_res = await pod_resumed.get_batch_detail("resume-batch-1")
        assert d_res.status == "done"
        assert d_res.counts.done == 2
        await pod_resumed.aclose()
    finally:
        await pod1.aclose()
        await pod2.aclose()


# ---------------------------------------------------------------------------
# 2. Gene-grouped exports & Curator sheet rules
# ---------------------------------------------------------------------------


def test_exports_grouped_in_batch_order_curator_sheet_rules_and_single_gene_parity():
    def edit_genes(doc):
        sym = doc["gene"]["hugo_symbol"]
        if sym == "ERBB2":
            # BRCA: Strong, CNA amp + high-expression associated, SV enriched
            # STAD: Moderate (candidate tier), CNA amp (frequency only)
            # LUAD: stats_pass=False, Point Mutation enriched + high-expression associated
            # KIRC: purity-demoted high tier (must never surface)
            brca = dict(doc["cancer_types"][0])
            brca["oncotree_code"] = "BRCA"
            brca["stats_pass"] = True
            brca["confidence_tier"] = "high"
            brca["purity_check"] = "pass"
            brca["alterations"] = {
                "alteration_types_enriched": ["Copy Number Alteration", "Structural Variant"],
                "categories": {
                    "Copy Number Alteration": {
                        "status": "enriched",
                        "direction": "amp",
                        "enriched_directions": ["amp"],
                    },
                    "Structural Variant": {"status": "enriched"},
                },
            }
            brca["alteration_high_expression"] = {
                "Copy Number Alteration (amp)": {"status": "associated_high_expression"},
                "Structural Variant": {"status": "anecdotal"},
            }

            stad = dict(doc["cancer_types"][1])
            stad["oncotree_code"] = "STAD"
            stad["stats_pass"] = True
            stad["confidence_tier"] = "candidate"
            stad["purity_check"] = None
            stad["alterations"] = {
                "alteration_types_enriched": ["Copy Number Alteration"],
                "categories": {
                    "Copy Number Alteration": {
                        "status": "enriched",
                        "direction": "amp",
                        "enriched_directions": ["amp"],
                    }
                },
            }

            luad = dict(doc["cancer_types"][4])
            luad["oncotree_code"] = "LUAD"
            luad["stats_pass"] = False
            luad["alterations"] = {
                "alteration_types_enriched": ["Point Mutation"],
                "categories": {
                    "Point Mutation": {"status": "enriched"},
                },
            }
            luad["alteration_high_expression"] = {
                "Point Mutation": {"status": "associated_high_expression"},
            }

            kirc = dict(brca)
            kirc["oncotree_code"] = "KIRC"
            kirc["stats_pass"] = True
            kirc["confidence_tier"] = "high"
            kirc["purity_check"] = "demoted"
            kirc["alterations"] = {}
            kirc["alteration_high_expression"] = {}

            doc["cancer_types"] = [brca, stad, luad, kirc]
        elif sym == "MYCN":
            # MYCN: LGG surfaced (Moderate), UCEC pending
            lgg = dict(doc["cancer_types"][0])
            lgg["oncotree_code"] = "LGG"
            lgg["stats_pass"] = True
            lgg["confidence_tier"] = "candidate"
            lgg["literature_precheck_pass"] = True
            lgg["alterations"] = {
                "alteration_types_enriched": ["Copy Number Alteration"],
                "categories": {
                    "Copy Number Alteration": {
                        "status": "enriched",
                        "direction": "del",
                        "enriched_directions": ["del"],
                    }
                },
            }
            doc["cancer_types"] = [lgg]

    manifest_cts = [
        {"study_id": "brca_tcga", "oncotree_code": "BRCA", "name": "Breast Invasive Carcinoma", "tumor_type_query": "breast cancer"},
        {"study_id": "stad_tcga", "oncotree_code": "STAD", "name": "Stomach Adenocarcinoma", "tumor_type_query": "gastric cancer"},
        {"study_id": "luad_tcga", "oncotree_code": "LUAD", "name": "Lung Adenocarcinoma", "tumor_type_query": "lung cancer"},
        {"study_id": "kirc_tcga", "oncotree_code": "KIRC", "name": "Kidney Clear Cell", "tumor_type_query": "kidney cancer"},
        {"study_id": "lgg_tcga", "oncotree_code": "LGG", "name": "Lower Grade Glioma", "tumor_type_query": "glioma"},
    ]
    bundle = build_bundle(
        ("ERBB2", "MYCN"),
        edit_gene=edit_genes,
        manifest_overrides={"cancer_types": manifest_cts, "contract_version": "1.6"},
    )
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["11111111"]),
            "gastric cancer": (False, ["22222222"]),
            "glioma": (False, ["33333333"]),
        }
    )
    harness = CsvHarness(gate=gate, bundle=bundle)
    now = datetime.now(timezone.utc)

    with TestClient(harness.app) as client:
        # Also seed stored supported evidence on stats-failing LUAD and purity-demoted KIRC
        # to prove neither ever surfaces in golden-set or curator-sheet Expression column.
        for code, query in (("LUAD", "lung cancer"), ("KIRC", "kidney cancer")):
            harness.store.rows[("ERBB2", code, RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
                hugo_symbol="ERBB2",
                oncotree_code=code,
                release_id=RELEASE_ID,
                literature_gate=TUMOR_SPECIFIC_GATE,
                surfaced=True,
                exclusion_reason=None,
                stats={"oncotree_code": code, "n": 100, "n_rest": 1000, "stats_pass": code == "KIRC"},
                evidence=_v2_supported_evidence("99999999", query),
                computed_at=now,
            )

        # Submit batch in deliberate order: MYCN first, then HER2 (alias for ERBB2), then UNKNOWN_GENE, then ERBB2 (duplicate)
        resp = client.post(
            f"{PREFIX}/batches",
            json={"genes": ["MYCN", "HER2", "UNKNOWN_GENE", "ERBB2"]},
            headers=SAME_ORIGIN,
        )
        assert resp.status_code == 202
        batch_id = resp.json()["batch_id"]
        wait_for_batch(client, batch_id)

        # 1) Batch Per-pair CSV (surfaced only & include_excluded=true)
        pair_resp = client.get(f"{PREFIX}/batches/{batch_id}/export.csv")
        assert pair_resp.status_code == 200
        _, batch_pair_rows = parse_csv_response(pair_resp.text)
        # Grouped by gene in batch order: MYCN (position 0), then ERBB2 (position 1)
        assert [r["gene_symbol"] for r in batch_pair_rows] == ["MYCN", "ERBB2", "ERBB2"]
        assert [(r["gene_symbol"], r["oncotree_code"]) for r in batch_pair_rows] == [
            ("MYCN", "LGG"),
            ("ERBB2", "BRCA"),
            ("ERBB2", "STAD"),
        ]

        # Per-pair CSV rows for each gene equal that gene's single-gene export
        _, single_mycn = parse_csv_response(client.get(f"{PREFIX}/genes/MYCN/export.csv").text)
        _, single_erbb2 = parse_csv_response(client.get(f"{PREFIX}/genes/HER2/export.csv").text)
        assert [r for r in batch_pair_rows if r["gene_symbol"] == "MYCN"] == single_mycn
        assert [r for r in batch_pair_rows if r["gene_symbol"] == "ERBB2"] == single_erbb2

        # With include_excluded=true: grouped by gene in batch order, then cohort
        pair_all_resp = client.get(
            f"{PREFIX}/batches/{batch_id}/export.csv", params={"include_excluded": "true"}
        )
        _, batch_all_rows = parse_csv_response(pair_all_resp.text)
        assert [r["gene_symbol"] for r in batch_all_rows] == [
            "MYCN",
            "ERBB2",
            "ERBB2",
            "ERBB2",
            "ERBB2",
        ]

        # 2) Batch Golden-set CSV
        golden_resp = client.get(f"{PREFIX}/batches/{batch_id}/golden-set.csv")
        assert golden_resp.status_code == 200
        golden_header, golden_rows = parse_csv_response(golden_resp.text)
        assert tuple(golden_header) == GOLDEN_SET_COLUMNS
        # Ordered by batch position; UNKNOWN_GENE (not_found) is omitted
        assert [r["Genes"] for r in golden_rows] == ["MYCN", "ERBB2"]
        assert golden_rows[0]["Relevant expression-based cancer type(s)"] == "Lower-grade glioma"
        assert (
            golden_rows[1]["Relevant expression-based cancer type(s)"]
            == "Breast cancer; Stomach adenocarcinoma"
        )
        # LUAD (stats_pass=False) and KIRC (purity-demoted) never surface
        assert "Lung" not in golden_rows[1]["Relevant expression-based cancer type(s)"]
        assert " Clear cell" not in golden_rows[1]["Relevant expression-based cancer type(s)"]

        # 3) Batch Curator sheet CSV
        curator_resp = client.get(f"{PREFIX}/batches/{batch_id}/curator-sheet.csv")
        assert curator_resp.status_code == 200
        curator_header, curator_rows = parse_csv_response(curator_resp.text)
        assert tuple(curator_header) == CURATOR_SHEET_COLUMNS
        assert [r["Gene"] for r in curator_rows] == ["MYCN", "ERBB2", "UNKNOWN_GENE"]

        mycn_row, erbb2_row, unk_row = curator_rows
        assert mycn_row["Expression - TCGA cancer types"] == "Lower-grade glioma (Moderate)"
        assert mycn_row["CNAs - TCGA cancer types"] == "Lower-grade glioma (del)"

        assert (
            erbb2_row["Expression - TCGA cancer types"]
            == "Breast cancer (Strong); Stomach adenocarcinoma (Moderate)"
        )
        # Stripping (Strong)/(Moderate) suffixes from curator sheet equals golden-set column!
        stripped_exp = "; ".join(
            part.rsplit(" (", 1)[0]
            for part in erbb2_row["Expression - TCGA cancer types"].split("; ")
            if part
        )
        assert stripped_exp == golden_rows[1]["Relevant expression-based cancer type(s)"]

        # CNAs across ALL cohorts, with [high-expr] only where associated_high_expression
        assert (
            erbb2_row["CNAs - TCGA cancer types"]
            == "Breast cancer (amp) [high-expr]; Stomach adenocarcinoma (amp)"
        )
        # SVs: BRCA is enriched, high-expression is anecdotal -> no [high-expr]
        assert erbb2_row["SVs - TCGA cancer types"] == "Breast cancer"
        # Point mutations: LUAD is enriched even though expression stats_pass=False, and has [high-expr]
        assert (
            erbb2_row["Point mutations - TCGA cancer types"]
            == "Lung adenocarcinoma [high-expr]"
        )
        # MSK-TARGET columns are empty via msk_target_cancer_types
        assert erbb2_row["Expression - MSK-TARGET cancer types"] == ""
        assert erbb2_row["SVs - MSK-TARGET cancer types"] == ""
        assert msk_target_cancer_types(None, "Expression") == []
        # Notes: queried as HER2 and duplicate of row 2
        assert "queried as HER2" in erbb2_row["Notes"]
        assert "duplicate of row 2" in erbb2_row["Notes"]

        # Unknown gene row in curator sheet
        assert unk_row["Gene"] == "UNKNOWN_GENE"
        assert unk_row["Expression - TCGA cancer types"] == ""
        assert unk_row["Notes"] == "not in expression data"

        # Single-gene curator sheet endpoint
        single_cur_resp = client.get(f"{PREFIX}/genes/HER2/curator-sheet.csv")
        assert single_cur_resp.status_code == 200
        _, single_cur_rows = parse_csv_response(single_cur_resp.text)
        assert len(single_cur_rows) == 1
        assert single_cur_rows[0]["Gene"] == "ERBB2"
        assert (
            single_cur_rows[0]["Expression - TCGA cancer types"]
            == erbb2_row["Expression - TCGA cancer types"]
        )
        assert "queried as HER2" in single_cur_rows[0]["Notes"]


def test_unchecked_and_partial_pending_notes_and_golden_set_batch_unchecked_row():
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(bundle=bundle)
    now = datetime.now(timezone.utc)

    # Seed a batch directly where ERBB2 is not checked at all, and MYCN has 1 checked pair and 1 pending pair
    with TestClient(harness.app) as client:
        # Check single-gene curator sheet before literature check -> "literature not checked"
        cur_unchecked = client.get(f"{PREFIX}/genes/ERBB2/curator-sheet.csv")
        assert cur_unchecked.status_code == 200
        _, rows_u = parse_csv_response(cur_unchecked.text)
        assert rows_u[0]["Notes"] == "literature not checked"

        # Seed 1 checked pair on ERBB2 so 2 eligible pairs remain pending
        brca_stats = dict(gene_file("ERBB2")["cancer_types"][0])
        harness.store.rows[("ERBB2", "BRCA", RELEASE_ID, TUMOR_SPECIFIC_GATE)] = StoredPair(
            hugo_symbol="ERBB2",
            oncotree_code="BRCA",
            release_id=RELEASE_ID,
            literature_gate=TUMOR_SPECIFIC_GATE,
            surfaced=True,
            stats=brca_stats,
            evidence=_v2_supported_evidence("12345678", "breast cancer"),
            computed_at=now,
        )
        cur_partial = client.get(f"{PREFIX}/genes/ERBB2/curator-sheet.csv")
        _, rows_p = parse_csv_response(cur_partial.text)
        assert "literature pending for 2 cancer types" in rows_p[0]["Notes"]

        # Create a batch record with MYCN unchecked and ERBB2 failed
        b_rec = BatchRecord(
            batch_id="b-unchecked",
            created_by="alice@example.org",
            created_at=now,
            release_id=RELEASE_ID,
            status="done",
            counts=BatchCounts(total=3, queued=1, failed=1, not_found=1),
        )
        asyncio.run(
            harness.store.create_batch(
                b_rec,
                [
                    BatchGeneRecord(
                        batch_id="b-unchecked",
                        position=0,
                        input_symbol="MYCN",
                        hugo_symbol="MYCN",
                        status="queued",
                        updated_at=now,
                    ),
                    BatchGeneRecord(
                        batch_id="b-unchecked",
                        position=1,
                        input_symbol="ERBB2",
                        hugo_symbol="ERBB2",
                        status="failed",
                        error="timeout",
                        updated_at=now,
                    ),
                    BatchGeneRecord(
                        batch_id="b-unchecked",
                        position=2,
                        input_symbol="NOPE",
                        hugo_symbol=None,
                        status="not_found",
                        error="not in expression data",
                        updated_at=now,
                    ),
                ],
            )
        )
        # Golden-set CSV for batch includes unchecked MYCN as a Genes-only row, and omits NOPE
        gs_resp = client.get(f"{PREFIX}/batches/b-unchecked/golden-set.csv")
        assert gs_resp.status_code == 200
        _, gs_rows = parse_csv_response(gs_resp.text)
        assert [r["Genes"] for r in gs_rows] == ["MYCN", "ERBB2"]
        assert gs_rows[0] == {
            "Genes": "MYCN",
            "Relevant expression-based cancer type(s)": "",
            "Relevant alteration type(s)": "",
            "Evidence-associated PMIDs": "",
        }

        # Curator sheet CSV for batch includes "literature not checked" for MYCN and "literature failed" for ERBB2
        cs_resp = client.get(f"{PREFIX}/batches/b-unchecked/curator-sheet.csv")
        _, cs_rows = parse_csv_response(cs_resp.text)
        assert [r["Gene"] for r in cs_rows] == ["MYCN", "ERBB2", "NOPE"]
        assert "literature not checked" in cs_rows[0]["Notes"]
        assert "literature failed" in cs_rows[1]["Notes"]
        assert "literature pending for 2 cancer types" in cs_rows[1]["Notes"]


def test_batch_exports_bundle_supersession_mid_stream_and_503_when_no_bundle():
    bundle_a = build_bundle(("ERBB2", "MYCN"), release_id="rel-a")
    harness = CsvHarness(bundle=bundle_a)

    with TestClient(harness.app) as client:
        resp = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2", "MYCN"]}, headers=SAME_ORIGIN)
        batch_id = resp.json()["batch_id"]
        wait_for_batch(client, batch_id)

        for export_path in (
            f"{PREFIX}/batches/{batch_id}/export.csv",
            f"{PREFIX}/batches/{batch_id}/golden-set.csv",
            f"{PREFIX}/batches/{batch_id}/curator-sheet.csv",
        ):
            harness.bundles.bundles.clear()
            harness.bundles.state = BundleState()
            harness.service.catalog.invalidate()
            r = client.get(export_path)
            assert r.status_code == 503, f"Expected 503 on supersession for {export_path}, got {r.status_code}"
            assert "text/csv" not in r.headers.get("content-type", "")
            asyncio.run(seed_bundle(harness.bundles, bundle_a))
            harness.service.catalog.invalidate()

    # 503 when no bundle is loaded
    unseeded = CsvHarness(seed=False)
    with TestClient(unseeded.app) as client:
        assert client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2"]}, headers=SAME_ORIGIN).status_code == 503
        assert client.get(f"{PREFIX}/batches").status_code == 503
        assert client.get(f"{PREFIX}/genes/ERBB2/curator-sheet.csv").status_code == 503


# ---------------------------------------------------------------------------
# 3. Extended Agreement Test (Single-gene paths unchanged with vs without batches)
# ---------------------------------------------------------------------------


def test_single_gene_read_paths_and_exports_unchanged_with_and_without_batches():
    bundle = build_bundle(("ERBB2", "MYCN"))
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (False, ["23456789"]),
        }
    )

    snapshots = []
    for use_batch in (False, True):
        harness = CsvHarness(gate=gate, bundle=bundle)
        with TestClient(harness.app) as client:
            if not use_batch:
                run_job(client, "ERBB2")
            else:
                b_resp = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2", "UNKNOWN"]}, headers=SAME_ORIGIN)
                wait_for_batch(client, b_resp.json()["batch_id"])

            stats = client.get(f"{PREFIX}/stats/ERBB2").json()
            genes = client.get(f"{PREFIX}/genes/ERBB2?include_excluded=true").json()
            history = client.get(f"{PREFIX}/genes").json()
            _, pair_rows = parse_csv_response(
                client.get(f"{PREFIX}/genes/ERBB2/export.csv?include_excluded=true").text
            )
            _, golden_rows = parse_csv_response(
                client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2").text
            )

            def strip_timestamps(obj):
                if isinstance(obj, dict):
                    return {
                        k: strip_timestamps(v)
                        for k, v in obj.items()
                        if k not in {"computed_at", "evaluated_at", "loaded_at", "literature_checked_at"}
                    }
                if isinstance(obj, list):
                    return [strip_timestamps(v) for v in obj]
                return obj

            snapshots.append(
                json.dumps(
                    strip_timestamps([stats, genes, history, pair_rows, golden_rows]),
                    sort_keys=True,
                )
            )

    assert snapshots[0] == snapshots[1]


# ---------------------------------------------------------------------------
# 4. Shared First-Column Gene Symbol Invariant Test Across ALL CSV Exports
# ---------------------------------------------------------------------------


def test_every_csv_export_endpoint_has_gene_symbol_first_column_and_non_empty_first_cells():
    bundle = build_bundle(("ERBB2", "MYCN"))
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
        }
    )
    harness = CsvHarness(gate=gate, bundle=bundle)

    with TestClient(harness.app) as client:
        b_resp = client.post(f"{PREFIX}/batches", json={"genes": ["HER2", "MYCN", "NOT_IN_DATA"]}, headers=SAME_ORIGIN)
        batch_id = b_resp.json()["batch_id"]
        wait_for_batch(client, batch_id)

        export_endpoints = [
            (f"{PREFIX}/genes/ERBB2/export.csv", "gene_symbol"),
            (f"{PREFIX}/genes/ERBB2/export.csv?include_excluded=true", "gene_symbol"),
            (f"{PREFIX}/genes/ERBB2/curator-sheet.csv", "Gene"),
            (f"{PREFIX}/export.csv", "gene_symbol"),
            (f"{PREFIX}/export.csv?include_excluded=true", "gene_symbol"),
            (f"{PREFIX}/golden-set.csv", "Genes"),
            (f"{PREFIX}/golden-set.csv?genes=ERBB2,MYCN", "Genes"),
            (f"{PREFIX}/batches/{batch_id}/export.csv", "gene_symbol"),
            (f"{PREFIX}/batches/{batch_id}/export.csv?include_excluded=true", "gene_symbol"),
            (f"{PREFIX}/batches/{batch_id}/golden-set.csv", "Genes"),
            (f"{PREFIX}/batches/{batch_id}/curator-sheet.csv", "Gene"),
        ]

        for url, expected_first_header in export_endpoints:
            resp = client.get(url)
            assert resp.status_code == 200, f"{url} failed with {resp.status_code}: {resp.text}"
            header, data_rows = parse_raw_csv_rows(resp.text)
            assert header[0] == expected_first_header, (
                f"{url}: expected first column {expected_first_header!r}, got {header[0]!r}"
            )
            assert header[0] in ("gene_symbol", "Genes", "Gene")
            assert len(data_rows) >= 1, f"{url} returned no data rows"
            for row_idx, row in enumerate(data_rows, start=1):
                assert row and row[0].strip() != "", (
                    f"{url} row {row_idx} has empty first cell: {row!r}"
                )


# ---------------------------------------------------------------------------
# 5. CLI tests with mocked transport (key never printed, polling, downloads)
# ---------------------------------------------------------------------------


def test_cli_with_mocked_transport_polls_downloads_and_never_prints_api_key(
    tmp_path, monkeypatch, capsys
):
    secret_key = "acgc_secret_test_key_never_print_987654321"
    monkeypatch.setenv("ACGC_API_KEY", secret_key)

    genes_file = tmp_path / "genes.txt"
    genes_file.write_text("ERBB2\nEGFR\nNOPE\n", encoding="utf-8")
    out_dir = tmp_path / "out"

    poll_calls = 0
    seen_auth: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal poll_calls
        seen_auth.append(request.headers.get("Authorization", ""))
        path = request.url.path
        if request.method == "POST" and path == f"{PREFIX}/batches":
            body = json.loads(request.content.decode())
            assert body == {"genes": ["ERBB2", "EGFR", "NOPE"]}
            return httpx.Response(
                202,
                json={
                    "batch_id": "cli-b1",
                    "status": "queued",
                    "status_url": f"{PREFIX}/batches/cli-b1",
                },
            )
        if request.method == "GET" and path == f"{PREFIX}/batches/cli-b1":
            poll_calls += 1
            st = "running" if poll_calls == 1 else "done"
            return httpx.Response(
                200,
                json={
                    "batch_id": "cli-b1",
                    "status": st,
                    "release_id": RELEASE_ID,
                    "counts": {"total": 3, "done": 2, "not_found": 1},
                    "genes": [],
                },
            )
        if request.method == "GET" and path == f"{PREFIX}/batches/cli-b1/export.csv":
            return httpx.Response(200, content=(UTF8_BOM + "gene_symbol\r\nERBB2\r\n").encode())
        if request.method == "GET" and path == f"{PREFIX}/batches/cli-b1/golden-set.csv":
            return httpx.Response(200, content=(UTF8_BOM + "Genes\r\nERBB2\r\n").encode())
        if request.method == "GET" and path == f"{PREFIX}/batches/cli-b1/curator-sheet.csv":
            return httpx.Response(200, content=(UTF8_BOM + "Gene\r\nERBB2\r\n").encode())
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    written = run_batch_cli(
        base_url="https://acgc.example.org",
        genes_file=genes_file,
        out_dir=out_dir,
        poll_interval=0.0,
        transport=transport,
        sleep_fn=lambda _s: None,
    )
    assert set(written.keys()) == {"per_pair", "golden_set", "curator_sheet"}
    for p in written.values():
        assert p.is_file() and p.read_text(encoding="utf-8").startswith(UTF8_BOM)
    assert poll_calls == 2
    assert all(a == f"Bearer {secret_key}" for a in seen_auth)

    # Also test main() error path and ensure key is never printed to stdout or stderr
    bad_transport = httpx.MockTransport(lambda req: httpx.Response(500, text="server error"))
    rc = cli_main(
        [
            "--base-url",
            "https://acgc.example.org",
            "--genes",
            str(genes_file),
            "--out-dir",
            str(out_dir),
        ],
        transport=bad_transport,
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert secret_key not in captured.out
    assert secret_key not in captured.err


# ---------------------------------------------------------------------------
# Blocker 1: Restart recovery can strand batches
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_recovery_crashed_owner_and_replacement_resumes_after_expiry():
    bundle = build_bundle(("ERBB2", "MYCN"))
    gate = FakeLiteratureGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (True, []),
        }
    )
    store = InMemoryExpressionContextStore()
    now = datetime.now(timezone.utc)
    # Crashed pod with short lease
    batch = BatchRecord(
        batch_id="stranded-b1",
        created_by="alice@example.org",
        created_at=now,
        release_id=RELEASE_ID,
        status="running",
        worker_owner="crashed-pod",
        worker_expires_at=now + timedelta(seconds=0.2),
        counts=BatchCounts(total=2, queued=2),
    )
    genes = [
        BatchGeneRecord(
            batch_id="stranded-b1",
            position=0,
            input_symbol="ERBB2",
            hugo_symbol="ERBB2",
            status="queued",
            updated_at=now,
        ),
        BatchGeneRecord(
            batch_id="stranded-b1",
            position=1,
            input_symbol="MYCN",
            hugo_symbol="MYCN",
            status="queued",
            updated_at=now,
        ),
    ]
    await store.create_batch(batch, genes)
    store.batch_locks["stranded-b1"] = ("crashed-pod", store.lock_clock() + 0.2)

    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, bundle)
    redis = LeaseFakeRedis()
    replacement_service = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
        lease_poll_seconds=0.01,
        releases_repo=REPO,
        batch_recovery_interval=0.1,
    )
    replacement_service.start()
    try:
        deadline = time.monotonic() + 4.0
        recovered = False
        while time.monotonic() < deadline:
            b = await store.get_batch("stranded-b1")
            if b and b.status == "done":
                recovered = True
                break
            await asyncio.sleep(0.05)
        assert recovered, "Batch did not recover and finish after crashed owner lease expiry"
        final_batch = await store.get_batch("stranded-b1")
        assert final_batch.status == "done"
        assert final_batch.counts.done >= 1
    finally:
        await replacement_service.aclose()


# ---------------------------------------------------------------------------
# Blocker 2: Cross-pod cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_pod_cancel_honored_by_worker_pod():
    bundle = build_bundle(("ERBB2", "MYCN"))
    store = InMemoryExpressionContextStore()
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, bundle)
    redis = LeaseFakeRedis()

    erbb2_started = asyncio.Event()
    cancel_done = asyncio.Event()

    class CoordinatingGate(FakeLiteratureGate):
        async def evaluate(self, hugo_symbol: str, tumor: TumorContext) -> LiteratureVerdict:
            if hugo_symbol == "ERBB2":
                erbb2_started.set()
                await cancel_done.wait()
            return await super().evaluate(hugo_symbol, tumor)

    gate = CoordinatingGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (True, []),
        }
    )

    pod1_worker = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
        lease_poll_seconds=0.01,
        releases_repo=REPO,
    )
    pod2_api = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
        lease_poll_seconds=0.01,
        releases_repo=REPO,
    )

    now = datetime.now(timezone.utc)
    batch = BatchRecord(
        batch_id="cancel-b1",
        created_by="alice@example.org",
        created_at=now,
        release_id=RELEASE_ID,
        status="queued",
        counts=BatchCounts(total=2, queued=2),
    )
    genes = [
        BatchGeneRecord(
            batch_id="cancel-b1",
            position=0,
            input_symbol="ERBB2",
            hugo_symbol="ERBB2",
            status="queued",
            updated_at=now,
        ),
        BatchGeneRecord(
            batch_id="cancel-b1",
            position=1,
            input_symbol="MYCN",
            hugo_symbol="MYCN",
            status="queued",
            updated_at=now,
        ),
    ]
    await store.create_batch(batch, genes)

    # Worker starts batch
    worker_task = asyncio.create_task(pod1_worker._run_batch("cancel-b1"))

    # Wait for ERBB2 evaluation to start
    await erbb2_started.wait()

    # Simulated Pod 2 receives cancel request
    cancelled_detail = await pod2_api.cancel_batch("cancel-b1")
    assert cancelled_detail.status == "cancelled"

    # Allow ERBB2 in-flight evaluation to finish
    cancel_done.set()
    await worker_task

    final_batch = await store.get_batch("cancel-b1")
    assert final_batch.status == "cancelled"

    batch_genes = await store.list_batch_genes("cancel-b1")
    # ERBB2 in-flight was cancelled, and late writes are no-ops
    assert batch_genes[0].error == "cancelled"
    # MYCN was never started by worker (error is cancelled from cancel_batch)
    assert batch_genes[1].error == "cancelled"
    assert all(sym == "ERBB2" for sym, _ in gate.calls)


# ---------------------------------------------------------------------------
# Blocker 3: Lease loss mid-gene
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_loss_worker_stops_and_stale_writes_are_no_ops():
    bundle = build_bundle(("ERBB2", "MYCN"))
    store = InMemoryExpressionContextStore()
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, bundle)
    redis = LeaseFakeRedis()

    lease_lost = asyncio.Event()

    class StolenLeaseGate(FakeLiteratureGate):
        async def evaluate(self, hugo_symbol: str, tumor: TumorContext) -> LiteratureVerdict:
            if hugo_symbol == "ERBB2":
                # Lease is stolen by pod-b while pod-a is in the gate
                store.batch_locks["loss-b1"] = ("pod-b", store.lock_clock() + 300.0)
                lease_lost.set()
            return await super().evaluate(hugo_symbol, tumor)

    gate = StolenLeaseGate(
        verdicts={
            "breast cancer": (False, ["12345678"]),
            "gastric cancer": (True, []),
        }
    )

    pod_a_service = ExpressionContextService(
        catalog=StatsCatalog(bundles, cache_seconds=0),
        gate=gate,
        store=store,
        jobs=JobStore("expression_context", JobRecord, redis_client_factory=lambda: None),
        lease=VerdictLease(ttl_seconds=5, redis=module_redis(redis)),
        lease_poll_seconds=0.01,
        releases_repo=REPO,
    )

    now = datetime.now(timezone.utc)
    batch = BatchRecord(
        batch_id="loss-b1",
        created_by="alice@example.org",
        created_at=now,
        release_id=RELEASE_ID,
        status="queued",
        counts=BatchCounts(total=2, queued=2),
    )
    genes = [
        BatchGeneRecord(
            batch_id="loss-b1",
            position=0,
            input_symbol="ERBB2",
            hugo_symbol="ERBB2",
            status="queued",
            updated_at=now,
        ),
        BatchGeneRecord(
            batch_id="loss-b1",
            position=1,
            input_symbol="MYCN",
            hugo_symbol="MYCN",
            status="queued",
            updated_at=now,
        ),
    ]
    await store.create_batch(batch, genes)

    # pod-a starts running the batch
    await pod_a_service._run_batch("loss-b1")

    assert lease_lost.is_set()
    # pod-a's write to "done" for ERBB2 was rejected because token "pod-a" was no longer owner
    batch_genes = await store.list_batch_genes("loss-b1")
    # ERBB2 is still running because pod-a's write to done was rejected
    assert batch_genes[0].status == "running"
    # MYCN was never started
    assert batch_genes[1].status == "queued"
    # pod-a never evaluated MYCN
    assert all(sym != "MYCN" for sym, _ in gate.calls)


# ---------------------------------------------------------------------------
# Blocker 4: Streamed batch exports (page-by-page and supersession)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_exports_streaming_page_by_page_and_supersession():
    bundle_a = build_bundle(("ERBB2", "MYCN"), release_id="rel-a")
    bundle_b = build_bundle(("ERBB2", "MYCN"), release_id="rel-b")
    harness = CsvHarness(bundle=bundle_a)

    with TestClient(harness.app) as client:
        resp = client.post(
            f"{PREFIX}/batches",
            json={"genes": ["ERBB2", "MYCN"]},
            headers=SAME_ORIGIN,
        )
        assert resp.status_code == 202
        batch_id = resp.json()["batch_id"]
        wait_for_batch(client, batch_id)

    # For all three exports, test streaming page by page with supersession mid-stream
    for export_fn_name in (
        "export_batch_per_pair_csv",
        "export_batch_golden_set_csv",
        "export_batch_curator_sheet_csv",
    ):
        await seed_bundle(harness.bundles, bundle_a)
        harness.service.catalog.invalidate()

        export_fn = getattr(harness.service, export_fn_name)
        filename, stream = await export_fn(batch_id, page_size=1)
        assert filename.endswith(".csv")

        header = await anext(stream)
        assert header.startswith(UTF8_BOM)

        page1 = await anext(stream)
        assert len(page1) > 0

        # Supersession mid-stream: switch active bundle to rel-b
        await seed_bundle(harness.bundles, bundle_b)
        harness.service.catalog.invalidate()

        with pytest.raises(BundleGone):
            async for _ in stream:
                pass


# ---------------------------------------------------------------------------
# Blocker 6: Authorization & CSRF tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_authorization_and_csrf():
    bundle = build_bundle(("ERBB2", "MYCN"))
    harness = CsvHarness(bundle=bundle)

    ALICE_SESSION = AuthenticatedUser(
        email="alice@example.org", name="Alice", domain="example.org", auth_method="session", role="viewer"
    )
    ALICE_API_KEY = AuthenticatedUser(
        email="alice@example.org", name="Alice", domain="example.org", auth_method="api_key", role="viewer"
    )
    BOB_SESSION = AuthenticatedUser(
        email="bob@example.org", name="Bob", domain="example.org", auth_method="session", role="viewer"
    )
    ADMIN_SESSION = AuthenticatedUser(
        email="admin@example.org", name="Admin", domain="example.org", auth_method="session", role="admin"
    )

    with TestClient(harness.app) as client:
        # 1. CSRF on POST /batches
        # Cookie session without CSRF header -> 403
        harness.user = ALICE_SESSION
        r = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2"]})
        assert r.status_code == 403
        assert "Same-origin request evidence required" in r.text

        # Cookie session with bad sec-fetch-site -> 403
        r = client.post(
            f"{PREFIX}/batches",
            json={"genes": ["ERBB2"]},
            headers={"sec-fetch-site": "cross-site"},
        )
        assert r.status_code == 403

        # API key user without CSRF header -> 202
        harness.user = ALICE_API_KEY
        r = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2"]})
        assert r.status_code == 202

        # Cookie session with valid same-origin header -> 202
        harness.user = ALICE_SESSION
        r = client.post(f"{PREFIX}/batches", json={"genes": ["ERBB2"]}, headers=SAME_ORIGIN)
        assert r.status_code == 202
        batch_id = r.json()["batch_id"]

        # 2. CSRF on POST /batches/{id}/cancel
        # Cookie session without CSRF header -> 403 CSRF error
        r = client.post(f"{PREFIX}/batches/{batch_id}/cancel", json={})
        assert r.status_code == 403
        assert "Same-origin request evidence required" in r.text

        # 3. Non-owner authorization: Bob (viewer) vs Alice's batch
        harness.user = BOB_SESSION
        # Detail -> 404
        assert client.get(f"{PREFIX}/batches/{batch_id}").status_code == 404
        # Exports -> 404
        assert client.get(f"{PREFIX}/batches/{batch_id}/export.csv").status_code == 404
        assert client.get(f"{PREFIX}/batches/{batch_id}/golden-set.csv").status_code == 404
        assert client.get(f"{PREFIX}/batches/{batch_id}/curator-sheet.csv").status_code == 404
        # Cancel -> 403 Forbidden
        assert client.post(f"{PREFIX}/batches/{batch_id}/cancel", json={}, headers=SAME_ORIGIN).status_code == 403

        # 4. Admin authorization: Admin can read, export, and cancel any batch
        harness.user = ADMIN_SESSION
        assert client.get(f"{PREFIX}/batches/{batch_id}").status_code == 200
        assert client.get(f"{PREFIX}/batches/{batch_id}/export.csv").status_code == 200
        assert client.get(f"{PREFIX}/batches/{batch_id}/golden-set.csv").status_code == 200
        assert client.get(f"{PREFIX}/batches/{batch_id}/curator-sheet.csv").status_code == 200

        # Mark batch running so it can be cancelled
        await harness.store.update_batch_status(batch_id, "running")
        cancel_resp = client.post(f"{PREFIX}/batches/{batch_id}/cancel", json={}, headers=SAME_ORIGIN)
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

        # 5. GET /batches filtering: lists only caller's batches, admin ?all=true
        # Bob creates a batch
        harness.user = BOB_SESSION
        r_bob = client.post(f"{PREFIX}/batches", json={"genes": ["MYCN"]}, headers=SAME_ORIGIN)
        assert r_bob.status_code == 202
        bob_batch_id = r_bob.json()["batch_id"]

        # Alice lists batches -> sees only Alice's batches
        harness.user = ALICE_SESSION
        alice_batches = client.get(f"{PREFIX}/batches").json()["items"]
        alice_ids = [b["batch_id"] for b in alice_batches]
        assert batch_id in alice_ids
        assert bob_batch_id not in alice_ids

        # Bob lists batches -> sees only Bob's batch
        harness.user = BOB_SESSION
        bob_batches = client.get(f"{PREFIX}/batches").json()["items"]
        bob_ids = [b["batch_id"] for b in bob_batches]
        assert bob_batch_id in bob_ids
        assert batch_id not in bob_ids

        # Bob passes ?all=true -> ignored for non-admin, still only sees own batches
        bob_all = client.get(f"{PREFIX}/batches?all=true").json()["items"]
        assert [b["batch_id"] for b in bob_all] == bob_ids

        # Admin lists batches without ?all=true -> sees only admin's own batches
        harness.user = ADMIN_SESSION
        admin_own = client.get(f"{PREFIX}/batches").json()["items"]
        assert batch_id not in [b["batch_id"] for b in admin_own]
        assert bob_batch_id not in [b["batch_id"] for b in admin_own]

        # Admin passes ?all=true -> sees all batches (both Alice's and Bob's)
        admin_all = client.get(f"{PREFIX}/batches?all=true").json()["items"]
        all_ids = [b["batch_id"] for b in admin_all]
        assert batch_id in all_ids
        assert bob_batch_id in all_ids
