"""RefSeq transcript/protein accessions as gene inputs.

Real normalization, orchestrator and API code; only the NCBI, Ensembl and HGNC
HTTP boundary (and the per-gene LLM step) is mocked, so NM_/NP_ -> gene ->
symbol resolution, GENE(NM_...) checks and HGVS handling run as in production.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from email.utils import formatdate
from urllib.parse import unquote

import httpx
import pytest
from fastapi.testclient import TestClient

from src import main
from src.config import settings
from src.models.schema import FusionEvidenceResult, GeneAnnotation
from src.pipeline import literature, normalization, orchestrator
from src.pipeline.fusion_context import input_position_context
from src.pipeline.normalization import (
    has_accession_identifier,
    normalize_fusions,
    unparsed_fusion_warning,
    unparsed_fusion_warnings,
)

pytestmark = pytest.mark.no_real_redis

_NCBI_HOST = "api.ncbi.nlm.nih.gov"
_NCBI_PREFIX = "/datasets/v2/gene/accession/"
_TP53_NM, _TP53_NP = "NM_000546", "NP_000537"
_EML4_NM, _ALK_NM, _KRAS_NM = "NM_019063", "NM_004304", "NM_004985"
_MOUSE_TRP53_NM = "NM_011640"
_UNKNOWN_NM = "NM_999999999"
_TP53_ENST = "ENST00000269305"

# Unversioned accession -> (symbol, tax_id), as NCBI Datasets reports it.
_NCBI_GENES = {
    _TP53_NM: ("TP53", 9606),
    _TP53_NP: ("TP53", 9606),
    _EML4_NM: ("EML4", 9606),
    _ALK_NM: ("ALK", 9606),
    _KRAS_NM: ("KRAS", 9606),
    _MOUSE_TRP53_NM: ("Trp53", 10090),
}
_HGNC_SYMBOLS = {"TP53", "EML4", "ALK", "KRAS"}


def _ncbi_report(accession: str) -> dict:
    if accession not in _NCBI_GENES:
        return {}  # what NCBI returns (HTTP 200) for an accession it doesn't know
    symbol, tax_id = _NCBI_GENES[accession]
    gene = {"gene_id": "1", "symbol": symbol, "description": f"{symbol} gene", "tax_id": str(tax_id)}
    if tax_id == 9606:
        gene["nomenclature_authority"] = {"authority": "HGNC", "identifier": f"HGNC:{symbol}"}
    return {"reports": [{"gene": gene, "query": [accession]}], "total_count": 1}


def _handler(ncbi_offline: bool = False, ncbi_status: int = 200, ncbi_429s: int = 0, ncbi_responder=None):
    requests: list[str] = []
    throttled = {"left": ncbi_429s}

    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        requests.append(f"{request.url.host}{path}")
        if request.url.host == _NCBI_HOST:
            if ncbi_offline:
                raise httpx.ConnectError("NCBI unreachable", request=request)
            if ncbi_responder is not None:
                response = ncbi_responder(request)
                if response is not None:
                    return response
            if ncbi_status != 200:
                return httpx.Response(ncbi_status, json={"error": "busy"})
            if throttled["left"]:
                throttled["left"] -= 1
                return httpx.Response(429, json={"error": "API rate limit exceeded"})
            return httpx.Response(200, json=_ncbi_report(path.removeprefix(_NCBI_PREFIX)))
        if request.url.host == "rest.ensembl.org":
            stable_id = path.removeprefix("/lookup/id/")
            docs = {
                _TP53_ENST: {"object_type": "Transcript", "species": "homo_sapiens", "Parent": "ENSG00000141510"},
                "ENSG00000141510": {
                    "object_type": "Gene", "species": "homo_sapiens", "display_name": "TP53", "description": "",
                },
            }
            if stable_id not in docs:
                return httpx.Response(400, json={"error": "not found"})
            return httpx.Response(200, json={"id": stable_id, **docs[stable_id]})
        docs = []
        if path.startswith("/fetch/symbol/"):
            symbol = path.removeprefix("/fetch/symbol/")
            if symbol.upper() in _HGNC_SYMBOLS:
                docs = [{"symbol": symbol.upper(), "hgnc_id": f"HGNC:{symbol.upper()}"}]
        elif path.startswith("/search/symbol/"):
            # Fuzzy like HGNC: 'TP53(NM_000546)' would find TP53 if it ever got here.
            text = path.removeprefix("/search/symbol/").upper()
            docs = [{"symbol": s, "hgnc_id": f"HGNC:{s}"} for s in sorted(_HGNC_SYMBOLS) if s in text][:1]
        return httpx.Response(200, json={"response": {"docs": docs}})

    handler.requests = requests
    return handler


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(normalization, "NCBI_RATE_LIMIT_BACKOFF_SECONDS", 0)
    real_client = httpx.AsyncClient
    state = {"handler": _handler()}

    def client_factory(*args, **kwargs):
        return real_client(transport=httpx.MockTransport(lambda request: state["handler"](request)))

    monkeypatch.setattr("src.pipeline.normalization.httpx.AsyncClient", client_factory)

    def configure(**kwargs):
        state["handler"] = _handler(**kwargs)
        return state["handler"]

    return configure


def _ncbi_requests(handler) -> list[str]:
    return [path.removeprefix(_NCBI_HOST + _NCBI_PREFIX) for path in handler.requests if path.startswith(_NCBI_HOST)]


def _hgnc_requests(handler) -> list[str]:
    return [path for path in handler.requests if path.startswith("rest.genenames.org")]


# --- parsing ------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        _TP53_NM, f"{_TP53_NM}.6", _TP53_NP, "nm_000546.6", "NR_003051", "XM_011544358.2", "XR_001737578", "XP_011542660",
        f"TP53({_TP53_NM})", f"TP53({_TP53_NM}.6)", f"{_TP53_NM}.6:c.215C>G", f"{_TP53_NM}.6(TP53):c.215C>G",
        f"{_TP53_NP}.3:p.Arg72Pro", f"EML4({_EML4_NM}.5)::ALK({_ALK_NM}.5)", f"{_EML4_NM}::{_ALK_NM}",
        "NM_12", "NC_000017.11", "NG_017013", f"{_TP53_NM}.x", f"{_TP53_NM}:x.1",
    ],
)
def test_refseq_forms_are_recognized(value):
    assert has_accession_identifier(value)


@pytest.mark.parametrize("value", [_TP53_NM, f"{_TP53_NM}.6", _TP53_NP, "NR_003051", "XM_011544358.2", "xp_011542660"])
def test_strict_pattern_accepts_refseq_transcripts_and_proteins(value):
    assert normalization._is_refseq_id(value)


@pytest.mark.parametrize(
    "value", ["NM_12", "NM_0005461234567", "NC_000017.11", "NG_017013", "NT_187361", f"{_TP53_NM}.x", "ZM_000546"]
)
def test_strict_pattern_rejects_other_or_malformed_accessions(value):
    assert not normalization._is_refseq_id(value)


# --- normalization ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, transcript",
    [(_TP53_NM, _TP53_NM), (f"{_TP53_NM}.6", f"{_TP53_NM}.6"), (_TP53_NP, None), (f"{_TP53_NP}.3", None)],
)
async def test_bare_accession_resolves_to_owning_gene_with_versionless_lookup(fake_http, value, transcript):
    handler = fake_http()

    gene_map = await normalize_fusions([value])

    assert list(gene_map) == ["TP53"]
    resolved = gene_map["TP53"][0]
    assert resolved.resolved and resolved.hgnc_id == "HGNC:TP53"
    assert resolved.refseq_transcript_id == transcript  # proteins name no transcript
    assert gene_map["TP53"][1] == ["TP53"]
    assert gene_map.submitted["TP53"] == [value]
    assert gene_map.inputs[value].errors == []
    assert _ncbi_requests(handler) == [value.split(".")[0]]
    assert _hgnc_requests(handler) == []  # RefSeq accessions never go to HGNC


@pytest.mark.asyncio
async def test_refseq_lookups_are_cached_and_rate_limited(fake_http, monkeypatch):
    handler = fake_http()
    acquired = []

    @contextlib.asynccontextmanager
    async def counting_limiter():
        acquired.append(1)
        yield

    monkeypatch.setattr(literature, "_get_ncbi_rate_limiter", counting_limiter)

    await normalize_fusions([_TP53_NM])
    await normalize_fusions([f"{_TP53_NM}.6", f"TP53({_TP53_NM}.6)"])

    assert _ncbi_requests(handler) == [_TP53_NM]
    assert len(acquired) == 1  # only the live call takes a token


@pytest.mark.asyncio
async def test_rate_limited_lookup_is_retried(fake_http):
    handler = fake_http(ncbi_429s=2)

    gene_map = await normalize_fusions([_TP53_NM])

    assert list(gene_map) == ["TP53"]
    assert _ncbi_requests(handler) == [_TP53_NM] * 3


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive the NCBI retry loop's clock: each mocked sleep advances it."""
    clock = {"now": 0.0, "sleeps": []}

    async def fake_sleep(seconds):
        clock["sleeps"].append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(normalization, "_monotonic", lambda: clock["now"])
    monkeypatch.setattr(normalization, "_sleep", fake_sleep)
    return clock


def _throttled_until(clock, recover_at: float, retry_after: str | None):
    """NCBI responder: 429 (with an optional Retry-After) until the fake clock reaches recover_at."""

    def respond(request):
        if clock["now"] >= recover_at:
            return None  # normal answer
        headers = {"Retry-After": retry_after} if retry_after is not None else {}
        return httpx.Response(429, headers=headers, json={"error": "API rate limit exceeded"})

    return respond


@pytest.mark.asyncio
async def test_retry_after_seconds_is_honored(fake_http, fake_clock):
    handler = fake_http(ncbi_responder=_throttled_until(fake_clock, recover_at=5, retry_after="5"))

    gene_map = await normalize_fusions([_TP53_NM])

    assert list(gene_map) == ["TP53"]
    assert fake_clock["sleeps"] == [5.0]  # waited what NCBI asked, not the 0.5 s backoff
    assert _ncbi_requests(handler) == [_TP53_NM] * 2


@pytest.mark.asyncio
async def test_retry_after_http_date_is_honored(fake_http, fake_clock):
    retry_at = formatdate(time.time() + 4, usegmt=True)
    fake_http(ncbi_responder=_throttled_until(fake_clock, recover_at=2, retry_after=retry_at))

    gene_map = await normalize_fusions([_TP53_NM])

    assert list(gene_map) == ["TP53"]
    [slept] = fake_clock["sleeps"]
    assert 2.5 <= slept <= 4.0  # HTTP-date has 1 s resolution


@pytest.mark.asyncio
async def test_retry_after_beyond_the_deadline_fails_fast(fake_http, fake_clock):
    handler = fake_http(ncbi_responder=_throttled_until(fake_clock, recover_at=60, retry_after="30"))

    gene_map = await normalize_fusions([_TP53_NM])

    failed = gene_map[_TP53_NM][0]
    assert failed.lookup_failed
    assert failed.resolution_error == f"The NCBI RefSeq lookup for {_TP53_NM} failed; please retry."
    assert fake_clock["sleeps"] == [] and _ncbi_requests(handler) == [_TP53_NM]


@pytest.mark.asyncio
async def test_repeated_retry_after_stops_within_the_deadline(fake_http, fake_clock):
    handler = fake_http(ncbi_responder=_throttled_until(fake_clock, recover_at=60, retry_after="6"))

    gene_map = await normalize_fusions([_TP53_NM])

    assert gene_map[_TP53_NM][0].lookup_failed
    # 0 s and 6 s attempts sleep 6 s each; at 12 s only 3 s of the 15 s budget is left.
    assert fake_clock["sleeps"] == [6.0, 6.0]
    assert sum(fake_clock["sleeps"]) <= normalization.NCBI_RETRY_DEADLINE_SECONDS
    assert len(_ncbi_requests(handler)) == 3

    fake_http()  # the rate-limit failure was not cached
    assert list(await normalize_fusions([_TP53_NM])) == ["TP53"]


@pytest.mark.asyncio
@pytest.mark.parametrize("retry_after", [None, "soon"])
async def test_missing_or_unparseable_retry_after_falls_back_to_backoff(fake_http, fake_clock, monkeypatch, retry_after):
    monkeypatch.setattr(normalization, "NCBI_RATE_LIMIT_BACKOFF_SECONDS", 0.5)  # fake_http zeroes it
    handler = fake_http(ncbi_responder=_throttled_until(fake_clock, recover_at=60, retry_after=retry_after))

    gene_map = await normalize_fusions([_TP53_NM])

    assert gene_map[_TP53_NM][0].lookup_failed
    assert fake_clock["sleeps"] == [0.5, 1.0, 2.0]
    assert len(_ncbi_requests(handler)) == normalization.NCBI_RATE_LIMIT_RETRIES + 1


@pytest.mark.asyncio
async def test_limiter_wait_counts_against_the_deadline(fake_http, monkeypatch):
    handler = fake_http()
    monkeypatch.setattr(normalization, "NCBI_RETRY_DEADLINE_SECONDS", 0.05)

    @contextlib.asynccontextmanager
    async def stuck_limiter():
        await asyncio.Event().wait()  # never gets a token
        yield

    monkeypatch.setattr(literature, "_get_ncbi_rate_limiter", stuck_limiter)

    gene_map = await normalize_fusions([_TP53_NM])

    failed = gene_map[_TP53_NM][0]
    assert failed.lookup_failed and failed.unresolvable
    assert _ncbi_requests(handler) == []


@pytest.mark.parametrize(
    "value, expected",
    [("5", 5.0), ("0.25", 0.25), ("-3", 0.0), ("", None), ("nan", None), ("not a date", None)],
)
def test_retry_after_parsing(value, expected):
    response = httpx.Response(429, headers={"Retry-After": value} if value else {})
    assert normalization._retry_after_seconds(response) == expected


def test_retry_after_past_http_date_is_zero():
    response = httpx.Response(429, headers={"Retry-After": formatdate(time.time() - 60, usegmt=True)})
    assert normalization._retry_after_seconds(response) == 0.0


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [f"TP53({_TP53_NM})", f"TP53({_TP53_NM}.6)", f"tp53( {_TP53_NM}.6 )"])
async def test_decorated_gene_matching_its_accession_is_annotated(fake_http, value):
    handler = fake_http()

    gene_map = await normalize_fusions([value])

    assert list(gene_map) == ["TP53"]
    assert gene_map["TP53"][0].hgnc_id == "HGNC:TP53"
    reading = gene_map.inputs[value]
    assert reading.canonical.upper() == "TP53" and reading.errors == reading.warnings == []
    # The named gene goes through an exact HGNC fetch, never fuzzy search of the decorated token.
    assert all("(" not in path for path in _hgnc_requests(handler))


@pytest.mark.asyncio
async def test_decorated_fusion_passes_partner_accessions_as_transcripts(fake_http):
    value = f"EML4({_EML4_NM}.5)::ALK({_ALK_NM}.5)"

    gene_map = await normalize_fusions([value])

    assert sorted(gene_map) == ["ALK", "EML4"]
    assert gene_map["EML4"][1] == gene_map["ALK"][1] == ["EML4::ALK"]
    reading = gene_map.inputs[value]
    assert reading.canonical == "EML4::ALK"
    assert (reading.five_transcript, reading.three_transcript) == (f"{_EML4_NM}.5", f"{_ALK_NM}.5")


@pytest.mark.asyncio
async def test_bare_accession_fusion_resolves_both_partners(fake_http):
    value = f"{_EML4_NM}.5::{_ALK_NM}"

    reading = (await normalize_fusions([value])).inputs[value]

    assert reading.canonical == "EML4::ALK"
    assert (reading.five_transcript, reading.three_transcript) == (f"{_EML4_NM}.5", _ALK_NM)


@pytest.mark.asyncio
async def test_mixed_ensembl_and_refseq_partners(fake_http):
    value = f"TP53({_TP53_ENST})::ALK({_ALK_NM})"

    reading = (await normalize_fusions([value])).inputs[value]

    assert reading.canonical == "TP53::ALK"
    assert (reading.five_transcript, reading.three_transcript) == (_TP53_ENST, _ALK_NM)


@pytest.mark.asyncio
async def test_decorated_accession_of_another_gene_is_an_error(fake_http):
    value = f"EML4({_KRAS_NM})::ALK"

    gene_map = await normalize_fusions([value])

    assert sorted(gene_map) == ["ALK", f"EML4({_KRAS_NM})"]
    failed = gene_map[f"EML4({_KRAS_NM})"][0]
    assert failed.unresolvable and not failed.lookup_failed
    assert failed.resolution_error == (
        f"5′ partner of '{value}' rejected — 'EML4({_KRAS_NM})': RefSeq {_KRAS_NM} belongs to KRAS, not EML4. "
        "Correct the gene or the RefSeq accession."
    )
    assert gene_map["ALK"][0].resolved and gene_map["ALK"][1] == [value]
    assert gene_map.inputs[value].errors == [f"{failed.resolution_error} The 3′ partner is still annotated on its own."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, variant",
    [
        (f"{_TP53_NM}.6:c.215C>G", "c.215C>G"),
        (f"{_TP53_NM}.6(TP53):c.215C>G", "c.215C>G"),
        (f"{_TP53_NP}.3:p.Arg72Pro", "p.Arg72Pro"),
    ],
)
async def test_hgvs_resolves_gene_and_warns_variant_not_interpreted(fake_http, value, variant):
    gene_map = await normalize_fusions([value])

    assert list(gene_map) == ["TP53"]
    reading = gene_map.inputs[value]
    assert reading.canonical == "TP53" and reading.errors == []
    assert reading.warnings == [
        f"'{value}' was annotated as gene TP53 (from {value.split(':')[0].split('(')[0]}); "
        f"variant-level interpretation of {variant} is not supported."
    ]


@pytest.mark.asyncio
async def test_hgvs_naming_another_gene_is_an_error(fake_http):
    value = f"{_TP53_NM}.6(KRAS):c.215C>G"

    gene_map = await normalize_fusions([value])

    assert list(gene_map) == [value]
    assert gene_map[value][0].resolution_error.startswith(f"'{value}': RefSeq {_TP53_NM}.6 belongs to TP53, not KRAS.")


@pytest.mark.asyncio
@pytest.mark.parametrize("accession", [_UNKNOWN_NM, _MOUSE_TRP53_NM])
async def test_unknown_or_non_human_accession_is_a_clear_error_not_a_fuzzy_match(fake_http, accession):
    handler = fake_http()

    gene_map = await normalize_fusions([accession, f"TP53({accession})"])

    for key in (accession, f"TP53({accession})"):
        resolved = gene_map[key][0]
        assert resolved.unresolvable and not resolved.lookup_failed
        assert resolved.resolution_error == f"NCBI has no human RefSeq transcript or protein {accession}."
    assert "TP53" not in gene_map and "Trp53" not in gene_map
    assert _hgnc_requests(handler) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["NM_12", "NC_000017.11", "NG_017013", f"{_TP53_NM}.x", f"{_TP53_NM}:x.1", "TP53(NM_12)", "BRCA1 NM_007294"],
)
async def test_malformed_or_unsupported_accessions_are_rejected_without_any_lookup(fake_http, value):
    handler = fake_http()

    gene_map = await normalize_fusions([value])

    assert list(gene_map) == [value]
    assert "is not a valid human RefSeq transcript or protein accession" in gene_map[value][0].resolution_error
    assert handler.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [{"ncbi_offline": True}, {"ncbi_status": 429}, {"ncbi_status": 503}])
async def test_ncbi_outage_is_a_retryable_error_and_is_not_cached(fake_http, failure):
    fake_http(**failure)

    gene_map = await normalize_fusions([_TP53_NM, f"EML4({_EML4_NM})::ALK"])

    failed = gene_map[_TP53_NM][0]
    assert failed.unresolvable and failed.lookup_failed
    assert failed.resolution_error == f"The NCBI RefSeq lookup for {_TP53_NM} failed; please retry."
    assert gene_map.inputs[f"EML4({_EML4_NM})::ALK"].lookup_failed
    assert "TP53" not in gene_map and "EML4" not in gene_map and gene_map["ALK"][0].resolved

    fake_http()  # NCBI is back: the failure was not cached as "absent"
    assert list(await normalize_fusions([_TP53_NM])) == ["TP53"]


@pytest.mark.asyncio
async def test_resolve_gene_handles_refseq_without_hgnc(fake_http):
    handler = fake_http()

    async with httpx.AsyncClient() as client:
        bare = await normalization.resolve_gene(f"{_TP53_NM}.6", client)
        decorated = await normalization.resolve_gene(f"TP53({_TP53_NM})", client)

    assert bare.canonical_symbol == "TP53" and bare.resolved
    assert decorated.unresolvable
    assert _hgnc_requests(handler) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [_TP53_NM, f"TP53({_TP53_NM})", f"{_EML4_NM}::{_ALK_NM}", f"{_TP53_NM}.6:c.215C>G", "NM_12", "EML4 NM_019063 fusion"],
)
async def test_fusion_notation_warning_does_not_fire_on_refseq_forms(fake_http, value):
    assert unparsed_fusion_warning(value) is None
    assert await unparsed_fusion_warnings([value], await normalize_fusions([value])) == []


# --- API ----------------------------------------------------------------------


class FakeRunStore:
    async def save_run(self, run_id, timestamp, request_payload, result_payload, **kwargs):
        pass

    async def save_gene_annotation(self, annotation, started_at, tumor_type=None):
        pass

    async def get_run(self, run_id):
        return None


@contextlib.asynccontextmanager
async def _no_limiter():
    yield


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

    # TestClient runs each request on a new event loop; an AsyncLimiter must not
    # span loops (the limiter itself is covered by the normalization tests).
    monkeypatch.setattr(literature, "_get_ncbi_rate_limiter", _no_limiter)
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


def test_annotate_resolves_refseq_forms_and_keeps_submitted_inputs(client):
    fusion = f"EML4({_EML4_NM}.5)::ALK({_ALK_NM}.5)"
    hgvs = f"{_TP53_NM}.6:c.215C>G"

    response = client.post("/v1/annotate", json={"fusions": [fusion, hgvs, {"fusion": _KRAS_NM, "tumor_type": "LUAD"}]})

    assert response.status_code == 200, response.text
    result = response.json()
    by_gene = {annotation["gene"]: annotation for annotation in result["annotations"]}
    assert sorted(by_gene) == ["ALK", "EML4", "KRAS", "TP53"]
    assert client.annotated["EML4"] == client.annotated["ALK"] == ["EML4::ALK"]
    assert by_gene["EML4"]["resolved_inputs"] == [
        {
            "input": fusion,
            "resolved": "EML4::ALK",
            "five_transcript": f"{_EML4_NM}.5",
            "three_transcript": f"{_ALK_NM}.5",
            "warnings": [],
        }
    ]
    assert by_gene["KRAS"]["resolved_inputs"][0]["input"] == _KRAS_NM
    assert client.tumor_types["KRAS"] == "LUAD"
    assert by_gene["TP53"]["resolved_inputs"][0]["warnings"] == result["warnings"]
    assert "variant-level interpretation of c.215C>G is not supported" in result["warnings"][0]


def test_annotate_reports_mismatch_and_unknown_accessions_per_input(client):
    response = client.post("/v1/annotate", json={"fusions": [f"EML4({_KRAS_NM})::ALK", _UNKNOWN_NM]})

    result = response.json()
    by_gene = {annotation["gene"]: annotation for annotation in result["annotations"]}
    assert sorted(by_gene) == ["ALK", f"EML4({_KRAS_NM})", _UNKNOWN_NM]
    assert client.annotated == {"ALK": [f"EML4({_KRAS_NM})::ALK"]}
    assert by_gene[f"EML4({_KRAS_NM})"]["error"].endswith(
        f"RefSeq {_KRAS_NM} belongs to KRAS, not EML4. Correct the gene or the RefSeq accession."
    )
    assert by_gene[_UNKNOWN_NM]["error"] == (
        f"Unresolvable gene symbol — NCBI has no human RefSeq transcript or protein {_UNKNOWN_NM}."
    )
    assert len(result["warnings"]) == 2


@pytest.mark.parametrize(
    "first, second",
    [("TP53", _TP53_NM), (_TP53_NP, f"TP53({_TP53_NM}.6)"), (_TP53_ENST, f"{_TP53_NM}.6:c.215C>G")],
)
@pytest.mark.parametrize("reverse", [False, True])
def test_first_submitted_tumor_type_wins_across_identifier_types(client, first, second, reverse):
    rows = [{"fusion": first, "tumor_type": "LUAD"}, {"fusion": second, "tumor_type": "BRCA"}]
    if reverse:
        rows.reverse()
    winner, loser = rows[0]["tumor_type"], rows[1]["tumor_type"]

    result = client.post("/v1/annotate", json={"fusions": rows}).json()

    assert [annotation["gene"] for annotation in result["annotations"]] == ["TP53"]
    assert client.tumor_types == {"TP53": winner}
    assert f"TP53: using tumor type {winner!r}; ignored tumor types: {loser!r}." in result["warnings"]


@pytest.mark.parametrize("route", ["sync", "job"])
def test_gene_query_accepts_refseq_forms(client, route):
    genes = [f"{_TP53_NM}.6", f"KRAS({_KRAS_NM})", f"EML4({_KRAS_NM})", f"{_ALK_NM}.5:c.3522C>A"]
    if route == "sync":
        response = client.post("/v1/genes/query", json={"genes": genes})
        assert response.status_code == 200, response.text
        body = response.json()
    else:
        created = client.post("/v1/genes/query/jobs", json={"genes": genes})
        assert created.status_code == 200, created.text
        body = _poll(client, created.json()["status_url"])

    by_gene = {item["gene"]: item for item in body["results"]}
    assert by_gene["TP53"]["submitted_inputs"] == [f"{_TP53_NM}.6"]
    assert by_gene["KRAS"]["submitted_inputs"] == [f"KRAS({_KRAS_NM})"]
    assert "variant-level interpretation" in by_gene["ALK"]["warnings"][0]
    assert by_gene[f"EML4({_KRAS_NM})"]["error"] == "The RefSeq accession belongs to a different gene than the one named."


def test_gene_query_get_unknown_accession_is_404_and_outage_is_503(client, fake_http):
    assert client.get(f"/v1/genes/{_UNKNOWN_NM}").status_code == 404
    assert client.get(f"/v1/genes/{_TP53_NM}").json()["results"][0]["gene"] == "TP53"
    fake_http(ncbi_offline=True)
    assert client.get(f"/v1/genes/{_KRAS_NM}").status_code == 503


# --- fusion evidence and fusion context ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("fusion", [f"EML4({_EML4_NM}.5)::ALK({_ALK_NM}.5)", f"{_EML4_NM}::{_ALK_NM}"])
async def test_fusion_evidence_queries_symbols_not_accessions(fake_http, monkeypatch, fusion):
    calls = []

    async def fake_uncached(fusion, tumor_type, max_results):
        calls.append(fusion)
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, interpretation="ok")

    monkeypatch.setattr(literature, "_retrieve_fusion_evidence_uncached", fake_uncached)

    result = await literature.retrieve_fusion_evidence(fusion)

    assert calls == ["EML4::ALK"]
    assert result.fusion == "EML4::ALK" and result.input == fusion


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


def test_fusion_context_passes_refseq_partner_transcripts(client, fusion_context_calls):
    response = client.post("/v1/fusion-context", json={"fusion": f"EML4({_EML4_NM}.5)::ALK({_ALK_NM}.5)"})

    assert response.status_code == 200, response.text
    parsed = fusion_context_calls[0]
    assert (parsed.five_gene, parsed.three_gene) == ("EML4", "ALK")
    assert (parsed.five_transcript, parsed.three_transcript) == (f"{_EML4_NM}.5", f"{_ALK_NM}.5")


def test_fusion_context_rejects_mismatched_accession_and_reports_outage(client, fusion_context_calls, fake_http):
    response = client.post("/v1/fusion-context", json={"fusion": f"EML4({_KRAS_NM})::ALK"})
    assert response.status_code == 400
    assert "belongs to KRAS, not EML4" in response.json()["detail"]

    fake_http(ncbi_offline=True)
    response = client.post("/v1/fusion-context", json={"fusion": f"EML4({_EML4_NM})::ALK"})
    assert response.status_code == 503
    assert fusion_context_calls == []
