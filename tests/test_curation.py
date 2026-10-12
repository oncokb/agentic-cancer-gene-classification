"""Curator overrides: API (PUT/GET/DELETE/history), and application wherever
results are served (cache reuse, fresh runs, shared run links, fusion jobs)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from src import main
from src.api import curation_routes
from src.models.schema import (
    AnnotationResult,
    ClinicalActionability,
    CuratedOverride,
    CurationHistoryEntry,
    EvidenceCard,
    FusionEvidenceCard,
    FusionEvidenceResult,
    FusionInput,
    GeneAnnotation,
    LiteratureRecord,
    QualityFlag,
    ResolvedGene,
)
from src.pipeline import curation, orchestrator, result_sanitizer
from src.pipeline.curation import (
    CURATED_RETRACTED_FLAG,
    CURATION_UNAVAILABLE_FLAG,
    NEWER_EVIDENCE_FLAG,
    apply_current_gene_overrides,
    apply_fusion_override,
    apply_override,
    normalize_subject_key,
    result_payload_for_storage,
)
from src.pipeline.normalization import split_fusion
from src.pipeline.curation_store import CurationVersionConflict
from src.pipeline.orchestrator import run_pipeline

NOW = "2026-10-12T12:00:00+00:00"


class FakeCurationStore:
    """In-memory RunStore stand-in: runs, gene_annotations, and curation tables
    with the same optimistic-concurrency rules as curation_store.py."""

    def __init__(self) -> None:
        self.overrides: Dict[Tuple[str, str, str], CuratedOverride] = {}
        self.history: List[CurationHistoryEntry] = []
        self.gene_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.saved_gene_annotations: List[GeneAnnotation] = []
        self.checked: List[Any] = []
        self.runs: Dict[str, Dict[str, Any]] = {}
        self.requests: Dict[str, Dict[str, Any]] = {}
        self.fail_curation = False  # simulate the curation tables being unreachable
        self.lookup_calls = 0

    def _maybe_fail(self) -> None:
        if self.fail_curation:
            raise RuntimeError("curation tables unreachable")

    # -- curation -----------------------------------------------------------
    async def get_curated_overrides(self, subject_type, keys):
        self._maybe_fail()
        self.lookup_calls += 1
        found = {}
        for key, tumor_type in keys:
            override = self.overrides.get((subject_type, key, tumor_type))
            if override is not None:
                found[(subject_type, key, tumor_type)] = override
        return found

    async def get_curated_override(self, subject_type, subject_key, tumor_type):
        self._maybe_fail()
        return self.overrides.get((subject_type, subject_key, tumor_type))

    def _max_history_version(self, subject) -> int:
        return max(
            (e.version for e in self.history if (e.subject_type, e.subject_key, e.tumor_type) == subject),
            default=0,
        )

    async def save_curated_override(self, *, subject_type, subject_key, tumor_type, payload, curator_email,
                                    expected_version, pipeline_pmids_snapshot, note=None):
        self._maybe_fail()
        subject = (subject_type, subject_key, tumor_type)
        current = self.overrides.get(subject)
        current_version = current.version if current else 0
        if current_version != expected_version:
            raise CurationVersionConflict(current_version)
        version = max(current_version, self._max_history_version(subject)) + 1
        override = CuratedOverride(
            subject_type=subject_type, subject_key=subject_key, tumor_type=tumor_type, payload=payload,
            curator_email=curator_email, created_at=current.created_at if current else NOW, updated_at=NOW,
            version=version, pipeline_pmids_snapshot=pipeline_pmids_snapshot, note=note,
        )
        self.overrides[subject] = override
        self.history.append(CurationHistoryEntry(
            subject_type=subject_type, subject_key=subject_key, tumor_type=tumor_type, version=version,
            action="save", curator_email=curator_email, created_at=NOW, payload=payload, note=note,
        ))
        return override

    async def delete_curated_override(self, *, subject_type, subject_key, tumor_type, curator_email,
                                      expected_version=None, note=None):
        self._maybe_fail()
        subject = (subject_type, subject_key, tumor_type)
        current = self.overrides.get(subject)
        if current is None:
            if expected_version is not None:
                raise CurationVersionConflict(0)
            return None
        if expected_version is not None and expected_version != current.version:
            raise CurationVersionConflict(current.version)
        del self.overrides[subject]
        self.history.append(CurationHistoryEntry(
            subject_type=subject_type, subject_key=subject_key, tumor_type=tumor_type,
            version=current.version + 1, action="revert", curator_email=curator_email, created_at=NOW, note=note,
        ))
        return current.version

    async def list_curation_history(self, subject_type, subject_key, tumor_type):
        self._maybe_fail()
        entries = [e for e in self.history if (e.subject_type, e.subject_key, e.tumor_type)
                   == (subject_type, subject_key, tumor_type)]
        return sorted(entries, key=lambda e: e.version, reverse=True)

    # -- runs ---------------------------------------------------------------
    async def save_run(self, run_id, timestamp, request_payload, result_payload):
        self.runs[run_id] = result_payload
        self.requests[run_id] = request_payload

    async def get_run(self, run_id):
        return self.runs.get(run_id)

    async def get_run_request(self, run_id):
        return self.requests.get(run_id)

    async def update_run_result(self, run_id, result_payload):
        self.runs[run_id] = result_payload

    # -- gene_annotations ---------------------------------------------------
    async def get_gene_annotation(self, gene, tumor_type=None):
        return self.gene_cache.get((gene.upper(), curation.normalize_tumor_type(tumor_type)))

    async def save_gene_annotation(self, annotation, updated_at, tumor_type=None):
        self.saved_gene_annotations.append(annotation.model_copy(deep=True))

    async def mark_gene_pubmed_checked(self, gene, checked_at, annotation=None, tumor_type=None):
        self.checked.append(annotation)


def _record(pmid: str, *, abstract: str = "Abstract text.", retracted: bool = False) -> LiteratureRecord:
    return LiteratureRecord(
        pmid=pmid,
        title=f"Title {pmid}",
        abstract=abstract,
        journal="J Onc",
        publication_types=["Retracted Publication"] if retracted else ["Journal Article"],
    )


PUBMED = {
    "111": _record("111"),
    "222": _record("222"),
    "333": _record("333"),
    "444": _record("444"),
    "666": _record("666", retracted=True),
    "777": _record("777", abstract="", retracted=False),
}


async def _fake_efetch(pmids, client, *, filter_retracted=True):
    # Mirrors _efetch: records without an abstract are dropped unless retracted.
    records = [PUBMED[p] for p in pmids if p in PUBMED]
    return [r for r in records if r.abstract or "Retracted Publication" in r.publication_types]


async def _no_retractions(pmids):
    return set()


ALIASES = {"MOZ": "KAT6A"}


async def _fake_normalize_fusions(inputs, **_kwargs):
    """HGNC normalization stand-in: split fusions, map known aliases."""
    gene_map: Dict[str, Any] = {}
    for value in inputs:
        five, three = split_fusion(value)
        for symbol in (five, three):
            if symbol:
                canonical = ALIASES.get(symbol.upper(), symbol.upper())
                entry = gene_map.setdefault(canonical, (ResolvedGene(input_symbol=symbol, resolved=True), []))
                entry[1].append(value)
    return gene_map


@pytest.fixture
def store(monkeypatch):
    fake = FakeCurationStore()
    previous = getattr(main.app.state, "run_store", None)
    main.app.state.run_store = fake
    monkeypatch.setattr(curation_routes.literature, "_efetch", _fake_efetch)
    monkeypatch.setattr(curation, "find_retracted_pmids", _no_retractions)
    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", _no_retractions)
    monkeypatch.setattr(curation, "normalize_fusions", _fake_normalize_fusions)
    yield fake
    main.app.state.run_store = previous


@pytest.fixture
def client(store):
    return TestClient(main.app)


def _pipeline_annotation(**overrides) -> GeneAnnotation:
    values = dict(
        gene="ALK",
        cancer_associated=True,
        cancer_association_rationale="Pipeline rationale.",
        citations=["111"],
        retrieved_pmids=["111", "222"],
        evidence_cards=[EvidenceCard(pmid="111", title="Pipeline card", selected_reason="pipeline")],
        evidence_support_score=0.8,
        clinical_actionability=ClinicalActionability(
            confidence_score=0.9, summary="Keyword precedent.", confidence_explanation="keywords", pmids=["111"]
        ),
    )
    values.update(overrides)
    return GeneAnnotation(**values)


def _override(payload: Dict[str, Any], *, subject_type="gene", key="ALK", tumor_type="luad",
              version=1, snapshot: Optional[List[str]] = None) -> CuratedOverride:
    return CuratedOverride(
        subject_type=subject_type, subject_key=key, tumor_type=tumor_type, payload=payload,
        curator_email="curator@mskcc.org", created_at=NOW, updated_at=NOW, version=version,
        pipeline_pmids_snapshot=snapshot,
    )


GENE_BODY = {
    "payload": {
        "cancer_association_rationale": "Curated rationale.",
        "citations": ["222", "333"],
        "evidence_cards": [
            {"pmid": "222", "title": "client title is ignored", "selected_reason": "Key trial", "quote": "q",
             "evidence_type": "clinical"},
        ],
        "therapeutic_precedent": {
            "summary": "Crizotinib response.",
            "precedents": [{"pmid": "333", "quote": "Responded.", "therapies": ["crizotinib"]}],
        },
    },
    "note": "first pass",
}


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def test_subject_keys_normalize():
    assert normalize_subject_key("gene", " alk ") == "ALK"
    assert normalize_subject_key("fusion", "eml4--alk") == "EML4::ALK"
    assert normalize_subject_key("fusion", "EML4/ALK") == "EML4::ALK"
    with pytest.raises(ValueError):
        normalize_subject_key("fusion", "ALK")
    with pytest.raises(ValueError):
        normalize_subject_key("gene", "EML4::ALK")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


def test_put_get_history_delete_roundtrip(client, store, monkeypatch):
    actions = []
    monkeypatch.setattr(curation_routes, "record_user_action", lambda user, action, details: actions.append(
        (user, action, details)))
    store.gene_cache[("ALK", "luad")] = {"annotation": _pipeline_annotation().model_dump()}

    assert client.get("/v1/curation/gene/alk?tumor_type=LUAD").json() == {
        "subject_type": "gene", "subject_key": "ALK", "tumor_type": "luad", "version": 0, "override": None,
    }

    saved = client.put("/v1/curation/gene/alk?tumor_type=LUAD", json=GENE_BODY)
    assert saved.status_code == 200, saved.text
    body = saved.json()
    assert body["version"] == 1
    override = body["override"]
    assert override["curator_email"] == "local@internal"
    assert override["pipeline_pmids_snapshot"] == ["111"]
    card = override["payload"]["evidence_cards"][0]
    # PubMed-derived fields are filled server-side; curator fields are kept.
    assert card["title"] == "Title 222" and card["journal"] == "J Onc" and card["abstract"] == "Abstract text."
    assert card["selected_reason"] == "Key trial" and card["evidence_type"] == "clinical"
    assert override["payload"]["therapeutic_precedent"]["precedents"][0]["title"] == "Title 333"
    assert "supporting_quotes" not in override["payload"]  # untouched sections stay unset
    assert actions[-1][1] == "curation_save" and actions[-1][2]["version"] == 1

    got = client.get("/v1/curation/gene/ALK?tumor_type=luad").json()
    assert got["version"] == 1 and got["override"]["payload"]["citations"] == ["222", "333"]

    # Stale expected_version -> 409 with the current version.
    stale = client.put("/v1/curation/gene/ALK?tumor_type=luad", json={**GENE_BODY, "expected_version": 0})
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_version"] == 1

    second = client.put("/v1/curation/gene/ALK?tumor_type=luad",
                        json={"expected_version": 1, "payload": {"citations": ["222"]}})
    assert second.status_code == 200 and second.json()["version"] == 2

    assert client.delete("/v1/curation/gene/ALK?tumor_type=luad&expected_version=1").status_code == 409
    reverted = client.delete("/v1/curation/gene/ALK?tumor_type=luad&note=wrong")
    assert reverted.status_code == 200 and reverted.json()["reverted_version"] == 2
    assert actions[-1][1] == "curation_revert"
    assert client.get("/v1/curation/gene/ALK?tumor_type=luad").json()["version"] == 0
    assert client.delete("/v1/curation/gene/ALK?tumor_type=luad").status_code == 404

    history = client.get("/v1/curation/gene/ALK/history?tumor_type=luad").json()["entries"]
    assert [(e["version"], e["action"]) for e in history] == [(3, "revert"), (2, "save"), (1, "save")]
    assert history[0]["note"] == "wrong" and history[2]["note"] == "first pass"

    # Re-creating after a revert continues the version sequence.
    again = client.put("/v1/curation/gene/ALK?tumor_type=luad", json={"payload": {"citations": ["111"]}})
    assert again.json()["version"] == 4


def test_tumor_type_scopes_overrides(client, store):
    client.put("/v1/curation/gene/ALK", json={"payload": {"citations": ["222"]}})
    assert client.get("/v1/curation/gene/ALK").json()["version"] == 1
    assert client.get("/v1/curation/gene/ALK?tumor_type=LUAD").json()["version"] == 0
    assert store.overrides[("gene", "ALK", "")].pipeline_pmids_snapshot is None  # no pipeline row yet


def test_put_rejects_unknown_retracted_and_abstractless_pmids(client, store):
    body = {"payload": {"citations": ["222", "999", "666", "777"]}}
    resp = client.put("/v1/curation/gene/ALK", json=body)
    assert resp.status_code == 422
    assert resp.json()["detail"]["invalid_pmids"] == [
        {"pmid": "999", "reason": "not_found_or_no_abstract"},
        {"pmid": "666", "reason": "retracted"},
        {"pmid": "777", "reason": "not_found_or_no_abstract"},
    ]
    assert store.overrides == {} and store.history == []


@pytest.mark.parametrize(
    ("url", "body"),
    [
        ("/v1/curation/gene/ALK", {"payload": {}}),
        ("/v1/curation/gene/ALK", {"payload": {"citations": ["not-a-pmid"]}}),
        ("/v1/curation/gene/ALK", {"payload": {"unknown_section": "x"}}),
        ("/v1/curation/fusion/ALK", {"payload": {"pmids": ["111"]}}),
        ("/v1/curation/variant/ALK", {"payload": {"citations": ["111"]}}),
    ],
)
def test_put_validation_errors(client, url, body):
    assert client.put(url, json=body).status_code == 422


def test_fusion_put_fills_cards_and_snapshots_pipeline(client, store, monkeypatch):
    async def fake_fusion_evidence(fusion, tumor_type=None, max_results=None):
        assert fusion == "EML4::ALK" and tumor_type == "nsclc"
        return FusionEvidenceResult(fusion=fusion, pmids=["111", "444"])

    monkeypatch.setattr(curation_routes.literature, "retrieve_fusion_evidence", fake_fusion_evidence)
    resp = client.put(
        "/v1/curation/fusion/eml4--alk?tumor_type=NSCLC",
        json={"payload": {"interpretation": "Curated.", "pmids": ["222"],
                          "evidence_cards": [{"pmid": "222", "selected_reason": "Exact pair"}]}},
    )
    assert resp.status_code == 200, resp.text
    override = resp.json()["override"]
    assert resp.json()["subject_key"] == "EML4::ALK"
    assert override["payload"]["evidence_cards"][0]["fusion"] == "EML4::ALK"
    assert override["payload"]["evidence_cards"][0]["title"] == "Title 222"
    assert override["pipeline_pmids_snapshot"] == ["111", "444"]


# ---------------------------------------------------------------------------
# apply_override (pure)
# ---------------------------------------------------------------------------


def test_apply_override_curator_wins_and_sets_metadata():
    pipeline = _pipeline_annotation()
    override = _override(
        {
            "cancer_association_rationale": "Curated rationale.",
            "citations": ["222"],
            "therapeutic_precedent": {
                "summary": "Curated precedent.",
                "precedents": [{"pmid": "333", "therapies": ["crizotinib"], "quote": "q", "title": "T"}],
            },
        },
        version=3,
        snapshot=["111"],
    )
    curated = apply_override(pipeline, override)

    assert curated.cancer_association_rationale == "Curated rationale."
    assert curated.citations == ["222"]
    assert curated.evidence_cards == pipeline.evidence_cards  # untouched section keeps pipeline value
    assert (curated.curated, curated.curated_by, curated.curation_version) == (True, "curator@mskcc.org", 3)
    assert curated.curated_fields == ["cancer_association_rationale", "citations", "therapeutic_precedent"]
    actionability = curated.clinical_actionability
    assert actionability.curated is True and actionability.confidence_level == "high"
    assert actionability.curated_by == "curator@mskcc.org"
    assert actionability.pmids == ["333"] and actionability.evidence[0].therapies == ["crizotinib"]
    assert actionability.score_components == []
    # The input is not mutated, and is kept as the pipeline original.
    assert pipeline.curated is False and pipeline.citations == ["111"]
    assert curated._pipeline_annotation is pipeline
    assert not any(flag.code == NEWER_EVIDENCE_FLAG for flag in curated.quality_flags)


def test_apply_override_empty_precedent_clears_actionability():
    curated = apply_override(_pipeline_annotation(), _override({"therapeutic_precedent": {"precedents": []}}))
    assert curated.clinical_actionability is None


def test_apply_override_strips_retracted_curated_pmids_with_flag():
    override = _override({
        "citations": ["222", "666"],
        "evidence_cards": [{"pmid": "666"}, {"pmid": "222"}],
        "supporting_quotes": [{"pmid": "666", "quote": "x"}],
        "therapeutic_precedent": {"precedents": [{"pmid": "666"}]},
    })
    curated = apply_override(_pipeline_annotation(), override, retracted_pmids={"666"})

    assert curated.citations == ["222"]
    assert [card.pmid for card in curated.evidence_cards] == ["222"]
    assert curated.supporting_quotes == []
    assert curated.clinical_actionability is None  # every curated precedent was retracted
    codes = {flag.code: flag for flag in curated.quality_flags}
    assert "666" in codes[CURATED_RETRACTED_FLAG].detail and codes[CURATED_RETRACTED_FLAG].severity == "critical"
    assert "retracted_citations_removed" in codes


def test_apply_override_flags_newer_pipeline_evidence():
    pipeline = _pipeline_annotation(
        citations=["111", "444", "555"], evidence_cards=[EvidenceCard(pmid="888")]
    )
    curated = apply_override(pipeline, _override({"citations": ["555"]}, snapshot=["111"]))
    flag = next(flag for flag in curated.quality_flags if flag.code == NEWER_EVIDENCE_FLAG)
    # 555 is already in the curated version, so only the truly new PMIDs are listed.
    assert "444" in flag.detail and "888" in flag.detail and "555" not in flag.detail
    assert flag.severity == "warning"

    # Unknown snapshot (no pipeline row at curation time) never flags.
    unknown = apply_override(pipeline, _override({"citations": ["555"]}, snapshot=None))
    assert not any(flag.code == NEWER_EVIDENCE_FLAG for flag in unknown.quality_flags)


def test_apply_override_is_idempotent_on_already_curated_annotation():
    first = apply_override(_pipeline_annotation(), _override({"citations": ["222"]}, version=1))
    second = apply_override(first, _override({"cancer_association_rationale": "v2"}, version=2))
    # Re-applied on the pipeline original: version-1 sections do not linger.
    assert second.citations == ["111"]
    assert second.cancer_association_rationale == "v2" and second.curation_version == 2


def test_apply_fusion_override_with_retraction_and_newer_evidence():
    pipeline = FusionEvidenceResult(
        fusion="EML4::ALK", tumor_type="NSCLC", pmids=["111", "444"], retrieved_count=2,
        evidence_cards=[FusionEvidenceCard(pmid="111", fusion="EML4::ALK")],
    )
    override = _override(
        {"interpretation": "Curated.", "well_supported": True, "pmids": ["222", "666"],
         "evidence_cards": [{"pmid": "222"}, {"pmid": "666"}]},
        subject_type="fusion", key="EML4::ALK", tumor_type="nsclc", snapshot=["111"],
    )
    curated = apply_fusion_override(pipeline, override, retracted_pmids={"666"})
    assert curated.interpretation == "Curated." and curated.well_supported is True
    assert curated.pmids == ["222"] and curated.retrieved_count == 1
    assert [(c.pmid, c.fusion) for c in curated.evidence_cards] == [("222", "EML4::ALK")]
    assert curated.curated and curated.curation_version == 1
    codes = [flag.code for flag in curated.quality_flags]
    assert CURATED_RETRACTED_FLAG in codes and NEWER_EVIDENCE_FLAG in codes
    assert pipeline.curated is False


async def test_apply_current_overrides_checks_curated_pmids_for_retraction(monkeypatch):
    store = FakeCurationStore()
    store.overrides[("gene", "ALK", "luad")] = _override({"citations": ["222", "666"]})

    async def fake_retracted(pmids):
        assert set(pmids) == {"222", "666"}
        return {"666"}

    monkeypatch.setattr(curation, "find_retracted_pmids", fake_retracted)
    other = GeneAnnotation(gene="BRAF")
    applied = await apply_current_gene_overrides([_pipeline_annotation(), other], store, ["LUAD", "LUAD"])
    assert applied[0].citations == ["222"]
    assert any(flag.code == CURATED_RETRACTED_FLAG for flag in applied[0].quality_flags)
    assert applied[1] is other


async def test_override_lookup_failure_is_flagged_not_silent(caplog):
    store = FakeCurationStore()
    store.fail_curation = True
    annotation = _pipeline_annotation()
    (flagged,) = await apply_current_gene_overrides([annotation], store, ["luad"])
    flag = next(flag for flag in flagged.quality_flags if flag.code == CURATION_UNAVAILABLE_FLAG)
    assert flag.severity == "critical" and "could not be loaded" in flag.detail
    assert not flagged.curated and annotation.quality_flags == []  # input untouched
    assert flagged._pipeline_annotation is annotation  # stored runs stay pure
    assert any(r.levelname == "ERROR" and "lookup failed" in r.message for r in caplog.records)
    # Stores without curation support (e.g. the benchmark store) are a no-op.
    assert (await apply_current_gene_overrides([annotation], object(), ["luad"]))[0] is annotation


def test_retracting_every_curated_precedent_clears_actionability():
    override = _override({"therapeutic_precedent": {"summary": "Stale.", "precedents": [{"pmid": "666"}]}})
    curated = apply_override(_pipeline_annotation(), override, retracted_pmids={"666"})
    assert curated.clinical_actionability is None
    assert any(flag.code == CURATED_RETRACTED_FLAG for flag in curated.quality_flags)
    # A partially retracted precedent keeps the remaining, verified PMIDs.
    override = _override({"therapeutic_precedent": {"precedents": [{"pmid": "666"}, {"pmid": "333"}]}})
    curated = apply_override(_pipeline_annotation(), override, retracted_pmids={"666"})
    assert curated.clinical_actionability.pmids == ["333"]


def test_result_payload_for_storage_is_pipeline_pure():
    curated = apply_override(_pipeline_annotation(), _override({"citations": ["222"]}))
    result = AnnotationResult(run_id="r", timestamp=NOW, fusions_processed=1, genes_annotated=1,
                              annotations=[curated])
    stored = result_payload_for_storage(result)["annotations"][0]
    assert stored["citations"] == ["111"] and stored["curated"] is False


# ---------------------------------------------------------------------------
# Orchestrator: cache reuse + fresh runs
# ---------------------------------------------------------------------------


def _resolved(gene: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True)


@pytest.fixture
def pipeline_env(monkeypatch):
    async def fake_normalize_fusions(_inputs, **_kwargs):
        return {"ALK": (_resolved("ALK"), ["ALK"])}

    async def no_retracted(_annotation):
        return set()

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "find_retracted_annotation_pmids", no_retracted)
    monkeypatch.setattr(curation, "find_retracted_pmids", _no_retractions)
    monkeypatch.setattr(orchestrator.settings, "gene_cache_enabled", True)


async def test_override_applied_on_cache_reuse_without_touching_gene_annotations(pipeline_env, monkeypatch):
    store = FakeCurationStore()
    store.gene_cache[("ALK", "luad")] = {
        "annotation": _pipeline_annotation(evidence_support_score=0.95).model_dump(),
        "updated_at": datetime.now(timezone.utc) - timedelta(days=1),
        "last_pubmed_checked_at": datetime.now(timezone.utc) - timedelta(days=1),
    }
    store.overrides[("gene", "ALK", "luad")] = _override({"citations": ["222"]}, snapshot=["111"])

    async def fail_if_called(**_kwargs):
        raise AssertionError("fresh cache should be reused")

    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)
    result = await run_pipeline([FusionInput(fusion="ALK", tumor_type="LUAD")], run_store=store)

    annotation = result.annotations[0]
    assert annotation.cache_status == "reused"
    assert annotation.curated and annotation.citations == ["222"]
    assert annotation.analysis_tumor_type == "LUAD"
    assert store.saved_gene_annotations == [] and store.checked == []


async def test_fresh_run_and_force_refresh_keep_override_and_pipeline_row_pure(pipeline_env, monkeypatch):
    store = FakeCurationStore()
    store.overrides[("gene", "ALK", "")] = _override({"citations": ["222"]}, tumor_type="")

    async def fake_annotate_gene(**_kwargs):
        return _pipeline_annotation(cache_status="refreshed")

    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    for force_refresh in (False, True):
        result = await run_pipeline(["ALK"], run_store=store, force_refresh=force_refresh)
        assert result.annotations[0].curated and result.annotations[0].citations == ["222"]

    # gene_annotations only ever received pipeline output; the override survives refreshes.
    assert [a.citations for a in store.saved_gene_annotations] == [["111"], ["111"]]
    assert not any(a.curated for a in store.saved_gene_annotations)
    assert ("gene", "ALK", "") in store.overrides


# ---------------------------------------------------------------------------
# Serving paths
# ---------------------------------------------------------------------------


def test_annotate_persists_pipeline_pure_run_and_serves_current_override(client, store, monkeypatch):
    async def fake_run_pipeline(fusions, **_kwargs):
        annotation = _pipeline_annotation()
        annotation.analysis_tumor_type = "LUAD"
        curated = apply_override(annotation, _override({"citations": ["222"]}))
        return AnnotationResult(run_id="run-cur", timestamp=NOW, fusions_processed=1, genes_annotated=1,
                                annotations=[curated])

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    resp = client.post("/v1/annotate", json={"fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}]})
    assert resp.json()["annotations"][0]["curated"] is True
    assert store.runs["run-cur"]["annotations"][0]["citations"] == ["111"]
    assert store.runs["run-cur"]["annotations"][0]["curated"] is False

    # Old link, no override yet -> pipeline output.
    assert client.get("/v1/annotate/run-cur").json()["annotations"][0]["curated"] is False

    # Override created later -> the old link shows the CURRENT override.
    client.put("/v1/curation/gene/ALK?tumor_type=LUAD", json={"payload": {"citations": ["333"]}})
    served = client.get("/v1/annotate/run-cur").json()["annotations"][0]
    assert served["curated"] is True and served["citations"] == ["333"] and served["curation_version"] == 1

    # Reverted -> the link is back to pipeline output.
    client.delete("/v1/curation/gene/ALK?tumor_type=LUAD")
    assert client.get("/v1/annotate/run-cur").json()["annotations"][0]["citations"] == ["111"]


def test_run_link_matches_tumor_type_from_fusion_inputs(client, store):
    store.runs["run-fusion"] = AnnotationResult(
        run_id="run-fusion", timestamp=NOW, fusions_processed=2, genes_annotated=2,
        annotations=[_pipeline_annotation(fusions=["EML4::ALK"]), GeneAnnotation(gene="EML4", fusions=["EML4::ALK"])],
    ).model_dump()
    store.requests["run-fusion"] = {"fusions": [{"fusion": "EML4::ALK", "tumor_type": "NSCLC"}]}
    store.overrides[("gene", "ALK", "nsclc")] = _override({"citations": ["222"]}, tumor_type="nsclc")

    served = client.get("/v1/annotate/run-fusion").json()["annotations"]
    assert served[0]["curated"] is True and served[1]["curated"] is False


def test_fusion_evidence_job_applies_override_and_persists_pure(client, store, monkeypatch):
    async def fake_fusion_evidence(fusion, tumor_type=None, max_results=None):
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, pmids=["111"], retrieved_count=1,
                                    interpretation="Pipeline.")

    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_fusion_evidence)
    store.runs["run-fe"] = AnnotationResult(run_id="run-fe", timestamp=NOW, fusions_processed=1,
                                            genes_annotated=0, annotations=[]).model_dump()
    store.overrides[("fusion", "EML4::ALK", "nsclc")] = _override(
        {"interpretation": "Curated."}, subject_type="fusion", key="EML4::ALK", tumor_type="nsclc"
    )

    created = client.post("/v1/fusion-evidence/jobs",
                          json={"fusions": [{"fusion": "EML4::ALK", "tumor_type": "NSCLC"}], "run_id": "run-fe"})
    status_url = created.json()["status_url"]
    for _ in range(100):
        job = client.get(status_url).json()
        if job["status"] == "complete":
            break
        time.sleep(0.02)
    assert job["fusion_evidence"][0]["interpretation"] == "Curated."
    assert job["fusion_evidence"][0]["curated"] is True
    assert store.runs["run-fe"]["fusion_evidence"][0]["interpretation"] == "Pipeline."

    # The shared run link overlays the fusion override too.
    served = client.get("/v1/annotate/run-fe").json()["fusion_evidence"][0]
    assert served["interpretation"] == "Curated." and served["curated"] is True


def test_gene_query_slim_response_reports_curated(client, store, monkeypatch):
    async def fake_run_pipeline(fusions, **_kwargs):
        curated = apply_override(_pipeline_annotation(), _override({"citations": ["222"]}))
        return AnnotationResult(run_id="run-gq", timestamp=NOW, fusions_processed=1, genes_annotated=1,
                                annotations=[curated])

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    body = client.post("/v1/genes/query", json={"genes": ["ALK"]}).json()
    assert body["results"][0]["curated"] is True
    assert body["results"][0]["citation_pmids"] == ["222"]
    assert store.runs["run-gq"]["annotations"][0]["citations"] == ["111"]


def test_quality_flag_model_unchanged_for_pipeline_results():
    # Backwards compatible defaults for payloads stored before curation existed.
    legacy = FusionEvidenceResult(**{"fusion": "A::B", "pmids": ["1"]})
    assert legacy.curated is False and legacy.quality_flags == []
    assert GeneAnnotation(gene="ALK").curated_fields == []
    assert isinstance(QualityFlag(code="x", label="y"), QualityFlag)


# ---------------------------------------------------------------------------
# Review follow-ups: live overrides on every poll, enrichment, aliases,
# unavailable curation, stale reverts, batching
# ---------------------------------------------------------------------------


def _poll(client, url: str) -> Dict[str, Any]:
    for _ in range(200):
        body = client.get(url).json()
        if body["status"] in ("complete", "failed"):
            return body
        time.sleep(0.01)
    raise AssertionError(f"job at {url} did not finish")


def _fake_pipeline(tumor_type: Optional[str] = "LUAD"):
    async def fake_run_pipeline(fusions, on_annotation=None, **_kwargs):
        annotation = _pipeline_annotation()
        annotation.analysis_tumor_type = tumor_type
        if on_annotation:
            await on_annotation(annotation)
        return AnnotationResult(run_id="run-job", timestamp=NOW, fusions_processed=1, genes_annotated=1,
                                annotations=[annotation], annotation_tumor_types={"ALK": tumor_type})

    return fake_run_pipeline


def test_annotation_job_poll_reflects_post_completion_edit_retraction_and_revert(client, store, monkeypatch):
    monkeypatch.setattr(main, "run_pipeline", _fake_pipeline())
    url = client.post("/v1/annotate/jobs", json={"fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}]}).json()[
        "status_url"
    ]
    job = _poll(client, url)
    assert job["annotations"][0]["curated"] is False and job["result"]["annotations"][0]["curated"] is False

    # Edit after the job finished -> next poll shows it, in both annotations and result.
    client.put("/v1/curation/gene/ALK?tumor_type=LUAD", json={"payload": {"citations": ["222", "333"]}})
    job = client.get(url).json()
    assert job["annotations"][0]["citations"] == ["222", "333"]
    assert job["result"]["annotations"][0]["curation_version"] == 1

    # A curated PMID retracted later is stripped on the next poll.
    async def retract_333(pmids):
        return {"333"} & set(pmids)

    monkeypatch.setattr(curation, "find_retracted_pmids", retract_333)
    job = client.get(url).json()
    assert job["annotations"][0]["citations"] == ["222"]
    assert CURATED_RETRACTED_FLAG in [f["code"] for f in job["annotations"][0]["quality_flags"]]

    # Revert -> pipeline output again; the stored run never held curated content.
    client.delete("/v1/curation/gene/ALK?tumor_type=LUAD")
    job = client.get(url).json()
    assert job["annotations"][0]["curated"] is False and job["annotations"][0]["citations"] == ["111"]
    assert store.runs["run-job"]["annotations"][0]["curated"] is False


def test_gene_query_job_poll_reflects_post_completion_edit(client, store, monkeypatch):
    monkeypatch.setattr(main, "run_pipeline", _fake_pipeline(tumor_type=None))
    url = client.post("/v1/genes/query/jobs", json={"genes": ["ALK"]}).json()["status_url"]
    assert _poll(client, url)["results"][0]["curated"] is False

    client.put("/v1/curation/gene/ALK", json={"payload": {"citations": ["222"]}})
    result = client.get(url).json()["results"][0]
    assert result["curated"] is True and result["citation_pmids"] == ["222"]
    assert result["curated_by"] == "local@internal" and result["curation_version"] == 1
    assert result["curated_fields"] == ["citations"] and result["curated_at"]

    client.delete("/v1/curation/gene/ALK")
    assert client.get(url).json()["results"][0]["curated"] is False


def test_fusion_job_poll_reflects_post_completion_edit(client, store, monkeypatch):
    async def fake_fusion_evidence(fusion, tumor_type=None, max_results=None):
        return FusionEvidenceResult(fusion=fusion, tumor_type=tumor_type, pmids=["111"], interpretation="Pipeline.")

    monkeypatch.setattr(main, "retrieve_fusion_evidence", fake_fusion_evidence)
    monkeypatch.setattr(curation_routes.literature, "retrieve_fusion_evidence", fake_fusion_evidence)
    url = client.post("/v1/fusion-evidence/jobs", json={"fusions": ["EML4::ALK"]}).json()["status_url"]
    assert _poll(client, url)["fusion_evidence"][0]["curated"] is False

    client.put("/v1/curation/fusion/EML4::ALK", json={"payload": {"interpretation": "Curated."}})
    assert client.get(url).json()["fusion_evidence"][0]["interpretation"] == "Curated."
    client.delete("/v1/curation/fusion/EML4::ALK")
    assert client.get(url).json()["fusion_evidence"][0]["interpretation"] == "Pipeline."


@pytest.fixture
def fake_enrichment(monkeypatch):
    from src.pipeline import enrichment

    async def fake_retrieve(gene, fusions, **_kwargs):
        return [], "tier1"

    async def fake_select(gene, records, limit, **_kwargs):
        return records

    async def fake_synthesize(**_kwargs):
        return {}

    def fake_build(**kwargs):
        return _pipeline_annotation(gene=kwargs["gene"], gene_summary="Enriched summary.")

    monkeypatch.setattr(enrichment, "retrieve_literature", fake_retrieve)
    monkeypatch.setattr(enrichment, "get_msk_genie_prevalence", lambda gene: None)
    monkeypatch.setattr(enrichment, "rank_literature_for_synthesis", lambda records, *a: (records, []))
    monkeypatch.setattr(enrichment, "select_papers_for_synthesis", fake_select)
    monkeypatch.setattr(enrichment, "synthesize_gene_annotation", fake_synthesize)
    monkeypatch.setattr(enrichment, "build_gene_annotation", fake_build)


def test_enrichment_ignores_forged_curation_and_applies_live_override(client, store, fake_enrichment):
    forged = GeneAnnotation(
        gene="ALK", in_oncokb=True, citations=["999"], cancer_association_rationale="Forged.",
        curated=True, curated_by="mallory@mskcc.org", curation_version=42, curated_fields=["citations"],
        quality_flags=[QualityFlag(code=NEWER_EVIDENCE_FLAG, label="forged")],
    ).model_dump()

    # No override exists: forged curation metadata and content are dropped.
    url = client.post("/v1/annotate/enrichment/jobs", json={"annotations": [forged]}).json()["status_url"]
    enriched = _poll(client, url)["annotations"][0]
    assert enriched["curated"] is False and enriched["curated_by"] is None and enriched["curated_fields"] == []
    assert enriched["citations"] == ["111"] and enriched["cancer_association_rationale"] == "Pipeline rationale."
    assert enriched["quality_flags"] == []

    # A live, tumor-scoped override is applied server-side using the tumor context...
    client.put("/v1/curation/gene/ALK?tumor_type=LUAD", json={"payload": {"citations": ["222"]}})
    url = client.post(
        "/v1/annotate/enrichment/jobs", json={"annotations": [forged], "tumor_types": {"ALK": "LUAD"}}
    ).json()["status_url"]
    enriched = _poll(client, url)["annotations"][0]
    assert enriched["curated"] is True and enriched["curated_by"] == "local@internal"
    assert enriched["citations"] == ["222"] and enriched["gene_summary"] == "Enriched summary."

    # ...and a later revert shows up on the next poll.
    client.delete("/v1/curation/gene/ALK?tumor_type=LUAD")
    assert client.get(url).json()["annotations"][0]["curated"] is False


def test_new_runs_persist_tumor_context_used_by_shared_links(client, store, monkeypatch):
    async def fake_run_pipeline(fusions, **_kwargs):
        annotation = _pipeline_annotation(gene="KAT6A")
        annotation.analysis_tumor_type = "AML"
        return AnnotationResult(run_id="run-map", timestamp=NOW, fusions_processed=1, genes_annotated=1,
                                annotations=[annotation], annotation_tumor_types={"KAT6A": "AML"})

    monkeypatch.setattr(main, "run_pipeline", fake_run_pipeline)
    client.post("/v1/annotate", json={"fusions": [{"fusion": "MOZ", "tumor_type": "AML"}]})
    assert store.runs["run-map"]["annotation_tumor_types"] == {"KAT6A": "AML"}
    store.requests["run-map"] = {}  # the persisted map alone must be enough

    store.overrides[("gene", "KAT6A", "aml")] = _override({"citations": ["222"]}, key="KAT6A", tumor_type="aml")
    served = client.get("/v1/annotate/run-map").json()
    assert served["annotations"][0]["curated"] is True
    assert served["annotation_tumor_types"] == {"KAT6A": "AML"}


def test_old_shared_link_resolves_alias_inputs_to_tumor_scoped_override(client, store):
    # Saved before annotation_tumor_types existed; the input was the alias MOZ.
    stored = AnnotationResult(
        run_id="run-alias", timestamp=NOW, fusions_processed=2, genes_annotated=2,
        annotations=[_pipeline_annotation(gene="KAT6A"), GeneAnnotation(gene="BRAF")],
    ).model_dump()
    del stored["annotation_tumor_types"]
    store.runs["run-alias"] = stored
    store.requests["run-alias"] = {"fusions": [{"fusion": "MOZ", "tumor_type": "AML"}, "BRAF"]}
    store.overrides[("gene", "KAT6A", "aml")] = _override({"citations": ["222"]}, key="KAT6A", tumor_type="aml")
    store.overrides[("gene", "KAT6A", "")] = _override({"citations": ["333"]}, key="KAT6A", tumor_type="")

    served = client.get("/v1/annotate/run-alias").json()
    assert served["annotations"][0]["citations"] == ["222"]  # the AML override, not the untyped one
    assert served["annotations"][1]["curated"] is False
    assert served["annotation_tumor_types"] == {"KAT6A": "AML", "BRAF": None}


def test_curation_unavailable_is_flagged_on_runs_and_jobs_and_503_on_curation_api(client, store, monkeypatch):
    store.runs["run-down"] = AnnotationResult(
        run_id="run-down", timestamp=NOW, fusions_processed=1, genes_annotated=1,
        annotations=[_pipeline_annotation()], annotation_tumor_types={"ALK": "LUAD"},
        fusion_evidence=[FusionEvidenceResult(fusion="EML4::ALK")],
    ).model_dump()
    store.fail_curation = True

    served = client.get("/v1/annotate/run-down").json()
    assert served["curation_unavailable"] is True
    assert CURATION_UNAVAILABLE_FLAG in [f["code"] for f in served["annotations"][0]["quality_flags"]]
    assert CURATION_UNAVAILABLE_FLAG in [f["code"] for f in served["fusion_evidence"][0]["quality_flags"]]
    # The stored run is not polluted with the flag.
    assert store.runs["run-down"]["annotations"][0]["quality_flags"] == []

    monkeypatch.setattr(main, "run_pipeline", _fake_pipeline())
    url = client.post("/v1/annotate/jobs", json={"fusions": ["ALK"]}).json()["status_url"]
    job = _poll(client, url)
    assert job["curation_unavailable"] is True and job["result"]["curation_unavailable"] is True

    assert client.get("/v1/curation/gene/ALK").status_code == 503
    assert client.get("/v1/curation/gene/ALK/history").status_code == 503
    assert client.put("/v1/curation/gene/ALK", json={"payload": {"citations": ["222"]}}).status_code == 503
    assert client.delete("/v1/curation/gene/ALK").status_code == 503


def test_stale_delete_of_already_reverted_override_is_409(client, store):
    client.put("/v1/curation/gene/ALK", json={"payload": {"citations": ["222"]}})
    assert client.delete("/v1/curation/gene/ALK?expected_version=1").status_code == 200
    stale = client.delete("/v1/curation/gene/ALK?expected_version=1")
    assert stale.status_code == 409 and stale.json()["detail"]["current_version"] == 0
    assert client.delete("/v1/curation/gene/ALK").status_code == 404  # no version given: nothing to revert


async def test_run_pipeline_batches_override_lookup_and_retraction_check(pipeline_env, monkeypatch):
    genes = ["ALK", "BRAF", "EGFR"]

    async def fake_normalize_fusions(_inputs, **_kwargs):
        return {gene: (_resolved(gene), [gene]) for gene in genes}

    async def fake_annotate_gene(**kwargs):
        return _pipeline_annotation(gene=kwargs["gene"], cache_status="refreshed")

    retraction_calls = []

    async def fake_retracted(pmids):
        retraction_calls.append(sorted(pmids))
        return set()

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    monkeypatch.setattr(curation, "find_retracted_pmids", fake_retracted)
    store = FakeCurationStore()
    for gene in ("ALK", "BRAF"):
        store.overrides[("gene", gene, "")] = _override({"citations": ["222", "333"]}, key=gene, tumor_type="")
    progress = []

    result = await run_pipeline(genes, run_store=store, on_annotation=progress.append)

    assert store.lookup_calls == 1
    assert retraction_calls == [["222", "333"]]
    assert [a.curated for a in result.annotations] == [True, True, False]
    assert result.annotation_tumor_types == {"ALK": None, "BRAF": None, "EGFR": None}
    assert result.curation_unavailable is False
    # Progress callbacks (and so job storage) only ever see pipeline output.
    assert not any(a.curated for a in progress)


async def test_run_pipeline_reports_curation_unavailable(pipeline_env, monkeypatch):
    async def fake_annotate_gene(**_kwargs):
        return _pipeline_annotation(cache_status="refreshed")

    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    store = FakeCurationStore()
    store.fail_curation = True
    result = await run_pipeline(["ALK"], run_store=store)
    assert result.curation_unavailable is True
    assert CURATION_UNAVAILABLE_FLAG in [f.code for f in result.annotations[0].quality_flags]
    # Neither gene_annotations nor the stored-run payload carries the flag.
    assert store.saved_gene_annotations[0].quality_flags == []
    stored = result_payload_for_storage(result)
    assert stored["curation_unavailable"] is False and stored["annotations"][0]["quality_flags"] == []


# ---------------------------------------------------------------------------
# Round 2: legacy tumor-context reconstruction, partial jobs, contention
# ---------------------------------------------------------------------------


def _legacy_run(store: FakeCurationStore, run_id: str, annotations: List[GeneAnnotation], request: Dict[str, Any]):
    """A run saved before annotation_tumor_types existed."""
    stored = AnnotationResult(run_id=run_id, timestamp=NOW, fusions_processed=len(annotations),
                              genes_annotated=len(annotations), annotations=annotations).model_dump()
    del stored["annotation_tumor_types"]
    store.runs[run_id] = stored
    store.requests[run_id] = request


async def test_pipeline_and_reconstruction_share_duplicate_input_rule(pipeline_env, monkeypatch):
    async def fake_annotate_gene(**_kwargs):
        return _pipeline_annotation(cache_status="refreshed")

    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)
    inputs = [FusionInput(fusion="ALK", tumor_type="LUAD"), FusionInput(fusion="ALK", tumor_type="NSCLC")]
    result = await run_pipeline(inputs, run_store=FakeCurationStore())
    assert result.annotation_tumor_types == {"ALK": "NSCLC"}  # the pipeline's rule

    store = FakeCurationStore()
    _legacy_run(store, "run-dup", [_pipeline_annotation()], {"fusions": [i.model_dump() for i in inputs]})
    legacy = AnnotationResult(**store.runs["run-dup"])
    monkeypatch.setattr(curation, "normalize_fusions", _fake_normalize_fusions)
    assert await curation.ensure_annotation_tumor_types(legacy, store, "run-dup") is True
    assert legacy.annotation_tumor_types == result.annotation_tumor_types


def test_legacy_link_with_duplicate_inputs_uses_pipeline_tumor_context(client, store):
    _legacy_run(store, "run-dup", [_pipeline_annotation()],
                {"fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}, {"fusion": "ALK", "tumor_type": "NSCLC"}]})
    store.overrides[("gene", "ALK", "luad")] = _override({"citations": ["333"]}, tumor_type="luad")
    store.overrides[("gene", "ALK", "nsclc")] = _override({"citations": ["222"]}, tumor_type="nsclc")
    assert client.get("/v1/annotate/run-dup").json()["annotations"][0]["citations"] == ["222"]


def test_unresolved_alias_never_applies_untyped_override_or_persists_guess(client, store, monkeypatch):
    _legacy_run(store, "run-moz", [_pipeline_annotation(gene="KAT6A"), GeneAnnotation(gene="BRAF")],
                {"fusions": [{"fusion": "MOZ", "tumor_type": "AML"}, "BRAF"]})
    store.overrides[("gene", "KAT6A", "")] = _override({"citations": ["333"]}, key="KAT6A", tumor_type="")
    store.overrides[("gene", "KAT6A", "aml")] = _override({"citations": ["222"]}, key="KAT6A", tumor_type="aml")

    async def hgnc_down(_inputs, **_kwargs):
        raise RuntimeError("HGNC unreachable")

    monkeypatch.setattr(curation, "normalize_fusions", hgnc_down)
    served = client.get("/v1/annotate/run-moz").json()
    kat6a = served["annotations"][0]
    assert kat6a["curated"] is False and kat6a["citations"] == ["111"]  # untyped override NOT applied
    assert CURATION_UNAVAILABLE_FLAG in [f["code"] for f in kat6a["quality_flags"]]
    assert served["curation_unavailable"] is True
    assert "KAT6A" not in served["annotation_tumor_types"]
    assert "annotation_tumor_types" not in store.runs["run-moz"]  # nothing guessed was persisted

    # A symbol lookup that "succeeds" with lookup_failed is just as unresolved.
    async def lookup_failed(inputs, **_kwargs):
        return {"MOZ": (ResolvedGene(input_symbol="MOZ", resolved=False, lookup_failed=True), list(inputs))}

    monkeypatch.setattr(curation, "normalize_fusions", lookup_failed)
    assert client.get("/v1/annotate/run-moz").json()["annotations"][0]["curated"] is False

    # Once resolution works, the AML override applies and the map is persisted.
    monkeypatch.setattr(curation, "normalize_fusions", _fake_normalize_fusions)
    served = client.get("/v1/annotate/run-moz").json()
    assert served["annotations"][0]["citations"] == ["222"] and served["curation_unavailable"] is False
    assert store.runs["run-moz"]["annotation_tumor_types"] == {"KAT6A": "AML", "BRAF": None}


def test_sanitizer_write_back_does_not_persist_unresolved_tumor_context(client, store, monkeypatch):
    _legacy_run(store, "run-san", [_pipeline_annotation(gene="KAT6A")],
                {"fusions": [{"fusion": "MOZ", "tumor_type": "AML"}]})
    store.overrides[("gene", "KAT6A", "aml")] = _override({"cancer_association_rationale": "AML."},
                                                           key="KAT6A", tumor_type="aml")

    async def retract_111(pmids):
        return {"111"} & set(pmids)

    async def hgnc_down(_inputs, **_kwargs):
        raise RuntimeError("HGNC unreachable")

    monkeypatch.setattr(result_sanitizer, "find_retracted_pmids", retract_111)
    monkeypatch.setattr(curation, "normalize_fusions", hgnc_down)
    client.get("/v1/annotate/run-san")
    persisted = store.runs["run-san"]
    assert persisted["annotations"][0]["citations"] == []  # the sanitizer did write back
    assert persisted["annotation_tumor_types"] == {}  # ...without an unresolved/null entry

    monkeypatch.setattr(curation, "normalize_fusions", _fake_normalize_fusions)
    served = client.get("/v1/annotate/run-san").json()  # reconstruction is retried
    assert served["annotations"][0]["cancer_association_rationale"] == "AML."
    assert store.runs["run-san"]["annotation_tumor_types"] == {"KAT6A": "AML"}


def test_reconstructed_context_is_persisted_once_and_only_typed_inputs_normalized(client, store, monkeypatch):
    _legacy_run(store, "run-once", [_pipeline_annotation(gene="KAT6A"), GeneAnnotation(gene="BRAF")],
                {"fusions": [{"fusion": "MOZ", "tumor_type": "AML"}, "BRAF", {"fusion": "EGFR"}]})
    calls = []

    async def counting_normalize(inputs, **kwargs):
        calls.append(list(inputs))
        return await _fake_normalize_fusions(inputs, **kwargs)

    monkeypatch.setattr(curation, "normalize_fusions", counting_normalize)
    client.get("/v1/annotate/run-once")  # no sanitization change; map persisted anyway
    assert store.runs["run-once"]["annotation_tumor_types"] == {"KAT6A": "AML", "BRAF": None}
    client.get("/v1/annotate/run-once")
    assert calls == [["MOZ"]]


def test_partial_annotation_job_exposes_completed_genes_tumor_context(store, monkeypatch):
    import threading

    class _StoreFactory:  # lifespan opens app.state.run_store via RunStore.create()
        @staticmethod
        async def create():
            return store

    monkeypatch.setattr(store, "close", lambda: _noop(), raising=False)
    monkeypatch.setattr(main, "RunStore", _StoreFactory)
    # One portal (event loop) for the whole test, so the background job keeps
    # running between polls instead of stalling on a per-request loop.
    with TestClient(main.app) as client:
        _check_partial_job(client, store, monkeypatch, threading.Event())


async def _noop():
    return None


def _check_partial_job(client, store, monkeypatch, release):

    async def slow_pipeline(fusions, on_annotation=None, **_kwargs):
        import asyncio

        annotation = _pipeline_annotation()
        annotation.analysis_tumor_type = "LUAD"
        await on_annotation(annotation)
        while not release.is_set():
            await asyncio.sleep(0.01)
        return AnnotationResult(run_id="run-partial", timestamp=NOW, fusions_processed=2, genes_annotated=1,
                                annotations=[annotation], annotation_tumor_types={"ALK": "LUAD"})

    monkeypatch.setattr(main, "run_pipeline", slow_pipeline)
    store.overrides[("gene", "ALK", "luad")] = _override({"citations": ["222"]})
    url = client.post("/v1/annotate/jobs", json={"fusions": [{"fusion": "ALK", "tumor_type": "LUAD"}]}).json()[
        "status_url"
    ]
    try:
        for _ in range(200):
            body = client.get(url).json()
            if body["genes_completed"]:
                break
            time.sleep(0.01)
        assert body["status"] == "running" and body["result"] is None
        assert body["annotation_tumor_types"] == {"ALK": "LUAD"}
        assert body["annotations"][0]["curated"] is True  # overlaid mid-run too
    finally:
        release.set()
    assert _poll(client, url)["annotation_tumor_types"] == {"ALK": "LUAD"}


def test_lock_contention_is_retryable_503(client, store, monkeypatch):
    from src.pipeline.curation_store import CurationStorageContention

    async def contended(**_kwargs):
        raise CurationStorageContention("busy")

    monkeypatch.setattr(store, "save_curated_override", contended)
    resp = client.put("/v1/curation/gene/ALK", json={"payload": {"citations": ["222"]}})
    assert resp.status_code == 503 and resp.headers["retry-after"] == "1"
    assert "contention" in resp.json()["detail"]
