"""Alteration release detail must not affect expression/literature decisions."""

import asyncio
import copy
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.expression_context.csv_export import (
    CSV_COLUMNS,
    gene_view_csv_rows,
    golden_set_row_from_view,
)
from src.expression_context.models import Alterations, CancerTypeStats
from tests.expression_context_helpers import build_bundle
from tests.test_expression_context_csv import (
    PREFIX,
    CsvHarness,
    parse_csv_response,
    run_job,
)

pytestmark = [pytest.mark.no_real_redis]
FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures/expression_context/alterations.json").read_text()
)


def block(*names):
    return {
        "alteration_types_enriched": list(names),
        "categories": {name: {"status": "enriched"} for name in names},
    }


def with_alterations(pair, data, **updates):
    stats = CancerTypeStats.model_validate({**pair.stats.model_dump(), "alterations": data})
    return pair.model_copy(update={"stats": stats, **updates})


def test_golden_set_real_categories_union_and_surfaced_only():
    harness = CsvHarness()
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        first = with_alterations(view.pairs[0], block("Point Mutation", "Copy Number Alteration"), status="surfaced")
        second = with_alterations(view.pairs[1], block("Structural Variant"), status="surfaced")
        combined = view.model_copy(update={"pairs": [first, second]})
        assert golden_set_row_from_view(combined)[2] == (
            "Point Mutation, Structural Variant, Expression, Copy Number Alteration"
        )
        # Unique SV on a candidate must not leak into the gene-level cell.
        candidate = second.model_copy(update={"status": "candidate", "exclusion_reason": "candidate"})
        mixed = view.model_copy(update={"pairs": [first, candidate]})
        assert golden_set_row_from_view(mixed)[2] == "Point Mutation, Expression, Copy Number Alteration"
        mixed.pairs = [candidate]
        assert golden_set_row_from_view(mixed)[1:] == ["", "", ""]


@pytest.mark.parametrize("symbol", ["EGFR", "KRAS"])
def test_release_fixture_golden_set_rows(symbol):
    harness = CsvHarness()
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        fixture = FIXTURE[symbol]
        pair = with_alterations(
            view.pairs[0], fixture["alterations"],
            status="surfaced", oncotree_code=fixture["oncotree_code"],
        )
        view = view.model_copy(update={"hugo_symbol": symbol, "pairs": [pair]})
        expected = (
            "Point Mutation, Structural Variant, Expression, Copy Number Alteration"
            if symbol == "EGFR" else "Point Mutation, Expression"
        )
        assert golden_set_row_from_view(view) == [
            symbol, "Glioblastoma" if symbol == "EGFR" else "Pancreatic adenocarcinoma", expected, "",
        ]
        row = dict(zip(CSV_COLUMNS, gene_view_csv_rows(view)[0], strict=True))
        assert row["alteration_types_enriched"] == expected.replace("Expression, ", "").replace(", Expression", "")


def test_old_unknown_and_untestable_categories(caplog):
    harness = CsvHarness()
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        pair = view.pairs[0]
        assert pair.stats.alterations is None
        data = block("Future Category", "Point Mutation", "Structural Variant")
        data["categories"]["Point Mutation"] = {"status": "not_testable", "reason": "no driver"}
        data["categories"]["Structural Variant"] = {"status": "insufficient_data"}
        updated = with_alterations(pair, data, status="surfaced")
        assert updated.stats.alterations.alteration_types_enriched == []
        assert "Future Category" not in updated.stats.alterations.categories
        assert "Ignoring unknown alteration category" in caplog.text
        assert golden_set_row_from_view(view.model_copy(update={"pairs": [updated]}))[2] == "Expression"
        assert updated.stats.alterations.categories["Point Mutation"].reason == "no driver"


def test_alterations_are_detail_only_through_apis_exports_and_history():
    snapshots = []
    for add_detail in (False, True):
        def edit(doc):
            # Exercise candidate, excluded, pending and surfaced decisions with detail.
            doc["cancer_types"][1]["confidence_tier"] = "candidate"
            if add_detail:
                for pair in doc["cancer_types"]:
                    pair["alterations"] = copy.deepcopy(FIXTURE["EGFR"]["alterations"])
                    pair["alterations"]["categories"]["Future Category"] = {"status": "enriched"}
        harness = CsvHarness(bundle=build_bundle(("ERBB2",), edit_gene=edit))
        with TestClient(harness.app) as client:
            run_job(client)
            stats = client.get(f"{PREFIX}/stats/ERBB2").json()
            result = client.get(f"{PREFIX}/genes/ERBB2?include_excluded=true").json()
            history = client.get(f"{PREFIX}/genes").json()
            _, rows = parse_csv_response(client.get(f"{PREFIX}/genes/ERBB2/export.csv").text)
            _, golden = parse_csv_response(client.get(f"{PREFIX}/golden-set.csv?genes=ERBB2").text)
            if add_detail:
                assert all(p["alterations"] for p in stats["pairs"])
                assert all(p["stats"]["alterations"] for p in result["surfaced"] + result["excluded"])
                cna = stats["pairs"][0]["stats"]["alterations"]["categories"]["Copy Number Alteration"]
                assert cna["del"]["n_events"] == 0
                assert all(r["alteration_types_enriched"] for r in rows)
            # Strip only added detail and wall-clock timestamps. Everything deciding
            # status/eligibility, all history counts and all exported identities stays identical.
            def strip(value):
                if isinstance(value, dict):
                    return {k: strip(v) for k, v in value.items() if k not in {
                        "alterations", "alteration_types_enriched", "Relevant alteration type(s)",
                        "computed_at", "evaluated_at", "loaded_at", "literature_checked_at",
                    }}
                if isinstance(value, list):
                    return [strip(v) for v in value]
                return value
            snapshots.append(json.dumps(strip([stats, result, history, rows, golden]), sort_keys=True))
    assert snapshots[0] == snapshots[1]


def test_cna_directions_survive_storage_round_trip():
    alterations = Alterations.model_validate(FIXTURE["EGFR"]["alterations"])
    round_trip = Alterations.model_validate(alterations.model_dump())
    assert round_trip == alterations
    assert round_trip.categories["Copy Number Alteration"].del_.n_events == 0


@pytest.mark.parametrize("invalid", [None, [], "enriched", 12, True])
def test_non_dict_alteration_categories_are_skipped(invalid):
    harness = CsvHarness()
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        data = block("Point Mutation", "Copy Number Alteration")
        data["categories"]["Point Mutation"] = invalid
        pair = with_alterations(view.pairs[0], data, status="surfaced")
        assert "Point Mutation" not in pair.alterations.categories
        assert pair.alterations.alteration_types_enriched == ["Copy Number Alteration"]
        view = view.model_copy(update={"pairs": [pair]})
        assert golden_set_row_from_view(view)[2] == "Expression, Copy Number Alteration"
        assert pair.model_dump(mode="json")["alterations"]["alteration_types_enriched"] == ["Copy Number Alteration"]


def test_pair_alteration_accessor_keeps_api_and_exports_in_sync():
    harness = CsvHarness()
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        pair = with_alterations(view.pairs[0], block("Point Mutation"), status="surfaced")
        # A stale denormalized value must not override the release stats accessor.
        pair = pair.model_copy(update={"alterations": Alterations.model_validate(block("Structural Variant"))})
        payload = pair.model_dump(mode="json")
        assert payload["alterations"] == payload["stats"]["alterations"]
        assert payload["alterations"]["alteration_types_enriched"] == ["Point Mutation"]
        view = view.model_copy(update={"pairs": [pair]})
        assert golden_set_row_from_view(view)[2] == "Point Mutation, Expression"
        row = dict(zip(CSV_COLUMNS, gene_view_csv_rows(view)[0], strict=True))
        assert row["alteration_types_enriched"] == "Point Mutation"


def test_per_pair_alteration_cell_uses_csv_guard(monkeypatch):
    from src.expression_context import csv_export

    harness = CsvHarness()
    with TestClient(harness.app):
        view, _ = asyncio.run(harness.service.gene_view("ERBB2"))
        pair = view.pairs[0].model_copy(update={"status": "surfaced"})
        view = view.model_copy(update={"pairs": [pair]})
        # Exercise the cell guard independently of the allowed-category filter.
        monkeypatch.setattr(csv_export, "enriched_alteration_types", lambda p: ["=unsafe"])
        row = dict(zip(CSV_COLUMNS, gene_view_csv_rows(view)[0], strict=True))
        assert row["alteration_types_enriched"] == "'=unsafe"
