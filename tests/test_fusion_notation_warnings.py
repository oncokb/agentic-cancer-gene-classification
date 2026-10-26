"""Tests for warning on fusion-like notation that is not parsed as a fusion,
and for accepting fusions given as separate gene1/gene2 partners."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import main
from src.models.schema import AnnotateRequest, FusionInput, GeneAnnotation, ResolvedGene
from src.pipeline.normalization import unparsed_fusion_warning, unparsed_fusion_warnings


def _unresolvable(symbol: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=False, unresolvable=True)


def _resolved(symbol: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=True)


def _lookup_failed(symbol: str) -> ResolvedGene:
    return ResolvedGene(
        input_symbol=symbol, canonical_symbol=symbol, resolved=False, unresolvable=False, lookup_failed=True
    )


# --- unparsed_fusion_warning ------------------------------------------------


def test_single_hyphen_fusion_warns_when_hgnc_confirms_not_a_symbol():
    assert unparsed_fusion_warning("EWSR1-FLI1", _unresolvable("EWSR1-FLI1")) == (
        "'EWSR1-FLI1' was treated as a single gene. To annotate it as a fusion, write 'EWSR1::FLI1'."
    )


def test_single_hyphen_does_not_warn_without_a_confirmed_hgnc_miss():
    assert unparsed_fusion_warning("EWSR1-FLI1") is None
    assert unparsed_fusion_warning("EWSR1-FLI1", _lookup_failed("EWSR1-FLI1")) is None
    assert unparsed_fusion_warning("EWSR1-FLI1", _resolved("EWSR1-FLI1")) is None


def test_whitespace_separated_partners_warn():
    assert unparsed_fusion_warning("EML4 ALK fusion", _unresolvable("EML4 ALK fusion")) == (
        "'EML4 ALK fusion' was treated as a single gene. To annotate it as a fusion, write 'EML4::ALK'."
    )
    assert unparsed_fusion_warning("EML4 ALK") is not None


@pytest.mark.parametrize("symbol", ["HLA-A", "HLA-DRB1", "NKX2-1", "MT-CO1"])
@pytest.mark.parametrize("resolution", [None, _resolved, _unresolvable, _lookup_failed])
def test_real_hyphenated_symbols_never_warn(symbol, resolution):
    resolved = resolution(symbol) if resolution else None
    assert unparsed_fusion_warning(symbol, resolved) is None


@pytest.mark.parametrize("value", ["GREB1::NCOA2", "BCR/ABL1", "EML4--ALK", "ALK", "KRAS G12D", "EML4 fusion"])
def test_parsed_fusions_and_non_fusion_text_do_not_warn(value):
    assert unparsed_fusion_warning(value, _unresolvable(value)) is None


def test_unparsed_fusion_warnings_reuses_normalization_results():
    gene_map = {
        "EWSR1-FLI1": (_unresolvable("EWSR1-FLI1"), ["EWSR1-FLI1"]),
        "HLA-A": (_resolved("HLA-A"), ["HLA-A"]),
        "GREB1": (_resolved("GREB1"), ["GREB1::NCOA2"]),
        "NCOA2": (_resolved("NCOA2"), ["GREB1::NCOA2"]),
    }
    inputs = ["EWSR1-FLI1", "HLA-A", "GREB1::NCOA2", "EWSR1-FLI1"]

    assert unparsed_fusion_warnings(inputs, gene_map) == [
        "'EWSR1-FLI1' was treated as a single gene. To annotate it as a fusion, write 'EWSR1::FLI1'."
    ]


# --- gene1 / gene2 inputs ---------------------------------------------------


def test_gene1_gene2_are_converted_to_fusion_notation():
    assert FusionInput(gene1="EML4", gene2="ALK").fusion == "EML4::ALK"
    request = AnnotateRequest(fusions=[{"gene1": "EML4", "gene2": "ALK", "tumor_type": "LUAD"}])
    assert request.fusions[0].fusion == "EML4::ALK"
    assert request.fusions[0].tumor_type == "LUAD"


def test_gene1_gene2_agreeing_with_fusion_keeps_fusion():
    assert FusionInput(fusion="EML4--ALK", gene1="EML4", gene2="ALK").fusion == "EML4--ALK"


def test_gene1_gene2_conflicting_with_fusion_is_rejected():
    with pytest.raises(ValidationError, match="conflicts with gene1/gene2"):
        FusionInput(fusion="BCR::ABL1", gene1="EML4", gene2="ALK")


def test_gene1_without_gene2_is_rejected():
    with pytest.raises(ValidationError, match="gene1 and gene2 must both be given"):
        FusionInput(gene1="EML4")


# --- API --------------------------------------------------------------------


class FakeRunStore:
    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        pass


@pytest.fixture
def client(monkeypatch):
    async def fake_normalize_fusions(inputs):
        gene_map = {}
        for value in inputs:
            if value == "EWSR1-FLI1":
                gene_map[value] = (_unresolvable(value), [value])
            else:
                gene_map[value] = (_resolved(value), [value])
        return gene_map

    async def fake_annotate_gene(gene, fusions, resolved_gene, unresolvable, **kwargs):
        return GeneAnnotation(gene=gene, fusions=fusions)

    async def no_cached_annotation(**kwargs):
        return None

    monkeypatch.setattr("src.pipeline.orchestrator.normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr("src.pipeline.orchestrator._annotate_gene", fake_annotate_gene)
    monkeypatch.setattr("src.pipeline.orchestrator._maybe_reuse_cached_annotation", no_cached_annotation)
    main.app.state.run_store = FakeRunStore()
    return TestClient(main.app)


_EXPECTED_WARNING = "'EWSR1-FLI1' was treated as a single gene. To annotate it as a fusion, write 'EWSR1::FLI1'."


def test_annotate_returns_unparsed_fusion_warning(client):
    response = client.post("/v1/annotate", json={"fusions": ["EWSR1-FLI1", "HLA-A"]})

    assert response.status_code == 200
    assert response.json()["warnings"] == [_EXPECTED_WARNING]


def test_annotate_job_result_carries_unparsed_fusion_warning(client):
    created = client.post("/v1/annotate/jobs", json={"fusions": ["EWSR1-FLI1"]})
    assert created.status_code == 200
    status_url = created.json()["status_url"]

    deadline = time.time() + 5
    while True:
        body = client.get(status_url).json()
        if body["status"] in ("complete", "failed") or time.time() > deadline:
            break
        time.sleep(0.02)

    assert body["status"] == "complete"
    assert body["result"]["warnings"] == [_EXPECTED_WARNING]


def test_annotate_accepts_gene1_gene2_and_rejects_conflicts(client):
    response = client.post("/v1/annotate", json={"fusions": [{"gene1": "EML4", "gene2": "ALK"}]})
    assert response.status_code == 200
    assert response.json()["warnings"] == []

    conflict = client.post(
        "/v1/annotate",
        json={"fusions": [{"fusion": "BCR::ABL1", "gene1": "EML4", "gene2": "ALK"}]},
    )
    assert conflict.status_code == 422
    assert "conflicts with gene1/gene2" in conflict.text
