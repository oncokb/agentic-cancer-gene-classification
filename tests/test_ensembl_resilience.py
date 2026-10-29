"""Ensembl partner resolution under outages, retired IDs and genes Ensembl has no name for.

Real normalization and fusion-evidence code; only the Ensembl/HGNC HTTP
boundary is mocked (and the retry sleep, which is recorded instead).
"""

from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import unquote

import httpx
import pytest

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
    """Ensembl, HGNC and PubMed over httpx.MockTransport.

    `failures` are served (in order) to Ensembl requests before the normal
    answers; each is a Response or an exception to raise. `routes` overrides
    the answer for one "METHOD /path" with a Response factory. `get_delay`
    makes every Ensembl GET take that long; `max_in_flight` records the most
    concurrent Ensembl requests seen."""

    def __init__(self):
        self.requests: list[str] = []
        self.failures: list = []
        self.batch_status: int | None = None
        self.routes: dict = {}
        self.get_delay = 0.0
        self.in_flight = self.max_in_flight = 0
        self.pubmed_terms: list[str] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        if request.url.host == "rest.genenames.org":
            symbol = path.rsplit("/", 1)[-1].upper()
            docs = (
                [{"symbol": symbol, "hgnc_id": f"HGNC:{symbol}", "alias_symbol": ["IA-2beta"]}]
                if symbol == "PTPRN2"
                else []
            )
            return httpx.Response(200, json={"response": {"docs": docs}})
        if request.url.host == "eutils.ncbi.nlm.nih.gov":
            return self._pubmed(request)
        key = f"{request.method} {path}"
        self.requests.append(key)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            if self.get_delay and request.method == "GET":
                await asyncio.sleep(self.get_delay)
            return self._ensembl(request, key, path)
        finally:
            self.in_flight -= 1

    def _ensembl(self, request: httpx.Request, key: str, path: str) -> httpx.Response:
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return failure
        if key in self.routes:
            return self.routes[key]()
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

    def _pubmed(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("esearch.fcgi"):
            term = request.url.params["term"]
            self.pubmed_terms.append(term)
            ids = ["111", "222"] if _LNCRNA in term else []
            return httpx.Response(200, json={"esearchresult": {"idlist": ids}})
        articles = "".join(
            f"<PubmedArticle><MedlineCitation><PMID>{pmid}</PMID><Article>"
            f"<ArticleTitle>{title}</ArticleTitle><Abstract><AbstractText>{title}.</AbstractText></Abstract>"
            "</Article></MedlineCitation></PubmedArticle>"
            for pmid, title in _PUBMED.items()
            if pmid in request.url.params["id"].split(",")
        )
        return httpx.Response(200, text=f"<PubmedArticleSet>{articles}</PubmedArticleSet>")


_PUBMED = {
    "111": f"A recurrent PTPRN2::{_LNCRNA} fusion in glioma",
    "222": "PTPRN2 expression in neuroendocrine tumors",  # never names the pair
}


@pytest.fixture
def ensembl(monkeypatch):
    fake = FakeEnsembl()
    real_client = httpx.AsyncClient
    # httpx is one module: this also routes literature's NCBI client to the fake.
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
async def test_fusion_evidence_runs_for_nameless_partner(ensembl):
    # Real alias expansion, PubMed query building, EFetch parsing and pair
    # matching; only HTTP is mocked.
    fusion = f"PTPRN2::{_LNCRNA}"

    result = await literature.retrieve_fusion_evidence(fusion)

    assert result.retrieval_status != "failed"
    assert result.fusion == fusion
    assert ensembl.pubmed_terms and all(_LNCRNA in term for term in ensembl.pubmed_terms)
    # PTPRN2's HGNC alias is searched too; the ID partner has none.
    assert any("IA-2beta" in term for term in ensembl.pubmed_terms)
    # Only the record naming the pair survives matching.
    assert result.pmids == ["111"]
    assert result.retrieved_count == 1


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
    ensembl.failures = [failure() for _ in range(normalization.ENSEMBL_ATTEMPTS)]
    fusion = f"PTPRN2::{_LNCRNA}"

    gene_map = await normalize_fusions([fusion])

    assert ensembl.requests == ["POST /lookup/id"] * (normalization.ENSEMBL_ATTEMPTS)
    assert len(ensembl.sleeps) == normalization.ENSEMBL_ATTEMPTS - 1
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
    ensembl.failures = [httpx.Response(200, text="<html>down</html>")] * (normalization.ENSEMBL_ATTEMPTS)

    gene_map = await normalize_fusions([transcript])

    assert gene_map[transcript][0].lookup_failed
    assert "(invalid response)" in _error(gene_map, transcript)


# --- caching -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_failures_are_not_cached(ensembl):
    ensembl.failures = [httpx.Response(503)] * (normalization.ENSEMBL_ATTEMPTS)
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


# --- lookup misses are only cached once the archive confirms them --------------

_ATG5_PARTNER = "ENSG00000286084"


@pytest.mark.asyncio
async def test_lookup_null_but_archive_current_is_retryable_and_not_cached(ensembl):
    archive_key = f"GET /archive/id/{_ATG5_PARTNER}"
    ensembl.routes[archive_key] = lambda: httpx.Response(
        200, json={"id": _ATG5_PARTNER, "is_current": "1", "release": "112", "possible_replacement": []}
    )
    fusion = f"ATG5::{_ATG5_PARTNER}"

    first = await normalize_fusions([fusion])

    resolved = first[_ATG5_PARTNER][0]
    assert resolved.unresolvable and resolved.lookup_failed
    assert first.inputs[fusion].lookup_failed
    assert resolved.resolution_error == (
        f"3′ partner of '{fusion}' rejected — "
        "Ensembl lookup temporarily unavailable (inconsistent response); retry later."
    )
    requests = len(ensembl.requests)

    # The contradiction was retried like any transient failure.
    assert ensembl.requests == ["POST /lookup/id"] + [archive_key] * normalization.ENSEMBL_ATTEMPTS

    await normalize_fusions([fusion])

    # Nothing was cached.
    assert ensembl.requests[requests:] == ["POST /lookup/id"] + [archive_key] * normalization.ENSEMBL_ATTEMPTS


@pytest.mark.asyncio
async def test_archive_current_once_then_retired_is_retried_to_the_retired_answer(ensembl):
    archive_key = f"GET /archive/id/{_RETIRED}"
    answers = [
        httpx.Response(200, json={**_ARCHIVE[_RETIRED], "is_current": "1"}),
        httpx.Response(200, json=_ARCHIVE[_RETIRED]),
    ]
    ensembl.routes[archive_key] = lambda: answers.pop(0)

    gene_map = await normalize_fusions([_RETIRED])

    assert ensembl.requests.count(archive_key) == 2
    assert not gene_map[_RETIRED][0].lookup_failed
    assert _error(gene_map, _RETIRED) == f"Ensembl ID {_RETIRED} is retired (last seen in release 110)."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "archive",
    [
        {"is_current": None},
        {"is_current": "unavailable"},
        {"is_current": 2},
        {"release": "110"},
        {"is_current": "", "possible_replacement": 7},
        {"is_current": "", "possible_replacement": [{"stable_id": 5}]},
        {"is_current": "", "possible_replacement": [{"score": 0.9}]},
        {"is_current": "", "possible_replacement": ["ENSG00000155093"]},
        {"is_current": "", "release": {"n": 110}},
        {"is_current": "", "release": True},
        [],
    ],
)
async def test_malformed_archive_answer_is_retried_never_cached_never_raised(ensembl, archive):
    archive_key = f"GET /archive/id/{_RETIRED}"
    ensembl.routes[archive_key] = lambda: httpx.Response(200, json=archive)

    first = await normalize_fusions([f"PTPRN2::{_RETIRED}"])

    assert first[_RETIRED][0].lookup_failed
    assert _error(first, _RETIRED).endswith(
        "Ensembl lookup temporarily unavailable (invalid response); retry later."
    )
    assert ensembl.requests.count(archive_key) == normalization.ENSEMBL_ATTEMPTS

    del ensembl.routes[archive_key]
    second = await normalize_fusions([_RETIRED])

    assert ensembl.requests.count(archive_key) == normalization.ENSEMBL_ATTEMPTS + 1  # asked again
    assert "is retired (last seen in release 110)" in _error(second, _RETIRED)


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["", "0", 0, False, "false"])
async def test_archive_not_current_flags_mean_retired(ensembl, monkeypatch, flag):
    monkeypatch.setitem(_ARCHIVE, _RETIRED, {**_ARCHIVE[_RETIRED], "is_current": flag})

    gene_map = await normalize_fusions([_RETIRED])

    assert _error(gene_map, _RETIRED) == f"Ensembl ID {_RETIRED} is retired (last seen in release 110)."


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["1", 1, True, "true"])
async def test_archive_current_flags_are_inconsistent(ensembl, monkeypatch, flag):
    monkeypatch.setitem(_ARCHIVE, _RETIRED, {**_ARCHIVE[_RETIRED], "is_current": flag})

    gene_map = await normalize_fusions([_RETIRED])

    assert gene_map[_RETIRED][0].lookup_failed
    assert "(inconsistent response)" in _error(gene_map, _RETIRED)


@pytest.mark.asyncio
async def test_archive_outage_is_retryable_and_not_cached(ensembl):
    archive_key = f"GET /archive/id/{_RETIRED}"
    ensembl.routes[archive_key] = lambda: httpx.Response(503)

    failed = await normalize_fusions([_RETIRED])

    assert failed[_RETIRED][0].lookup_failed
    assert _error(failed, _RETIRED) == "Ensembl lookup temporarily unavailable (HTTP 503); retry later."
    assert ensembl.requests.count(archive_key) == normalization.ENSEMBL_ATTEMPTS

    del ensembl.routes[archive_key]
    recovered = await normalize_fusions([_RETIRED])

    assert not recovered[_RETIRED][0].lookup_failed
    assert "is retired (last seen in release 110)" in _error(recovered, _RETIRED)


@pytest.mark.asyncio
async def test_parent_missing_from_lookup_is_retryable(ensembl, monkeypatch):
    transcript = "ENST00000000002"
    monkeypatch.setitem(
        _GENES,
        transcript,
        {"id": transcript, "object_type": "Transcript", "species": "homo_sapiens", "Parent": _UNKNOWN},
    )

    gene_map = await normalize_fusions([transcript])

    assert gene_map[transcript][0].lookup_failed
    assert "(inconsistent response)" in _error(gene_map, transcript)


# --- response shapes ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [[], {}])
async def test_single_id_lookup_with_empty_json_is_not_absence(ensembl, body):
    transcript = "ENST00000000001"
    ensembl.routes[f"GET /lookup/id/{transcript}"] = lambda: httpx.Response(200, json=body)

    failed = await normalize_fusions([transcript])

    assert failed[transcript][0].lookup_failed
    assert _error(failed, transcript) == "Ensembl lookup temporarily unavailable (invalid response); retry later."
    del ensembl.routes[f"GET /lookup/id/{transcript}"]
    requests = len(ensembl.requests)

    again = await normalize_fusions([transcript])

    assert ensembl.requests[requests] == f"GET /lookup/id/{transcript}"  # not cached as absent
    assert _error(again, transcript) == f"Ensembl did not find ID {transcript}."


@pytest.mark.asyncio
async def test_empty_batch_answer_is_retried(ensembl):
    ensembl.failures = [httpx.Response(200, json={})]

    gene_map = await normalize_fusions([f"ATG5::{_LNCRNA}"])

    assert ensembl.requests == ["POST /lookup/id", "POST /lookup/id"]
    assert gene_map[_LNCRNA][0].resolved


@pytest.mark.asyncio
async def test_single_id_batch_that_stays_incomplete_is_retryable(ensembl):
    ensembl.routes["POST /lookup/id"] = lambda: httpx.Response(200, json={_PTPRN2: {"bogus": True}})

    gene_map = await normalize_fusions([_PTPRN2])

    assert ensembl.requests == ["POST /lookup/id"] * normalization.ENSEMBL_ATTEMPTS
    assert gene_map[_PTPRN2][0].lookup_failed
    assert _error(gene_map, _PTPRN2) == "Ensembl lookup temporarily unavailable (incomplete response); retry later."


@pytest.mark.asyncio
async def test_batch_that_stays_incomplete_falls_back_per_id(ensembl):
    ensembl.routes["POST /lookup/id"] = lambda: httpx.Response(200, json={_UNKNOWN: None, _PTPRN2: _GENES[_PTPRN2]})

    gene_map = await normalize_fusions([_UNKNOWN, _PTPRN2, _LNCRNA])

    # The entry missing for the lncRNA is retried, then each ID is looked up alone.
    assert ensembl.requests.count("POST /lookup/id") == normalization.ENSEMBL_ATTEMPTS
    assert f"GET /lookup/id/{_LNCRNA}" in ensembl.requests
    assert gene_map["PTPRN2"][0].resolved and gene_map[_LNCRNA][0].resolved
    assert _error(gene_map, _UNKNOWN) == f"Ensembl did not find ID {_UNKNOWN}."  # explicit null: archive checked


@pytest.mark.asyncio
async def test_successful_lookup_replaces_a_cached_null(ensembl):
    from src.pipeline import cache as cache_module

    # An earlier version cached lookup misses as null.
    await cache_module._get_client().set(f"ensembl:lookup:{_LNCRNA}", "null")

    assert (await normalize_fusions([_LNCRNA]))[_LNCRNA][0].resolved
    requests = len(ensembl.requests)

    assert (await normalize_fusions([_LNCRNA]))[_LNCRNA][0].resolved
    assert len(ensembl.requests) == requests  # served from the refreshed cache


# --- one deadline and one concurrency bound per resolution ---------------------


@pytest.mark.asyncio
async def test_deadline_bounds_a_slowly_streamed_response(ensembl, monkeypatch):
    monkeypatch.setattr(normalization, "ENSEMBL_RETRY_DEADLINE_SECONDS", 0.05)

    async def trickle():
        for _ in range(8):
            await asyncio.sleep(0.03)
            yield b" "

    ensembl.routes["POST /lookup/id"] = lambda: httpx.Response(200, content=trickle())

    started = time.monotonic()
    gene_map = await normalize_fusions([_PTPRN2])
    elapsed = time.monotonic() - started

    assert elapsed < 0.15
    assert _error(gene_map, _PTPRN2) == "Ensembl lookup temporarily unavailable (timeout); retry later."


def _ids(count: int, start: int = 900000) -> list[str]:
    return [f"ENSG00000{start + i}" for i in range(count)]


@pytest.mark.asyncio
async def test_per_id_fallback_shares_the_resolution_deadline(ensembl, monkeypatch):
    monkeypatch.setattr(normalization, "ENSEMBL_RETRY_DEADLINE_SECONDS", 0.2)
    ensembl.batch_status = 400
    ensembl.get_delay = 1.0
    ids = _ids(20)

    started = time.monotonic()
    gene_map = await normalize_fusions(ids)
    elapsed = time.monotonic() - started

    assert elapsed < 0.6
    assert ensembl.max_in_flight <= normalization.NORMALIZATION_CONCURRENCY
    for stable_id in ids:
        assert gene_map[stable_id][0].lookup_failed
        assert _error(gene_map, stable_id) == "Ensembl lookup temporarily unavailable (timeout); retry later."


@pytest.mark.asyncio
async def test_archive_checks_are_concurrency_bounded(ensembl):
    ensembl.get_delay = 0.01
    ids = _ids(20)

    gene_map = await normalize_fusions(ids)

    assert sum(request.startswith("GET /archive/id/") for request in ensembl.requests) == 20
    assert ensembl.max_in_flight <= normalization.NORMALIZATION_CONCURRENCY
    assert all(_error(gene_map, stable_id) == f"Ensembl did not find ID {stable_id}." for stable_id in ids)
