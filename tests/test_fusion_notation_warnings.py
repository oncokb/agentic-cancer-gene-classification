"""Tests for warning on fusion-like notation that is not parsed as a fusion,
and for accepting fusions given as separate gene1/gene2 partners."""

from __future__ import annotations

import re
import time
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src import main
from src.models.schema import AnnotateRequest, FusionInput, GeneAnnotation, ResolvedGene
from src.pipeline.normalization import (
    normalize_fusions,
    unparsed_fusion_warning,
    unparsed_fusion_warnings,
)


def _unresolvable(symbol: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=False, unresolvable=True)


def _resolved(symbol: str, **kwargs) -> ResolvedGene:
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=True, **kwargs)


def _lookup_failed(symbol: str) -> ResolvedGene:
    return ResolvedGene(
        input_symbol=symbol, canonical_symbol=symbol, resolved=False, unresolvable=False, lookup_failed=True
    )


def _warning(text: str, suggestion: str) -> str:
    return f"'{text}' was treated as a single gene. To annotate it as a fusion, write '{suggestion}'."


# --- Fake HGNC --------------------------------------------------------------

# symbol -> (aliases, previous symbols)
_HGNC = {
    "EWSR1": ([], []),
    "FLI1": ([], []),
    "EML4": ([], []),
    "ALK": ([], []),
    "GREB1": ([], []),
    "NCOA2": ([], []),
    "BCR": ([], []),
    "ABL1": ([], []),
    "HLA-A": ([], []),
    "HLA-DRB1": ([], []),
    "NKX2-1": ([], ["TITF1"]),
    "MT-CO1": ([], []),
    "HOXA-AS2": ([], []),
    "CSF2": (["GM-CSF"], []),
    "TNF": (["TNF-alpha"], []),
    "IL6": (["IL-6"], []),
}
_ALIAS_QUERY = re.compile(r'alias_symbol:"([^"]+)"')


def _doc(symbol: str) -> dict:
    aliases, prev = _HGNC[symbol]
    return {"symbol": symbol, "hgnc_id": f"HGNC:{symbol}", "alias_symbol": aliases, "prev_symbol": prev}


def _hgnc_handler(offline: bool = False, alias_offline: bool = False):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        requests.append(path)
        if offline:
            raise httpx.ConnectError("HGNC unreachable", request=request)
        alias_query = _ALIAS_QUERY.search(path)
        if alias_query:
            if alias_offline:
                raise httpx.ConnectError("HGNC unreachable", request=request)
            wanted = alias_query.group(1).upper()
            docs = [
                _doc(symbol)
                for symbol, (aliases, prev) in _HGNC.items()
                if wanted in {name.upper() for name in [*aliases, *prev]}
            ]
        elif path.startswith("/fetch/symbol/"):
            symbol = path.removeprefix("/fetch/symbol/")
            docs = [_doc(symbol)] if symbol in _HGNC else []
        elif path.startswith("/search/symbol/"):
            # Like HGNC, search tokenizes on whitespace: 'EML4 ALK fusion' finds EML4.
            tokens = path.removeprefix("/search/symbol/").upper().split()
            docs = [_doc(name) for name in _HGNC if name.upper() in tokens][:1]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json={"response": {"docs": docs}})

    handler.requests = requests
    return handler


@pytest.fixture
def fake_hgnc(monkeypatch):
    """Patch only the HGNC HTTP boundary (and bypass the Redis cache) so tests exercise real normalization."""
    real_client = httpx.AsyncClient
    state = {"handler": _hgnc_handler()}

    def client_factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: state["handler"](request)))

    async def no_cache(key, compute, ttl_seconds=None):
        return await compute()

    monkeypatch.setattr("src.pipeline.normalization.httpx.AsyncClient", client_factory)
    monkeypatch.setattr("src.pipeline.normalization.cached_call", no_cache)

    def configure(**kwargs):
        state["handler"] = _hgnc_handler(**kwargs)
        return state["handler"]

    configure()
    return configure


async def _warnings_for(inputs: list[str]) -> list[str]:
    return await unparsed_fusion_warnings(inputs, await normalize_fusions(inputs))


# --- unparsed_fusion_warning (no lookups) -----------------------------------


def test_single_hyphen_fusion_warns_when_hgnc_confirms_not_a_symbol():
    assert unparsed_fusion_warning("EWSR1-FLI1", _unresolvable("EWSR1-FLI1")) == _warning(
        "EWSR1-FLI1", "EWSR1::FLI1"
    )


def test_lowercase_single_hyphen_fusion_suggests_uppercase_notation():
    assert unparsed_fusion_warning("ewsr1-fli1", _unresolvable("ewsr1-fli1")) == _warning(
        "ewsr1-fli1", "EWSR1::FLI1"
    )


def test_single_hyphen_does_not_warn_without_a_confirmed_hgnc_miss():
    assert unparsed_fusion_warning("EWSR1-FLI1") is None
    assert unparsed_fusion_warning("EWSR1-FLI1", _lookup_failed("EWSR1-FLI1")) is None
    assert unparsed_fusion_warning("EWSR1-FLI1", _resolved("EWSR1-FLI1")) is None


def test_whitespace_separated_partners_warn():
    assert unparsed_fusion_warning("EML4 ALK fusion", _unresolvable("EML4 ALK fusion")) == _warning(
        "EML4 ALK fusion", "EML4::ALK"
    )
    assert unparsed_fusion_warning("EML4 ALK") is not None
    # HGNC's fuzzy search fallback "resolves" the whole string to EML4; that must not suppress it.
    fuzzy_hit = ResolvedGene(input_symbol="EML4 ALK fusion", canonical_symbol="EML4", resolved=True)
    assert unparsed_fusion_warning("EML4 ALK fusion", fuzzy_hit) is not None


@pytest.mark.parametrize("symbol", ["HLA-A", "HLA-DRB1", "NKX2-1", "MT-CO1", "IL-6"])
@pytest.mark.parametrize("resolution", [None, _resolved, _unresolvable, _lookup_failed])
def test_real_hyphenated_symbols_never_warn(symbol, resolution):
    resolved = resolution(symbol) if resolution else None
    assert unparsed_fusion_warning(symbol, resolved) is None


@pytest.mark.parametrize("value", ["TNF-alpha", "IFN-gamma", "NF-kB", "anti-EGFR"])
def test_greek_letter_and_short_lowercase_halves_never_warn(value):
    assert unparsed_fusion_warning(value, _unresolvable(value)) is None


def test_known_aliases_from_resolved_genes_suppress_the_warning():
    assert unparsed_fusion_warning("GM-CSF", _unresolvable("GM-CSF")) is not None
    assert unparsed_fusion_warning("GM-CSF", _unresolvable("GM-CSF"), known_aliases={"gm-csf"}) is None


@pytest.mark.parametrize(
    "value",
    [
        "GREB1::NCOA2",
        "BCR/ABL1",
        "EML4--ALK",
        "ALK",
        "EML4 fusion",
        "BRAF V600E",
        "KRAS G12C",
        "KRAS G12D",
        "EGFR exon19",
        "TNF alpha",
    ],
)
def test_parsed_fusions_and_non_fusion_text_do_not_warn(value):
    assert unparsed_fusion_warning(value, _unresolvable(value)) is None


# --- unparsed_fusion_warnings over real normalization -----------------------


@pytest.mark.asyncio
async def test_warnings_from_real_normalization(fake_hgnc):
    inputs = [
        "EWSR1-FLI1",
        "HLA-A",
        "HLA-DRB1",
        "NKX2-1",
        "MT-CO1",
        "HOXA-AS2",
        "GREB1::NCOA2",
        "BCR/ABL1",
        "EML4 ALK fusion",
        "EWSR1-FLI1",
    ]

    assert await _warnings_for(inputs) == [
        _warning("EWSR1-FLI1", "EWSR1::FLI1"),
        _warning("EML4 ALK fusion", "EML4::ALK"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias", ["GM-CSF", "TNF-alpha", "IL-6"])
async def test_hyphenated_hgnc_aliases_do_not_warn(fake_hgnc, alias):
    assert await _warnings_for([alias]) == []


@pytest.mark.asyncio
async def test_alias_lookup_runs_only_for_hyphen_candidates(fake_hgnc):
    handler = fake_hgnc()

    await _warnings_for(["GM-CSF", "HLA-A", "EML4 ALK fusion", "EGFR"])

    alias_lookups = [path for path in handler.requests if "alias_symbol" in path]
    assert len(alias_lookups) == 1 and '"GM-CSF"' in alias_lookups[0]


@pytest.mark.asyncio
async def test_alias_known_from_another_resolved_gene_needs_no_lookup(fake_hgnc):
    handler = fake_hgnc()

    assert await _warnings_for(["CSF2", "GM-CSF"]) == []
    assert not [path for path in handler.requests if "alias_symbol" in path]


@pytest.mark.asyncio
async def test_alias_lookup_failure_suppresses_the_hyphen_warning(fake_hgnc):
    fake_hgnc(alias_offline=True)

    assert await _warnings_for(["EWSR1-FLI1"]) == []


@pytest.mark.asyncio
async def test_hgnc_offline_suppresses_hyphen_but_not_whitespace_warning(fake_hgnc):
    # The whitespace form needs no lookup (symbols never contain spaces), so it still warns offline.
    fake_hgnc(offline=True)

    assert await _warnings_for(["EWSR1-FLI1", "EML4 ALK fusion"]) == [_warning("EML4 ALK fusion", "EML4::ALK")]


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


# --- API (real normalization; HGNC HTTP mocked) -----------------------------


class FakeRunStore:
    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        pass


@pytest.fixture
def client(monkeypatch, fake_hgnc):
    annotated = []

    async def fake_annotate_gene(gene, fusions, resolved_gene, unresolvable, **kwargs):
        annotated.append(gene)
        return GeneAnnotation(gene=gene, fusions=fusions)

    async def no_cached_annotation(**kwargs):
        return None

    monkeypatch.setattr("src.pipeline.orchestrator._annotate_gene", fake_annotate_gene)
    monkeypatch.setattr("src.pipeline.orchestrator._maybe_reuse_cached_annotation", no_cached_annotation)
    main.app.state.run_store = FakeRunStore()
    test_client = TestClient(main.app)
    test_client.annotated = annotated
    return test_client


_EWSR1_FLI1_WARNING = _warning("EWSR1-FLI1", "EWSR1::FLI1")


def test_annotate_returns_unparsed_fusion_warning(client):
    response = client.post("/v1/annotate", json={"fusions": ["EWSR1-FLI1", "HLA-A", "GM-CSF", "TNF-alpha"]})

    assert response.status_code == 200
    assert response.json()["warnings"] == [_EWSR1_FLI1_WARNING]


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
    assert body["result"]["warnings"] == [_EWSR1_FLI1_WARNING]


def test_annotate_returns_warning_when_annotation_is_reused_from_cache(client, monkeypatch):
    async def cached_annotation(*, gene, fusions, **kwargs):
        return GeneAnnotation(gene=gene, fusions=fusions, cache_status="reused")

    monkeypatch.setattr("src.pipeline.orchestrator._maybe_reuse_cached_annotation", cached_annotation)

    response = client.post("/v1/annotate", json={"fusions": ["EWSR1-FLI1"]})

    assert response.status_code == 200
    body = response.json()
    assert body["annotations"][0]["cache_status"] == "reused"
    assert client.annotated == []
    assert body["warnings"] == [_EWSR1_FLI1_WARNING]


def test_annotate_accepts_gene1_gene2_and_rejects_conflicts(client):
    response = client.post("/v1/annotate", json={"fusions": [{"gene1": "EML4", "gene2": "ALK"}]})
    assert response.status_code == 200
    assert response.json()["warnings"] == []
    assert sorted(client.annotated) == ["ALK", "EML4"]

    conflict = client.post(
        "/v1/annotate",
        json={"fusions": [{"fusion": "BCR::ABL1", "gene1": "EML4", "gene2": "ALK"}]},
    )
    assert conflict.status_code == 422
    assert "conflicts with gene1/gene2" in conflict.text
