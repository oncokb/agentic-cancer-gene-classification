"""Issue #166: gene co-occurrence search for fusion evidence.

PubMed's phrase index does not match "GENE1::GENE2" text, so IRF2BP2::RUNX2
papers that write the fusion only in "::" form were missed by the quoted
exact-pair phrases. No live network: every PubMed/HGNC call is mocked.
"""
from __future__ import annotations

import httpx
import pytest

from src.models.schema import LiteratureRecord, ResolvedGene
from src.pipeline import literature
from src.pipeline.literature import (
    _COOCCURRENCE_RETMAX_MULTIPLIER,
    _fusion_evidence_cache_key,
    _fusion_evidence_queries,
    retrieve_fusion_evidence,
)

_FUSION = "IRF2BP2::RUNX2"
_FUSION_TERMS = "(fusion*[tiab] OR rearrang*[tiab] OR translocation*[tiab] OR chimer*[tiab])"

_PHRASE_PMID = "40026174"
_COLON_ONLY_PMIDS = ["38084005", "42314909"]

_RECORDS = {
    "40026174": LiteratureRecord(
        pmid="40026174",
        title="IRF2BP2-RUNX2 fusion in a mesenchymal tumor",
        abstract="RNA sequencing identified an IRF2BP2-RUNX2 fusion transcript.",
        publication_types=["Journal Article"],
    ),
    "38084005": LiteratureRecord(
        pmid="38084005",
        title="A sarcoma harboring IRF2BP2::RUNX2",
        abstract="Targeted RNA sequencing detected IRF2BP2::RUNX2 in the tumor.",
        publication_types=["Journal Article"],
    ),
    "42314909": LiteratureRecord(
        pmid="42314909",
        title="Recurrent IRF2BP2::RUNX2 gene fusion",
        abstract="We describe tumors with the IRF2BP2::RUNX2 rearrangement.",
        publication_types=["Journal Article"],
    ),
    # Names both genes, but as a signalling axis, not a fusion (like 31186082).
    "31186082": LiteratureRecord(
        pmid="31186082",
        title="The IRF2BP2-KLF2 axis regulates RUNX2 expression in osteoblasts",
        abstract="IRF2BP2 represses KLF2; RUNX2 levels rose after knockdown.",
        publication_types=["Journal Article"],
    ),
}


async def _uncached(_key, compute, ttl_seconds=None, force_refresh=False):
    return await compute()


async def _resolve_no_aliases(symbol, client):
    return ResolvedGene(input_symbol=symbol, canonical_symbol=symbol, resolved=True)


async def _resolve_fails(symbol, client):
    raise httpx.ConnectError("HGNC down")


def _is_cooccurrence(query: str) -> bool:
    return "[tiab]" in query


def _patch(monkeypatch, *, resolve=_resolve_no_aliases, cooccurrence_pmids=None, fail_cooccurrence=False):
    calls = []

    async def fake_esearch(query, max_results, client):
        calls.append((query, max_results))
        if _is_cooccurrence(query):
            if fail_cooccurrence:
                raise httpx.HTTPStatusError(
                    "upstream error",
                    request=httpx.Request("POST", literature.ESEARCH_URL),
                    response=httpx.Response(503),
                )
            return list(cooccurrence_pmids if cooccurrence_pmids is not None else _COLON_ONLY_PMIDS)
        return [_PHRASE_PMID]

    async def fake_efetch(pmids, client):
        return [_RECORDS[pmid] for pmid in pmids if pmid in _RECORDS]

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "resolve_gene", resolve)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)
    monkeypatch.setattr(literature.settings, "min_papers_for_strong_association", 3)
    return calls


# ---------------------------------------------------------------------------
# Query builder
# ---------------------------------------------------------------------------

def test_queries_include_tiab_cooccurrence_without_aliases():
    queries = _fusion_evidence_queries(_FUSION)
    expected = f'("IRF2BP2"[tiab]) AND ("RUNX2"[tiab]) AND {_FUSION_TERMS}'
    assert expected in queries
    # The exact-pair phrase query is still run, ahead of the co-occurrence one.
    assert queries[0].startswith('("IRF2BP2::RUNX2" OR')
    assert queries.index(expected) > 0


def test_queries_include_tiab_cooccurrence_with_aliases_and_tumor_scope():
    queries = _fusion_evidence_queries(
        _FUSION, "sarcoma", ["PEBP2A1", "FLJ12345"], ["CBFA1", "AML3", "cbfa1"],
    )
    cooccurrence = (
        '("IRF2BP2"[tiab] OR "PEBP2A1"[tiab]) AND '
        '("RUNX2"[tiab] OR "CBFA1"[tiab] OR "AML3"[tiab]) AND ' + _FUSION_TERMS
    )
    assert cooccurrence in queries
    assert f'{cooccurrence} AND "sarcoma"' in queries
    assert "FLJ12345" not in " ".join(q for q in queries if "[tiab]" in q)
    assert len(queries) == 4


def test_cooccurrence_caps_aliases_per_partner():
    aliases = [f"ALIAS{i}" for i in range(20)]
    (query,) = [q for q in _fusion_evidence_queries("A1::B1", None, aliases, []) if "[tiab]" in q]
    left = query.split(" AND ")[0]
    assert left.count("[tiab]") == literature._MAX_QUERY_ALIASES_PER_PARTNER + 1


@pytest.mark.parametrize("fusion", ["IRF2BP2::.", ".::RUNX2", "IRF2BP2::--", "IRF2BP2::"])
def test_no_cooccurrence_query_for_punctuation_or_empty_partner(fusion):
    assert literature._fusion_cooccurrence_query(fusion) is None
    assert not any("[tiab]" in q for q in _fusion_evidence_queries(fusion, "sarcoma"))


def test_unnamed_ensembl_partner_builds_cooccurrence_query():
    query = literature._fusion_cooccurrence_query("ENSG00000284922::RUNX2")
    assert query.startswith('("ENSG00000284922"[tiab]) AND ("RUNX2"[tiab])')


def test_cooccurrence_query_skips_unquotable_alias():
    query = literature._fusion_cooccurrence_query("A1::B1", ['BAD"ALIAS', "OK1"], [])
    assert 'BAD"ALIAS' not in query
    assert '"OK1"[tiab]' in query


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------

async def test_cooccurrence_retrieves_colon_only_papers(monkeypatch):
    calls = _patch(monkeypatch)

    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert set(result.pmids) == {_PHRASE_PMID, *_COLON_ONLY_PMIDS}
    assert result.retrieved_count == 3
    assert not result.partial
    assert result.literature_scope == "any_tumor"
    assert not result.interpretation.startswith("No non-retracted")
    retmax = {query: max_results for query, max_results in calls}
    assert all(
        value == (20 * _COOCCURRENCE_RETMAX_MULTIPLIER if _is_cooccurrence(query) else 20)
        for query, value in retmax.items()
    )


async def test_cooccurrence_retrieves_tumor_scoped(monkeypatch):
    calls = _patch(monkeypatch)

    result = await retrieve_fusion_evidence(_FUSION, tumor_type="sarcoma", max_results=20)

    assert set(result.pmids) == {_PHRASE_PMID, *_COLON_ONLY_PMIDS}
    assert result.requested_tumor_pmids == ["38084005"]
    assert result.literature_scope == "requested_tumor"
    assert sum(_is_cooccurrence(query) for query, _ in calls) == 2


async def test_hgnc_alias_failure_still_finds_colon_only_papers(monkeypatch):
    calls = _patch(monkeypatch, resolve=_resolve_fails)

    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert set(_COLON_ONLY_PMIDS) <= set(result.pmids)
    assert not result.partial
    assert (
        f'("IRF2BP2"[tiab]) AND ("RUNX2"[tiab]) AND {_FUSION_TERMS}' in [q for q, _ in calls]
    )


async def test_cooccurring_non_fusion_record_is_filtered(monkeypatch):
    _patch(monkeypatch, cooccurrence_pmids=["31186082", *_COLON_ONLY_PMIDS])

    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert "31186082" not in result.pmids
    assert set(result.pmids) == {_PHRASE_PMID, *_COLON_ONLY_PMIDS}


async def test_pmids_merge_deterministically_across_searches(monkeypatch):
    _patch(monkeypatch, cooccurrence_pmids=[_COLON_ONLY_PMIDS[1], _PHRASE_PMID, _COLON_ONLY_PMIDS[0]])
    fetched = []
    original = literature._efetch

    async def recording_efetch(pmids, client):
        fetched.append(list(pmids))
        return await original(pmids, client)

    literature._efetch = recording_efetch
    try:
        first = await retrieve_fusion_evidence(_FUSION, max_results=20)
        second = await retrieve_fusion_evidence(_FUSION, max_results=20)
    finally:
        literature._efetch = original
    # Phrase hits first, then co-occurrence hits in their own order, deduped.
    assert fetched[0] == [_PHRASE_PMID, _COLON_ONLY_PMIDS[1], _COLON_ONLY_PMIDS[0]]
    assert fetched[0] == fetched[1]
    assert first.pmids == second.pmids


@pytest.mark.parametrize("tumor_type", [None, "sarcoma"])
async def test_cooccurrence_failure_is_partial_and_not_cached(monkeypatch, fake_redis, tumor_type):
    from src.pipeline import cache

    _patch(monkeypatch, fail_cooccurrence=True)
    monkeypatch.setattr(literature, "cached_call", cache.cached_call)

    result = await retrieve_fusion_evidence(_FUSION, tumor_type=tumor_type, max_results=20)

    assert result.partial is True
    assert result.retrieval_status == "ok"
    assert result.pmids == [_PHRASE_PMID]
    assert "results are partial" in result.interpretation
    # A failed tumor-scoped co-occurrence search leaves tumor scope unknown.
    assert result.literature_scope == (None if tumor_type else "any_tumor")
    assert not fake_redis.keys_with_prefix("fusion_evidence:")


async def test_cooccurrence_failure_with_no_phrase_hits_is_not_no_literature(monkeypatch):
    calls = _patch(monkeypatch, fail_cooccurrence=True)

    async def phrase_empty(query, max_results, client):
        if _is_cooccurrence(query):
            raise httpx.ReadTimeout("timeout")
        calls.append((query, max_results))
        return []

    monkeypatch.setattr(literature, "_esearch", phrase_empty)
    result = await retrieve_fusion_evidence(_FUSION, max_results=20)

    assert result.partial is True
    assert result.literature_scope is None
    assert result.retrieval_status == "ok"
    assert result.pmids == []
    assert result.interpretation == (
        "Fusion evidence lookup for IRF2BP2::RUNX2 is incomplete. "
        "One search failed: PubMed search failed (timeout). "
        "The searches that completed retained no matching records, so this does "
        "not show that no literature exists; retry the lookup."
    )
    assert "No non-retracted" not in result.interpretation


async def test_several_failures_with_no_records_is_incomplete_not_no_records(monkeypatch):
    _patch(monkeypatch)

    async def mostly_failing(query, max_results, client):
        if "sarcoma" in query:
            raise httpx.ReadTimeout("timeout")
        return []

    monkeypatch.setattr(literature, "_esearch", mostly_failing)
    result = await retrieve_fusion_evidence(_FUSION, tumor_type="sarcoma", max_results=20)

    assert result.partial is True
    assert result.interpretation.startswith(
        "Fusion evidence lookup for IRF2BP2::RUNX2 is incomplete. 2 of 4 searches failed: "
    )
    assert "No non-retracted" not in result.interpretation


def test_cache_key_includes_cooccurrence_version():
    key = _fusion_evidence_cache_key(_FUSION, None, 20)
    assert '"cooccurrence_search_version": 3' in key


# ---------------------------------------------------------------------------
# Strict pair check for co-occurrence-only records
# ---------------------------------------------------------------------------

def _rec(pmid: str, abstract: str, title: str = "Fusion study") -> LiteratureRecord:
    return LiteratureRecord(
        pmid=pmid, title=title, abstract=abstract, publication_types=["Journal Article"],
    )


_STRICT_NEGATIVES = [
    ("ALK::MYC", "ALK fusion and MYC amplification were detected in lung cancer."),
    ("ALK::RET", "The panel detects fusion events involving ALK, RET, ROS1 and NTRK1 in lung cancer."),
    ("ALK::RET", "ALK rearrangements and RET rearrangements define distinct subsets of lung cancer."),
    ("RET::ROS1", "The panel detects fusion events involving ALK, RET, ROS1 and NTRK1 in lung cancer."),
    ("ALK::ROS1", "Fusions in ALK/ROS1/RET were assayed in lung cancer."),
    ("ALK::RET", "ALK and RET fusions were mutually exclusive in lung cancer."),
    ("ALK::RET", "ALK and RET fusion-positive tumors were compared."),
    ("IRF2BP2::RUNX2", "The IRF2BP2-KLF2 axis regulates RUNX2; no fusion was found."),
    # Round 2: a pair that is one member of a list or alternative.
    ("ALK::RET", "The panel detects fusion involving ALK and RET or ROS1 in lung cancer."),
    ("ALK::RET", "The panel detects fusion involving ALK and RET and ROS1 in lung cancer."),
    ("ALK::RET", "The panel detects fusion involving ALK and RET and/or ROS1 in lung cancer."),
    ("ALK::RET", "The panel detects fusion involving ALK and RET, ROS1 in lung cancer."),
    ("ALK::RET", "The panel detects fusion involving ALK and the RET gene or NTRK1."),
    ("ALK::RET", "ROS1, ALK and RET fusion testing was performed in lung cancer."),
    ("ALK::RET", "ROS1 or ALK and RET fusion testing was performed in lung cancer."),
    ("ALK::RET", "Screening for ROS1/ALK-RET rearrangement in lung cancer."),
    ("ALK::RET", "Screening for ALK/RET/ROS1 rearrangement in lung cancer."),
    ("ALK::RET", "Screening for ALK-RET or ROS1 rearrangement in lung cancer."),
    ("NTRK3::ALK", "ETV6::NTRK3 and ALK fusion were found in separate tumors."),
]

_STRICT_POSITIVES = [
    ("IRF2BP2::RUNX2", "Targeted RNA sequencing detected IRF2BP2::RUNX2 in the tumor."),
    ("IRF2BP2::RUNX2", "Sequencing detected IRF2BP2 :: RUNX2 in the tumor."),
    ("IRF2BP2::RUNX2", "An IRF2BP2-RUNX2 fusion was found."),
    ("IRF2BP2::RUNX2", "An IRF2BP2\u2013RUNX2 fusion was found."),
    ("IRF2BP2::RUNX2", "An IRF2BP2--RUNX2 transcript was found."),
    ("IRF2BP2::RUNX2", "A RUNX2/IRF2BP2 rearrangement was found."),
    ("IRF2BP2::RUNX2", "We describe a fusion of IRF2BP2 and RUNX2 in sarcoma."),
    ("IRF2BP2::RUNX2", "We describe a fusion between the IRF2BP2 gene and the RUNX2 gene."),
    ("IRF2BP2::RUNX2", "An IRF2BP2 and RUNX2 fusion was detected."),
    ("BCR::ABL1", "The t(9;22) BCR/ABL1 translocation was found in leukemia."),
    # Ordinary words after the pair are not list members.
    ("ALK::RET", "We describe a fusion involving ALK and RET in lung cancer."),
    ("ALK::RET", "An ALK-RET fusion and a second event were found."),
    ("ALK::RET", "An ALK-RET fusion, which was in-frame, was found."),
    # "::" notation is explicit even when other fusions are listed beside it.
    ("ALK::RET", "ETV6::NTRK3, ALK::RET and CIC::DUX4 were detected."),
    ("ALK::RET", "Sequencing found ALK::RET or ROS1 fusions."),
]


@pytest.mark.parametrize("fusion, abstract", _STRICT_NEGATIVES)
def test_strict_pair_check_rejects_unrelated_events(fusion, abstract):
    assert not literature.strict_fusion_pair_match(_rec("1", abstract), fusion)


@pytest.mark.parametrize("fusion, abstract", _STRICT_POSITIVES)
def test_strict_pair_check_accepts_explicit_pairs(fusion, abstract):
    assert literature.strict_fusion_pair_match(_rec("1", abstract), fusion)


def test_strict_pair_check_accepts_alias_pair():
    record = _rec("1", "A PEBP2A1-IRF2BP2 chimera was detected.")
    assert not literature.strict_fusion_pair_match(record, _FUSION)
    assert literature.strict_fusion_pair_match(record, _FUSION, [], ["PEBP2A1"])


def test_strict_pair_check_known_alias_is_a_list_member():
    # "Lrir" alone reads as an ordinary word; as a requested alias it is a gene.
    record = _rec("1", "A fusion involving RUNX2 and IRF2BP2 or Lrir was assayed.")
    assert literature.strict_fusion_pair_match(record, _FUSION)
    assert not literature.strict_fusion_pair_match(record, _FUSION, ["LRIR"], [])


def test_strict_pair_check_accepts_real_irf2bp2_runx2_titles():
    for record in (_RECORDS["40026174"], _RECORDS["38084005"], _RECORDS["42314909"]):
        assert literature.strict_fusion_pair_match(record, _FUSION)


@pytest.mark.parametrize("fusion, abstract", _STRICT_NEGATIVES[:3])
async def test_cooccurrence_only_unrelated_event_is_not_evidence(monkeypatch, fusion, abstract):
    record = _rec("42", abstract, title="Molecular profiling of lung cancer")

    async def fake_esearch(query, max_results, client):
        return ["42"] if "[tiab]" in query else []

    async def fake_efetch(pmids, client):
        return [record] if "42" in pmids else []

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "resolve_gene", _resolve_no_aliases)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)

    result = await retrieve_fusion_evidence(fusion, max_results=20)

    assert result.pmids == []
    assert result.partial is False
    assert result.well_supported is False
    assert result.literature_scope == "no_literature"
    assert result.evidence_cards == []


async def test_phrase_hit_keeps_existing_matcher(monkeypatch):
    # Scope is not widened to phrase hits: the same text retrieved by an
    # exact-pair phrase search is judged by _match_fusion_evidence alone.
    record = _rec("42", _STRICT_NEGATIVES[0][1], title="Molecular profiling of lung cancer")

    async def fake_esearch(query, max_results, client):
        return [] if "[tiab]" in query else ["42"]

    async def fake_efetch(pmids, client):
        return [record] if "42" in pmids else []

    monkeypatch.setattr(literature, "cached_call", _uncached)
    monkeypatch.setattr(literature, "resolve_gene", _resolve_no_aliases)
    monkeypatch.setattr(literature, "_esearch", fake_esearch)
    monkeypatch.setattr(literature, "_efetch", fake_efetch)

    result = await retrieve_fusion_evidence("ALK::MYC", max_results=20)
    assert result.pmids == ["42"]
