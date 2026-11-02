"""Ensembl gene/transcript/protein IDs as gene inputs.

Real normalization, orchestrator and API code; only the Ensembl and HGNC HTTP
boundary (and the per-gene LLM step) is mocked, so ENST/ENSP -> gene -> symbol
resolution, GENE(ENST...) checks and HGVS handling run as in production.
"""

from __future__ import annotations

import json
import time
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import settings
from src.models.schema import FusionEvidenceResult, FusionPositionContext, GeneAnnotation
from src.pipeline import literature, normalization, orchestrator
from src.pipeline.fusion_context import input_position_context
from src.pipeline.normalization import (
    has_accession_identifier,
    normalize_fusions,
    unparsed_fusion_warning,
    unparsed_fusion_warnings,
)

pytestmark = pytest.mark.no_real_redis

_TP53_GENE, _TP53_TX, _TP53_PROTEIN = "ENSG00000141510", "ENST00000269305", "ENSP00000269305"
_EML4_TX, _ALK_TX, _KRAS_TX = "ENST00000318522", "ENST00000389048", "ENST00000256078"
_UNKNOWN_TX = "ENST00000000001"
_TP53_TX_2 = "ENST00000420246"  # a second TP53 transcript

_GENES = {
    _TP53_GENE: "TP53",
    "ENSG00000143924": "EML4",
    "ENSG00000171094": "ALK",
    "ENSG00000133703": "KRAS",
}
_TRANSCRIPTS = {
    _TP53_TX: _TP53_GENE,
    _TP53_TX_2: _TP53_GENE,
    _EML4_TX: "ENSG00000143924",
    _ALK_TX: "ENSG00000171094",
    _KRAS_TX: "ENSG00000133703",
}
_PROTEINS = {_TP53_PROTEIN: _TP53_TX}
_HGNC_SYMBOLS = {"TP53", "EML4", "ALK", "KRAS"}


def _ensembl_doc(stable_id: str):
    if stable_id in _GENES:
        symbol = _GENES[stable_id]
        return {
            "id": stable_id,
            "object_type": "Gene",
            "species": "homo_sapiens",
            "display_name": symbol,
            "description": f"{symbol} gene [Source:HGNC Symbol;Acc:HGNC:{len(symbol)}]",
        }
    if stable_id in _TRANSCRIPTS:
        return {
            "id": stable_id,
            "object_type": "Transcript",
            "species": "homo_sapiens",
            "display_name": f"{_GENES[_TRANSCRIPTS[stable_id]]}-201",
            "Parent": _TRANSCRIPTS[stable_id],
        }
    if stable_id in _PROTEINS:
        return {"id": stable_id, "object_type": "Translation", "species": "homo_sapiens", "Parent": _PROTEINS[stable_id]}
    return None


def _handler(ensembl_offline: bool = False, hgnc_offline: bool = False):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        requests.append(f"{request.url.host}{path}")
        if request.url.host == "rest.ensembl.org":
            if ensembl_offline:
                raise httpx.ConnectError("Ensembl unreachable", request=request)
            if request.method == "POST":  # batch ENSG lookup
                ids = json.loads(request.content)["ids"]
                return httpx.Response(200, json={i: _ensembl_doc(i) for i in ids})
            stable_id = path.removeprefix("/lookup/id/")
            doc = _ensembl_doc(stable_id)
            if doc is None:
                return httpx.Response(400, json={"error": f"ID '{stable_id}' not found"})
            return httpx.Response(200, json=doc)
        if hgnc_offline:
            raise httpx.ConnectError("HGNC unreachable", request=request)
        docs = []
        if path.startswith("/fetch/symbol/"):
            symbol = path.removeprefix("/fetch/symbol/")
            if symbol.upper() in _HGNC_SYMBOLS:
                docs = [{"symbol": symbol.upper(), "hgnc_id": f"HGNC:{symbol.upper()}"}]
        elif path.startswith("/search/symbol/"):
            # Fuzzy like HGNC: 'EML4(ENST...)' would find EML4 if it ever got here.
            text = path.removeprefix("/search/symbol/").upper()
            docs = [{"symbol": s, "hgnc_id": f"HGNC:{s}"} for s in sorted(_HGNC_SYMBOLS) if s in text][:1]
        return httpx.Response(200, json={"response": {"docs": docs}})

    handler.requests = requests
    return handler


@pytest.fixture
def fake_http(monkeypatch):
    real_client = httpx.AsyncClient
    state = {"handler": _handler()}

    def client_factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: state["handler"](request)))

    monkeypatch.setattr("src.pipeline.normalization.httpx.AsyncClient", client_factory)

    async def no_sleep(_seconds):
        pass

    monkeypatch.setattr(normalization, "_sleep", no_sleep)  # Ensembl retry backoff

    def configure(**kwargs):
        state["handler"] = _handler(**kwargs)
        return state["handler"]

    configure.current = lambda: state["handler"]
    return configure


def _hgnc_requests(handler) -> list[str]:
    return [path for path in handler.requests if path.startswith("rest.genenames.org")]


# --- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        _TP53_TX, f"{_TP53_TX}.9", _TP53_PROTEIN, _TP53_GENE, f"{_TP53_GENE}.17", _TP53_TX.lower(),
        f"TP53({_TP53_TX})", f"{_TP53_TX}:c.215C>G", f"{_TP53_TX}.9(TP53):c.215C>G", f"{_TP53_PROTEIN}:p.Arg72Pro",
        f"{_EML4_TX}::{_ALK_TX}", f"EML4({_EML4_TX})::ALK({_ALK_TX})",
        "ENSG123", "ENST0000026930", "ENSMUSG00000017167", f"{_TP53_TX}x", "TP53(ENST123)",
    ],
)
def test_ensembl_forms_are_recognized(value):
    assert has_accession_identifier(value)


@pytest.mark.parametrize("value", ["TP53", "ENSA", "ENSAP1", "EML4::ALK", "HLA-A", "C1orf112", "BRAF_V600E"])
def test_plain_symbols_are_not_accession_forms(value):
    assert not has_accession_identifier(value)


@pytest.mark.parametrize(
    "value",
    [_TP53_GENE, _TP53_TX, f"{_TP53_TX}.9", _TP53_PROTEIN],
)
def test_strict_pattern_accepts_human_stable_ids(value):
    assert normalization._is_ensembl_id(value)


@pytest.mark.parametrize(
    "value",
    ["ENSG123", "ENSG000001415100", "ENST0000026930", "ENSMUSG00000017167", "ENSE00001146308", f"{_TP53_GENE}.x"],
)
def test_strict_pattern_rejects_malformed_or_non_human_ids(value):
    assert not normalization._is_ensembl_id(value)


# --- normalization ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [_TP53_TX, f"{_TP53_TX}.9", _TP53_PROTEIN, f"{_TP53_PROTEIN}.4", _TP53_GENE])
async def test_ensembl_id_resolves_to_owning_gene(fake_http, value):
    handler = fake_http()

    gene_map = await normalize_fusions([value])

    assert list(gene_map) == ["TP53"]
    resolved, inputs = gene_map["TP53"]
    assert resolved.resolved and resolved.input_symbol == value
    assert inputs == ["TP53"]
    assert gene_map.submitted["TP53"] == [value]
    assert gene_map.inputs[value].canonical == "TP53"
    assert gene_map.inputs[value].errors == []
    assert _hgnc_requests(handler) == []  # Ensembl IDs never go to HGNC


@pytest.mark.asyncio
async def test_protein_resolves_through_parent_transcript_with_versionless_lookups(fake_http):
    handler = fake_http()

    gene_map = await normalize_fusions([f"{_TP53_PROTEIN}.4"])

    assert gene_map["TP53"][0].ensembl_transcript_id == _TP53_TX
    assert handler.requests == [
        f"rest.ensembl.org/lookup/id/{_TP53_PROTEIN}",
        f"rest.ensembl.org/lookup/id/{_TP53_TX}",
        f"rest.ensembl.org/lookup/id/{_TP53_GENE}",
    ]


@pytest.mark.asyncio
async def test_transcript_lookups_are_cached(fake_http):
    handler = fake_http()

    await normalize_fusions([_TP53_TX])
    await normalize_fusions([_TP53_TX])

    assert handler.requests.count(f"rest.ensembl.org/lookup/id/{_TP53_TX}") == 1


@pytest.mark.asyncio
async def test_transcript_fusion_resolves_both_partners(fake_http):
    value = f"{_EML4_TX}::{_ALK_TX}.11"
    gene_map = await normalize_fusions([value])

    assert sorted(gene_map) == ["ALK", "EML4"]
    assert gene_map["EML4"][1] == gene_map["ALK"][1] == ["EML4::ALK"]
    reading = gene_map.inputs[value]
    assert reading.canonical == "EML4::ALK"
    assert (reading.five_transcript, reading.three_transcript) == (_EML4_TX, f"{_ALK_TX}.11")


@pytest.mark.asyncio
async def test_decorated_fusion_matching_transcripts_uses_named_genes(fake_http):
    value = f"EML4({_EML4_TX})::ALK({_ALK_TX})"
    gene_map = await normalize_fusions([value])

    assert sorted(gene_map) == ["ALK", "EML4"]
    assert gene_map["EML4"][0].hgnc_id == "HGNC:EML4"  # resolved through HGNC like a plain symbol
    reading = gene_map.inputs[value]
    assert reading.canonical == "EML4::ALK"
    assert (reading.five_transcript, reading.three_transcript) == (_EML4_TX, _ALK_TX)
    assert reading.warnings == reading.errors == []


@pytest.mark.asyncio
async def test_decorated_transcript_of_another_gene_is_an_error(fake_http):
    value = f"EML4({_KRAS_TX})::ALK"
    gene_map = await normalize_fusions([value])

    # EML4 is not silently annotated from the decorated token, nor is KRAS.
    assert sorted(gene_map) == ["ALK", f"EML4({_KRAS_TX})"]
    failed = gene_map[f"EML4({_KRAS_TX})"][0]
    assert failed.unresolvable and not failed.lookup_failed
    assert failed.resolution_error == (
        f"5′ partner of '{value}' rejected — 'EML4({_KRAS_TX})': Ensembl {_KRAS_TX} belongs to KRAS, not EML4. "
        "Correct the gene or the Ensembl ID."
    )
    # The valid partner is still annotated, and the request-level error says so.
    assert gene_map["ALK"][0].resolved and gene_map["ALK"][1] == [value]
    assert gene_map.inputs[value].errors == [
        f"{failed.resolution_error} The 3′ partner is still annotated on its own."
    ]
    assert gene_map.inputs[value].canonical == value


@pytest.mark.asyncio
async def test_single_decorated_gene_mismatch_and_match(fake_http):
    gene_map = await normalize_fusions([f"TP53({_KRAS_TX})", f"KRAS({_KRAS_TX}.4)"])

    assert sorted(gene_map) == ["KRAS", f"TP53({_KRAS_TX})"]
    assert gene_map[f"TP53({_KRAS_TX})"][0].unresolvable


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, variant",
    [
        (f"{_TP53_TX}:c.215C>G", "c.215C>G"),
        (f"{_TP53_TX}.9(TP53):c.215C>G", "c.215C>G"),
        (f"{_TP53_PROTEIN}:p.Arg72Pro", "p.Arg72Pro"),
    ],
)
async def test_hgvs_resolves_gene_and_warns_variant_not_interpreted(fake_http, value, variant):
    gene_map = await normalize_fusions([value])

    assert list(gene_map) == ["TP53"]
    reading = gene_map.inputs[value]
    assert reading.canonical == "TP53"
    assert len(reading.warnings) == 1
    assert "annotated as gene TP53" in reading.warnings[0]
    assert f"variant-level interpretation of {variant} is not supported" in reading.warnings[0]


@pytest.mark.asyncio
async def test_hgvs_naming_another_gene_is_an_error(fake_http):
    value = f"{_TP53_TX}(KRAS):c.215C>G"
    gene_map = await normalize_fusions([value])

    assert list(gene_map) == [value]
    assert gene_map[value][0].resolution_error.startswith(f"'{value}': Ensembl {_TP53_TX} belongs to TP53, not KRAS.")


@pytest.mark.asyncio
async def test_unknown_transcript_is_a_clear_error_not_a_fuzzy_match(fake_http):
    handler = fake_http()

    gene_map = await normalize_fusions([_UNKNOWN_TX, f"EML4({_UNKNOWN_TX})", f"ALK::{_UNKNOWN_TX}.2"])

    assert sorted(gene_map) == ["ALK", f"EML4({_UNKNOWN_TX})", _UNKNOWN_TX, f"{_UNKNOWN_TX}.2"]
    for key in (_UNKNOWN_TX, f"EML4({_UNKNOWN_TX})"):
        resolved = gene_map[key][0]
        assert resolved.unresolvable and not resolved.lookup_failed
        assert resolved.resolution_error == f"Ensembl did not find ID {_UNKNOWN_TX}."
    assert gene_map[f"{_UNKNOWN_TX}.2"][0].resolution_error.startswith(
        f"3′ partner of 'ALK::{_UNKNOWN_TX}.2' rejected — Ensembl did not find ID"
    )
    assert _hgnc_requests(handler) == ["rest.genenames.org/fetch/symbol/ALK"]  # only the plain symbol


_UNKNOWN_MESSAGE = f"Ensembl did not find ID {_UNKNOWN_TX}."
_UNKNOWN_FUSION = f"{_UNKNOWN_TX}::ALK"
_UNKNOWN_PARTNER_MESSAGE = f"5′ partner of '{_UNKNOWN_FUSION}' rejected — {_UNKNOWN_MESSAGE}"


@pytest.mark.asyncio
@pytest.mark.parametrize("reverse", [False, True])
async def test_repeated_failed_token_row_carries_each_inputs_message(fake_http, reverse):
    inputs = [_UNKNOWN_TX, _UNKNOWN_FUSION]
    if reverse:
        inputs.reverse()

    gene_map = await normalize_fusions(inputs)

    # One row for the token, carrying both inputs' own messages in request order.
    expected = [_UNKNOWN_MESSAGE, _UNKNOWN_PARTNER_MESSAGE]
    if reverse:
        expected.reverse()
    assert gene_map[_UNKNOWN_TX][0].resolution_error == " ".join(expected)
    assert gene_map[_UNKNOWN_TX][1] == inputs
    # Each input's own request-level error, whatever the order.
    assert gene_map.inputs[_UNKNOWN_TX].errors == [_UNKNOWN_MESSAGE]
    assert gene_map.inputs[_UNKNOWN_FUSION].errors == [
        f"{_UNKNOWN_PARTNER_MESSAGE} The 3′ partner is still annotated on its own."
    ]


@pytest.mark.asyncio
async def test_same_failed_partner_in_two_fusions_names_each_fusion(fake_http):
    other_fusion = f"EML4::{_UNKNOWN_TX}"
    gene_map = await normalize_fusions([_UNKNOWN_FUSION, other_fusion])

    assert gene_map[_UNKNOWN_TX][0].resolution_error == (
        f"{_UNKNOWN_PARTNER_MESSAGE} 3′ partner of '{other_fusion}' rejected — {_UNKNOWN_MESSAGE}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["ENSG123", "ENSMUSG00000017167", "TP53(ENST123)", f"{_TP53_TX}:x.1", f"EML4({_EML4_TX}) ALK({_ALK_TX}) fusion"],
)
async def test_malformed_ensembl_ids_are_rejected_without_any_lookup(fake_http, value):
    handler = fake_http()

    gene_map = await normalize_fusions([value])

    assert list(gene_map) == [value]
    assert "is not a valid human Ensembl ID" in gene_map[value][0].resolution_error
    assert handler.requests == []


@pytest.mark.asyncio
async def test_ensembl_outage_is_a_retryable_per_input_error(fake_http):
    handler = fake_http(ensembl_offline=True)

    gene_map = await normalize_fusions([_TP53_TX, f"EML4({_EML4_TX})::ALK", "KRAS"])

    tx = gene_map[_TP53_TX][0]
    assert tx.unresolvable and tx.lookup_failed
    assert tx.resolution_error == "Ensembl lookup temporarily unavailable (network error); retry later."
    assert gene_map[f"EML4({_EML4_TX})"][0].lookup_failed
    assert gene_map["KRAS"][0].resolved and gene_map["ALK"][0].resolved
    assert gene_map.inputs[f"EML4({_EML4_TX})::ALK"].lookup_failed
    assert "TP53" not in gene_map and "EML4" not in gene_map
    assert all("EML4(" not in path for path in _hgnc_requests(handler))


@pytest.mark.asyncio
async def test_hgnc_outage_during_decorated_check_is_a_retryable_error(fake_http):
    fake_http(hgnc_offline=True)

    # Same symbol: no HGNC check needed, so it still resolves.
    same = await normalize_fusions([f"KRAS({_KRAS_TX})"])
    differs = await normalize_fusions([f"kras-alias({_KRAS_TX})"])

    assert list(same) == ["KRAS"]
    failed = differs[f"kras-alias({_KRAS_TX})"][0]
    assert failed.lookup_failed
    assert "the HGNC lookup failed" in failed.resolution_error


@pytest.mark.asyncio
async def test_plain_inputs_are_unchanged(fake_http):
    gene_map = await normalize_fusions(["EML4--ALK", "TP53"])

    assert gene_map["EML4"][1] == ["EML4--ALK"]
    assert gene_map.inputs["EML4--ALK"].canonical == "EML4--ALK"
    assert gene_map.inputs["TP53"].warnings == []


# --- fusion-notation warnings (#129) stay quiet -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        _TP53_TX,
        f"{_EML4_TX}::{_ALK_TX}",
        f"EML4({_EML4_TX})::ALK({_ALK_TX})",
        f"EML4({_EML4_TX})",
        f"{_TP53_TX}:c.215C>G",
        f"EML4({_EML4_TX}) ALK({_ALK_TX}) fusion",
        "ENSG123",
    ],
)
async def test_fusion_notation_warning_does_not_fire_on_ensembl_forms(fake_http, value):
    assert unparsed_fusion_warning(value) is None
    assert await unparsed_fusion_warnings([value], await normalize_fusions([value])) == []


# --- API ----------------------------------------------------------------------


class FakeRunStore:
    def __init__(self):
        self.saved = []

    async def save_run(self, run_id, timestamp, request_payload, result_payload, **kwargs):
        self.saved.append(result_payload)

    async def save_gene_annotation(self, annotation, started_at, tumor_type=None):
        pass

    async def get_run(self, run_id):
        return None


@pytest.fixture
def client(monkeypatch, fake_http):
    annotated = {}
    annotated_tumor_types = {}
    real_annotate_gene = orchestrator._annotate_gene

    async def fake_annotate_gene(**kwargs):
        if kwargs["unresolvable"]:
            return await real_annotate_gene(**kwargs)  # production early return; no network
        annotated[kwargs["gene"]] = kwargs["fusions"]
        annotated_tumor_types[kwargs["gene"]] = kwargs["tumor_type"]
        return GeneAnnotation(gene=kwargs["gene"], fusions=kwargs["fusions"], cache_status="refreshed")

    async def no_cached_annotation(**kwargs):
        return None

    async def no_retractions(annotation):
        return set()

    monkeypatch.setattr(main, "run_pipeline", orchestrator.run_pipeline)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(orchestrator, "_maybe_reuse_cached_annotation", no_cached_annotation)
    monkeypatch.setattr(orchestrator, "find_retracted_annotation_pmids", no_retractions, raising=False)
    main.app.state.run_store = FakeRunStore()
    test_client = TestClient(main.app)
    test_client.annotated = annotated
    test_client.tumor_types = annotated_tumor_types
    return test_client


def _poll(client, status_url: str) -> dict:
    deadline = time.time() + 5
    while True:
        body = client.get(status_url).json()
        if body["status"] in ("complete", "failed") or time.time() > deadline:
            return body
        time.sleep(0.02)


def _annotate(client, path: str, payload: dict) -> dict:
    response = client.post(path, json=payload)
    assert response.status_code == 200, response.text
    if path == "/v1/annotate/jobs":
        body = _poll(client, response.json()["status_url"])
        assert body["status"] == "complete", body
        return body["result"]
    return response.json()


@pytest.mark.parametrize("path", ["/v1/annotate", "/v1/annotate/jobs"])
def test_annotate_resolves_transcripts_and_keeps_submitted_inputs(client, path):
    fusion = f"{_EML4_TX}::{_ALK_TX}"
    hgvs = f"{_TP53_TX}:c.215C>G"
    result = _annotate(client, path, {"fusions": [fusion, hgvs, {"fusion": _KRAS_TX, "tumor_type": "LUAD"}]})

    by_gene = {annotation["gene"]: annotation for annotation in result["annotations"]}
    assert sorted(by_gene) == ["ALK", "EML4", "KRAS", "TP53"]
    assert client.annotated["EML4"] == client.annotated["ALK"] == ["EML4::ALK"]
    assert by_gene["EML4"]["fusions"] == ["EML4::ALK"]
    assert by_gene["EML4"]["resolved_inputs"] == [
        {
            "input": fusion,
            "resolved": "EML4::ALK",
            "five_transcript": _EML4_TX,
            "three_transcript": _ALK_TX,
            "warnings": [],
        }
    ]
    assert by_gene["KRAS"]["resolved_inputs"][0]["input"] == _KRAS_TX
    assert by_gene["TP53"]["resolved_inputs"][0]["warnings"] == result["warnings"]
    assert "variant-level interpretation of c.215C>G is not supported" in result["warnings"][0]


def test_annotate_reports_mismatch_and_unknown_ids_per_input(client):
    result = _annotate(client, "/v1/annotate", {"fusions": [f"EML4({_KRAS_TX})::ALK", _UNKNOWN_TX]})

    by_gene = {annotation["gene"]: annotation for annotation in result["annotations"]}
    assert sorted(by_gene) == ["ALK", f"EML4({_KRAS_TX})", _UNKNOWN_TX]
    assert "EML4" not in client.annotated and "KRAS" not in client.annotated
    assert client.annotated["ALK"] == [f"EML4({_KRAS_TX})::ALK"]  # the valid partner is still annotated
    assert by_gene[f"EML4({_KRAS_TX})"]["error"] == (
        f"Unresolvable gene symbol — 5′ partner of 'EML4({_KRAS_TX})::ALK' rejected — "
        f"'EML4({_KRAS_TX})': Ensembl {_KRAS_TX} belongs to KRAS, not EML4. Correct the gene or the Ensembl ID."
    )
    assert by_gene[_UNKNOWN_TX]["error"] == (
        f"Unresolvable gene symbol — Ensembl did not find ID {_UNKNOWN_TX}."
    )
    assert len(result["warnings"]) == 2


# Two spellings of TP53 in each pair; the first-submitted tumor type must win.
_SAME_GENE_SPELLINGS = [
    ("TP53", _TP53_TX),
    ("TP53", _TP53_GENE),
    (_TP53_TX, _TP53_TX_2),
    (f"TP53({_TP53_TX})", _TP53_TX_2),
    (f"TP53({_TP53_TX})", "TP53"),
    (_TP53_PROTEIN, f"{_TP53_TX}.9"),
    (f"{_TP53_TX}:c.215C>G", "TP53"),
    # Case variants merge into one gene; request order, not spelling sort order, decides.
    ("tp53", "TP53"),
    ("Tp53", "tp53"),
    ("tp53", _TP53_TX),
]


@pytest.mark.parametrize("first, second", _SAME_GENE_SPELLINGS)
@pytest.mark.parametrize("reverse", [False, True])
def test_first_submitted_tumor_type_wins_across_identifier_types(client, first, second, reverse):
    rows = [{"fusion": first, "tumor_type": "LUAD"}, {"fusion": second, "tumor_type": "BRCA"}]
    if reverse:
        rows.reverse()
    winner, loser = rows[0]["tumor_type"], rows[1]["tumor_type"]

    result = _annotate(client, "/v1/annotate", {"fusions": rows})

    assert [annotation["gene"] for annotation in result["annotations"]] == ["TP53"]
    assert client.tumor_types == {"TP53": winner}
    assert f"TP53: using tumor type {winner!r}; ignored tumor types: {loser!r}." in result["warnings"]


def test_first_tumor_type_wins_when_earlier_input_has_none(client):
    rows = ["TP53", {"fusion": _TP53_TX, "tumor_type": "BRCA"}, {"fusion": "TP53", "tumor_type": "LUAD"}]

    result = _annotate(client, "/v1/annotate", {"fusions": rows})

    assert client.tumor_types == {"TP53": "BRCA"}
    assert "TP53: using tumor type 'BRCA'; ignored tumor types: 'LUAD'." in result["warnings"]


def test_gene_query_first_submitted_tumor_type_wins(client):
    genes = [{"gene": "TP53", "tumor_type": "LUAD"}, {"gene": _TP53_TX, "tumor_type": "BRCA"}]

    for order in (genes, genes[::-1]):
        client.tumor_types.clear()
        body = client.post("/v1/genes/query", json={"genes": order}).json()
        assert client.tumor_types == {"TP53": order[0]["tumor_type"]}
        assert body["results"][0]["tumor_type"] == order[0]["tumor_type"]


@pytest.mark.parametrize("reverse", [False, True])
def test_annotate_error_row_names_rejected_partner_in_either_order(client, reverse):
    inputs = [_UNKNOWN_TX, _UNKNOWN_FUSION]
    if reverse:
        inputs.reverse()

    result = _annotate(client, "/v1/annotate", {"fusions": inputs})

    row = next(annotation for annotation in result["annotations"] if annotation["gene"] == _UNKNOWN_TX)
    messages = [_UNKNOWN_MESSAGE, _UNKNOWN_PARTNER_MESSAGE]
    if reverse:
        messages.reverse()
    assert row["error"] == "Unresolvable gene symbol — " + " ".join(messages)
    assert row["fusions"] == [_UNKNOWN_FUSION]
    assert client.annotated == {"ALK": [_UNKNOWN_FUSION]}


def test_annotate_gene_endpoint_resolves_protein_id(client):
    response = client.post("/v1/annotate/gene", json={"gene": _TP53_PROTEIN})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["gene"] == "TP53"
    assert body["resolved_inputs"][0]["input"] == _TP53_PROTEIN


@pytest.mark.parametrize("route", ["sync", "job"])
def test_gene_query_accepts_ensembl_forms(client, route):
    genes = [_TP53_TX, f"KRAS({_KRAS_TX})", f"EML4({_KRAS_TX})", f"{_ALK_TX}:c.3522C>A"]
    if route == "sync":
        response = client.post("/v1/genes/query", json={"genes": genes})
        assert response.status_code == 200, response.text
        body = response.json()
    else:
        created = client.post("/v1/genes/query/jobs", json={"genes": genes})
        assert created.status_code == 200, created.text
        body = _poll(client, created.json()["status_url"])

    by_gene = {item["gene"]: item for item in body["results"]}
    assert by_gene["TP53"]["submitted_inputs"] == [_TP53_TX]
    assert by_gene["KRAS"]["submitted_inputs"] == [f"KRAS({_KRAS_TX})"]
    assert "variant-level interpretation" in by_gene["ALK"]["warnings"][0]
    assert by_gene[f"EML4({_KRAS_TX})"]["error"] == "The Ensembl ID belongs to a different gene than the one named."


def test_gene_query_get_unknown_transcript_is_404_and_outage_is_503(client, fake_http):
    assert client.get(f"/v1/genes/{_UNKNOWN_TX}").status_code == 404
    fake_http(ensembl_offline=True)
    response = client.get(f"/v1/genes/{_KRAS_TX}")
    assert response.status_code == 503


# --- fusion evidence ----------------------------------------------------------


@pytest.fixture
def evidence_calls(monkeypatch):
    calls = []

    async def fake_uncached(fusion, tumor_type, max_results):
        calls.append(fusion)
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, interpretation="ok")

    monkeypatch.setattr(literature, "_retrieve_fusion_evidence_uncached", fake_uncached)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fusion",
    [f"{_EML4_TX}::{_ALK_TX}", "ENSG00000143924::ENSG00000171094", f"EML4({_EML4_TX})::ALK"],
)
async def test_fusion_evidence_queries_symbols_not_ensembl_ids(fake_http, evidence_calls, fusion):
    result = await literature.retrieve_fusion_evidence(fusion)

    assert evidence_calls == ["EML4::ALK"]
    assert result.fusion == "EML4::ALK"
    assert result.input == fusion


@pytest.mark.asyncio
async def test_fusion_evidence_with_unresolvable_partner_skips_pubmed(fake_http, evidence_calls):
    result = await literature.retrieve_fusion_evidence(f"{_UNKNOWN_TX}::ALK")

    assert evidence_calls == []
    assert result.interpretation.startswith("Fusion evidence was not retrieved:")
    assert _UNKNOWN_TX in result.interpretation
    # Rejected before any search: not evidence of missing literature.
    assert result.retrieval_status == "failed" and result.literature_scope is None


@pytest.mark.asyncio
async def test_fusion_evidence_cache_key_and_tumor_scope_use_symbols(fake_http, monkeypatch):
    keys, scoped = [], []

    async def fake_cached_call(key, compute, ttl_seconds=None, force_refresh=False):
        keys.append(key)
        return await compute()

    async def fake_uncached(fusion, tumor_type, max_results):
        scoped.append((fusion, tumor_type))
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, literature_scope="requested_tumor")

    monkeypatch.setattr(literature, "cached_call", fake_cached_call)
    monkeypatch.setattr(literature, "_retrieve_fusion_evidence_uncached", fake_uncached)

    result = await literature.retrieve_fusion_evidence(f"{_EML4_TX}::{_ALK_TX}", tumor_type="NSCLC", max_results=5)

    assert scoped == [("EML4::ALK", "NSCLC")]
    assert keys == [literature._fusion_evidence_cache_key("EML4::ALK", "NSCLC", 5)]
    assert result.literature_scope == "requested_tumor" and result.input == f"{_EML4_TX}::{_ALK_TX}"


@pytest.mark.asyncio
async def test_fusion_evidence_plain_fusion_is_unchanged(fake_http, evidence_calls):
    handler = fake_http()

    result = await literature.retrieve_fusion_evidence("EML4::ALK")

    assert evidence_calls == ["EML4::ALK"] and result.input is None
    assert handler.requests == []


def test_fusion_evidence_job_resolves_ensembl_partners(client, evidence_calls):
    created = client.post("/v1/fusion-evidence/jobs", json={"fusions": [f"{_EML4_TX}::{_ALK_TX}"]})
    assert created.status_code == 200, created.text
    body = _poll(client, created.json()["status_url"])

    assert body["status"] == "complete"
    assert evidence_calls == ["EML4::ALK"]
    assert body["fusion_evidence"][0]["input"] == f"{_EML4_TX}::{_ALK_TX}"


# --- fusion context -----------------------------------------------------------


@pytest.fixture
def fusion_context_calls(monkeypatch):
    calls = []

    async def fake_contexts(parsed_inputs):
        calls.extend(parsed_inputs)
        return [input_position_context(parsed) for parsed in parsed_inputs]

    monkeypatch.setattr(settings, "fusion_annotation_api_enabled", True)
    monkeypatch.setattr(settings, "fusion_annotation_api_base_url", "http://fusion-annotation.test")
    monkeypatch.setattr(main, "annotate_fusion_position_contexts", fake_contexts)
    return calls


def test_fusion_context_passes_decorated_transcripts(client, fusion_context_calls):
    response = client.post("/v1/fusion-context", json={"fusion": f"EML4({_EML4_TX})::ALK({_ALK_TX})"})

    assert response.status_code == 200, response.text
    parsed = fusion_context_calls[0]
    assert (parsed.five_gene, parsed.three_gene) == ("EML4", "ALK")
    assert (parsed.five_transcript, parsed.three_transcript) == (_EML4_TX, _ALK_TX)
    context = FusionPositionContext(**response.json()["context"])
    assert context.five_prime.transcript_id == _EML4_TX


def test_fusion_context_explicit_transcript_wins(client, fusion_context_calls):
    response = client.post(
        "/v1/fusion-context",
        json={"fusion": f"{_EML4_TX}::{_ALK_TX}", "five_transcript": "ENST00000999999"},
    )

    assert response.status_code == 200, response.text
    parsed = fusion_context_calls[0]
    assert (parsed.five_gene, parsed.five_transcript, parsed.three_transcript) == ("EML4", "ENST00000999999", _ALK_TX)


def test_fusion_context_rejects_mismatched_transcript(client, fusion_context_calls):
    response = client.post("/v1/fusion-context", json={"fusion": f"EML4({_KRAS_TX})::ALK"})

    assert response.status_code == 400
    assert "belongs to KRAS, not EML4" in response.json()["detail"]
    assert fusion_context_calls == []
