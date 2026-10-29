"""Ensembl partner resolution under outages, retired IDs and genes Ensembl has no name for.

Real normalization and fusion-evidence code; only the Ensembl/HGNC HTTP
boundary is mocked (and the retry sleep, which is recorded instead).
"""

from __future__ import annotations

import json
from urllib.parse import unquote

import httpx
import pytest

from src.models.schema import FusionEvidenceResult
from src.pipeline import literature, normalization
from src.pipeline.normalization import normalize_fusions

pytestmark = pytest.mark.no_real_redis

_LNCRNA = "ENSG00000225718"  # current human lncRNA, no display_name
_RETIRED = "ENSG00000286466"
_UNKNOWN = "ENSG00000999999"
_PTPRN2 = "ENSG00000155093"

_GENES = {
    _PTPRN2: {
        "id": _PTPRN2,
        "object_type": "Gene",
        "species": "homo_sapiens",
        "display_name": "PTPRN2",
        "biotype": "protein_coding",
        "description": "protein tyrosine phosphatase receptor type N2 [Source:HGNC Symbol;Acc:HGNC:9677]",
    },
    _LNCRNA: {
        "id": _LNCRNA,
        "object_type": "Gene",
        "species": "homo_sapiens",
        "biotype": "lncRNA",
        "description": "novel transcript",
    },
}
_ARCHIVE = {
    _RETIRED: {"id": _RETIRED, "is_current": "", "release": "110", "type": "Gene", "possible_replacement": []},
}


class FakeEnsembl:
    """Ensembl + HGNC over httpx.MockTransport. `failures` are served (in order)
    to Ensembl requests before the normal answers; each is a Response or an
    exception to raise."""

    def __init__(self):
        self.requests: list[str] = []
        self.failures: list = []
        self.batch_status: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.url.host == "rest.genenames.org":
            symbol = path.rsplit("/", 1)[-1].upper()
            docs = [{"symbol": symbol, "hgnc_id": f"HGNC:{symbol}"}] if symbol == "PTPRN2" else []
            return httpx.Response(200, json={"response": {"docs": docs}})
        self.requests.append(f"{request.method} {path}")
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        if request.method == "POST":
            if self.batch_status:
                return httpx.Response(self.batch_status, json={"error": "bad batch"})
            ids = json.loads(request.content)["ids"]
            return httpx.Response(200, json={i: _GENES.get(i) for i in ids})
        stable_id = path.rsplit("/", 1)[-1]
        doc = (_ARCHIVE if path.startswith("/archive/id/") else _GENES).get(stable_id)
        if doc is None:
            return httpx.Response(400, json={"error": f"ID '{stable_id}' not found"})
        return httpx.Response(200, json=doc)


@pytest.fixture
def ensembl(monkeypatch):
    fake = FakeEnsembl()
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        "src.pipeline.normalization.httpx.AsyncClient",
        lambda *args, **kwargs: real_client(transport=httpx.MockTransport(fake)),
    )
    fake.sleeps = []

    async def record_sleep(seconds):
        fake.sleeps.append(seconds)

    monkeypatch.setattr(normalization, "_sleep", record_sleep)
    return fake


def _error(gene_map, key):
    return gene_map[key][0].resolution_error


# --- genes with no HGNC symbol or Ensembl name ---------------------------------


@pytest.mark.asyncio
async def test_nameless_lncrna_resolves_by_its_stable_id(ensembl):
    gene_map = await normalize_fusions([f"{_LNCRNA}.2"])

    resolved = gene_map[_LNCRNA][0]
    assert resolved.resolved and not resolved.unresolvable and resolved.resolution_error is None
    assert resolved.canonical_symbol == _LNCRNA
    assert resolved.symbol_source == "ensembl_id"
    assert resolved.biotype == "lncRNA" and resolved.description == "novel transcript"
    assert resolved.hgnc_id is None
    reading = gene_map.inputs[f"{_LNCRNA}.2"]
    assert reading.errors == []
    assert reading.warnings == [
        f"{_LNCRNA} is a current human lncRNA with no HGNC symbol or Ensembl name; "
        "the Ensembl ID is used as its name."
    ]


@pytest.mark.asyncio
async def test_named_ensembl_gene_is_unchanged(ensembl):
    gene_map = await normalize_fusions([_PTPRN2])

    resolved = gene_map["PTPRN2"][0]
    assert resolved.canonical_symbol == "PTPRN2" and resolved.hgnc_id == "HGNC:9677"
    assert resolved.symbol_source is None and resolved.biotype is None
    assert gene_map.inputs[_PTPRN2].warnings == []


@pytest.mark.asyncio
async def test_fusion_with_nameless_partner_is_annotated(ensembl):
    fusion = f"PTPRN2::{_LNCRNA}"

    gene_map = await normalize_fusions([fusion])

    reading = gene_map.inputs[fusion]
    assert reading.errors == [] and not reading.lookup_failed
    assert reading.canonical == fusion
    assert sorted(gene_map) == [_LNCRNA, "PTPRN2"]
    assert gene_map[_LNCRNA][1] == [fusion]


@pytest.mark.asyncio
async def test_fusion_evidence_runs_for_nameless_partner(ensembl, monkeypatch):
    searched = []

    async def fake_uncached(fusion, tumor_type, max_results):
        searched.append(fusion)
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, interpretation="ok")

    monkeypatch.setattr(literature, "_retrieve_fusion_evidence_uncached", fake_uncached)

    result = await literature.retrieve_fusion_evidence(f"PTPRN2::{_LNCRNA}")

    assert searched == [f"PTPRN2::{_LNCRNA}"]
    assert result.retrieval_status != "failed"
    assert not result.interpretation.startswith("Fusion evidence was not retrieved")


# --- retired and unknown IDs ---------------------------------------------------


@pytest.mark.asyncio
async def test_retired_id_names_its_last_release(ensembl):
    gene_map = await normalize_fusions([f"PTPRN2::{_RETIRED}"])

    resolved = gene_map[_RETIRED][0]
    assert resolved.unresolvable and not resolved.lookup_failed
    assert _error(gene_map, _RETIRED) == (
        f"3′ partner of 'PTPRN2::{_RETIRED}' rejected — Ensembl ID {_RETIRED} is retired (last seen in release 110)."
    )
    assert "retry" not in _error(gene_map, _RETIRED)


@pytest.mark.asyncio
async def test_retired_id_mentions_replacement_only_when_ensembl_gives_one(ensembl, monkeypatch):
    archive = {**_ARCHIVE[_RETIRED], "possible_replacement": [{"stable_id": _PTPRN2, "score": 0.9}]}
    monkeypatch.setitem(_ARCHIVE, _RETIRED, archive)

    gene_map = await normalize_fusions([_RETIRED])

    assert _error(gene_map, _RETIRED) == (
        f"Ensembl ID {_RETIRED} is retired (last seen in release 110). "
        f"Ensembl lists {_PTPRN2} as a possible replacement."
    )


@pytest.mark.asyncio
async def test_unknown_id_is_a_permanent_not_found(ensembl):
    gene_map = await normalize_fusions([_UNKNOWN])

    resolved = gene_map[_UNKNOWN][0]
    assert resolved.unresolvable and not resolved.lookup_failed
    assert resolved.resolution_error == f"Ensembl did not find ID {_UNKNOWN}."
    assert ensembl.requests == ["POST /lookup/id", f"GET /archive/id/{_UNKNOWN}"]


@pytest.mark.asyncio
async def test_batch_400_falls_back_to_per_id_lookups(ensembl):
    ensembl.batch_status = 400

    gene_map = await normalize_fusions([f"{_PTPRN2}::{_LNCRNA}", _UNKNOWN])

    assert gene_map["PTPRN2"][0].resolved and gene_map[_LNCRNA][0].resolved
    assert _error(gene_map, _UNKNOWN) == f"Ensembl did not find ID {_UNKNOWN}."
    assert not gene_map[_UNKNOWN][0].lookup_failed
    assert ensembl.requests.count("POST /lookup/id") == 1
    assert {f"GET /lookup/id/{i}" for i in (_PTPRN2, _LNCRNA, _UNKNOWN)} <= set(ensembl.requests)


# --- transient failures --------------------------------------------------------


def _timeout():
    return httpx.ReadTimeout("slow", request=httpx.Request("POST", normalization.ENSEMBL_LOOKUP_URL))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure, reason",
    [
        (_timeout, "timeout"),
        (lambda: httpx.Response(429), "HTTP 429"),
        (lambda: httpx.Response(503), "HTTP 503"),
        (lambda: httpx.Response(200, text="<html>Ensembl is down for maintenance</html>"), "invalid response"),
    ],
)
async def test_transient_failures_are_retried_then_reported(ensembl, failure, reason):
    ensembl.failures = [failure() for _ in range(normalization.ENSEMBL_RETRIES + 1)]
    fusion = f"PTPRN2::{_LNCRNA}"

    gene_map = await normalize_fusions([fusion])

    assert ensembl.requests == ["POST /lookup/id"] * (normalization.ENSEMBL_RETRIES + 1)
    assert len(ensembl.sleeps) == normalization.ENSEMBL_RETRIES
    resolved = gene_map[_LNCRNA][0]
    assert resolved.unresolvable and resolved.lookup_failed
    assert gene_map.inputs[fusion].lookup_failed
    assert resolved.resolution_error == (
        f"3′ partner of '{fusion}' rejected — Ensembl lookup temporarily unavailable ({reason}); retry later."
    )


@pytest.mark.asyncio
async def test_transient_failure_then_success_resolves(ensembl):
    ensembl.failures = [httpx.Response(503), httpx.Response(200, text="<html>oops</html>")]

    gene_map = await normalize_fusions([_LNCRNA])

    assert gene_map[_LNCRNA][0].resolved
    assert ensembl.sleeps == [normalization.ENSEMBL_RETRY_BACKOFF_SECONDS, normalization.ENSEMBL_RETRY_BACKOFF_SECONDS * 2]


@pytest.mark.asyncio
async def test_retry_after_is_honoured(ensembl):
    ensembl.failures = [httpx.Response(429, headers={"Retry-After": "2"})]

    gene_map = await normalize_fusions([_PTPRN2])

    assert gene_map["PTPRN2"][0].resolved
    assert ensembl.sleeps == [2.0]


@pytest.mark.asyncio
async def test_retry_after_beyond_the_deadline_fails_fast(ensembl):
    ensembl.failures = [httpx.Response(429, headers={"Retry-After": "120"})]

    gene_map = await normalize_fusions([_PTPRN2])

    assert ensembl.requests == ["POST /lookup/id"] and ensembl.sleeps == []
    assert gene_map[_PTPRN2][0].resolution_error == "Ensembl lookup temporarily unavailable (HTTP 429); retry later."


@pytest.mark.asyncio
async def test_html_200_on_transcript_lookup_does_not_escape(ensembl):
    transcript = "ENST00000000001"
    ensembl.failures = [httpx.Response(200, text="<html>down</html>")] * (normalization.ENSEMBL_RETRIES + 1)

    gene_map = await normalize_fusions([transcript])

    assert gene_map[transcript][0].lookup_failed
    assert "(invalid response)" in _error(gene_map, transcript)


# --- caching -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failures_are_not_cached(ensembl):
    ensembl.failures = [httpx.Response(503)] * (normalization.ENSEMBL_RETRIES + 1)
    failed = await normalize_fusions([_LNCRNA])
    assert failed[_LNCRNA][0].lookup_failed

    recovered = await normalize_fusions([_LNCRNA])

    assert recovered[_LNCRNA][0].resolved


@pytest.mark.asyncio
async def test_resolutions_and_not_found_are_cached_per_id(ensembl):
    await normalize_fusions([f"PTPRN2::{_LNCRNA}", _RETIRED, _UNKNOWN])
    first = len(ensembl.requests)

    gene_map = await normalize_fusions([_UNKNOWN, _LNCRNA, _RETIRED])
    # A batch mixing cached and new IDs only asks Ensembl about the new one.
    await normalize_fusions([_LNCRNA, _PTPRN2])

    assert len(ensembl.requests) == first + 1
    assert ensembl.requests[-1] == "POST /lookup/id"
    assert gene_map[_LNCRNA][0].resolved
    assert "retired" in _error(gene_map, _RETIRED)
    assert _error(gene_map, _UNKNOWN) == f"Ensembl did not find ID {_UNKNOWN}."
