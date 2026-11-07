"""Tumor-specific literature gate (acgc_tumor_specific_v2).

The KLK3/KICH case from the local preview: v1 ("pipeline not insufficient
AND >=1 citation") surfaced KLK3 in kidney chromophobe purely on
prostate-cancer papers. v2 must only count papers that describe the gene IN
the requested tumor type, cite only those, drop PMIDs the model invents, and
never reuse verdicts of the v1 rule.

Retrieval and the LLM judge are injected fakes; ACGC's real (pure)
tumor-alias matcher is used as the deterministic guard.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.expression_context.bundle_store import InMemoryBundleStore
from src.expression_context.literature_gate import (
    JUDGE_MODEL_PURPOSE,
    JUDGE_TOOL,
    TUMOR_SPECIFIC_GATE,
    LiteratureGate,
    LiteratureGateError,
    TumorSpecificGate,
    is_supported,
    verdict_supported,
)
from src.expression_context.models import LiteratureVerdict, PaperJudgement, StoredPair, TumorContext
from src.expression_context.settings import ExpressionContextSettings
from src.models.schema import LiteratureRecord
from tests.expression_context_helpers import (
    M3,
    M3_RELEASE_ID,
    FakeLiteratureGate,
    build_bundle,
    gene_file,
    make_service,
    seed_bundle,
)

pytestmark = [
    pytest.mark.no_real_redis,
    pytest.mark.httpx_mock(assert_all_responses_were_requested=False),
]

KICH = TumorContext(
    oncotree_code="KICH", name="Kidney Chromophobe",
    tumor_type_query="chromophobe renal cell carcinoma",
)
PRAD = TumorContext(oncotree_code="PRAD", name="Prostate Adenocarcinoma", tumor_type_query="prostate cancer")


def rec(pmid: str, title: str, abstract: str) -> LiteratureRecord:
    return LiteratureRecord(pmid=pmid, title=title, abstract=abstract, journal="J")


# What the preview retrieved for KLK3 + "chromophobe renal cell carcinoma":
# prostate papers (3 shared with KLK3/PRAD), nothing about chromophobe RCC.
PROSTATE_PAPERS = [
    rec("30000001", "PSA screening in prostate cancer", "KLK3 (PSA) levels track prostate cancer burden."),
    rec("30000002", "KLK3 regulation by androgen receptor", "In prostate cancer cells KLK3 is AR-driven."),
    rec("30000003", "Kallikreins in prostate adenocarcinoma", "KLK3 is highly expressed in prostate cancer."),
    rec("30000004", "Serum PSA after prostatectomy", "Biochemical recurrence of prostate cancer."),
    rec("30000005", "KLK3 germline variants", "KLK3 variants and prostate cancer risk."),
]
# A renal paper that mentions chromophobe RCC only as a differential diagnosis.
RENAL_DIFFERENTIAL = rec(
    "30000010", "Immunohistochemistry of renal oncocytic tumours",
    "Panels distinguishing chromophobe renal cell carcinoma from oncocytoma; "
    "metastatic prostate cancer was excluded by negative KLK3 (PSA) staining.",
)
KICH_PAPER = rec(
    "30000020", "KLK3 transcripts in chromophobe renal cell carcinoma",
    "We report KLK3 mRNA expression in a subset of chromophobe renal cell carcinoma tumors.",
)


class Retriever:
    def __init__(self, records):
        self.records = records
        self.calls = []

    async def __call__(self, gene, tumor_type_query):
        self.calls.append((gene, tumor_type_query))
        return list(self.records)


class Judge:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    async def __call__(self, **kwargs):
        self.calls.append(kwargs)
        if self.exc:
            raise self.exc
        return self.result


def gate(records, judge, **kwargs) -> TumorSpecificGate:
    return TumorSpecificGate(
        retrieve=Retriever(records), judge=judge, model=kwargs.pop("model", "judge-model"), **kwargs
    )


# --------------------------------------------------------------- the rule


def test_gate_satisfies_protocol_and_is_versioned():
    assert isinstance(TumorSpecificGate(), LiteratureGate)
    assert TumorSpecificGate.name == TUMOR_SPECIFIC_GATE == "acgc_tumor_specific_v2"
    assert ExpressionContextSettings(_env_file=None).literature_gate == TUMOR_SPECIFIC_GATE


def _verdict(**fields) -> LiteratureVerdict:
    base = dict(
        literature_gate=TUMOR_SPECIFIC_GATE, tumor_type_query="q", supported=True,
        insufficient_evidence=False, evaluated_at=datetime.now(timezone.utc),
    )
    base.update(fields)
    return LiteratureVerdict(**base)


def test_verdict_supported_rule():
    specific = [PaperJudgement(pmid="1", tumor_specific=True), PaperJudgement(pmid="2")]
    assert verdict_supported(_verdict(verdict="supported", pmids=["1"], papers=specific))
    assert not verdict_supported(_verdict(verdict="supported", pmids=[], papers=specific))
    assert not verdict_supported(_verdict(verdict="insufficient", pmids=["1"], papers=specific))
    assert not verdict_supported(_verdict(verdict="not_supported", pmids=["1"], papers=specific))
    # A cited PMID that wasn't judged tumor-specific invalidates the verdict.
    assert not verdict_supported(_verdict(verdict="supported", pmids=["1", "2"], papers=specific))
    # Legacy (no verdict field) keeps the v1 rule.
    assert verdict_supported(_verdict(pmids=["1"])) is is_supported(False, ["1"]) is True


# ------------------------------------------------------- KLK3 / KICH case


async def test_klk3_kich_prostate_only_papers_are_insufficient_without_llm_call():
    judge = Judge(result={"papers": [], "verdict": "supported", "rationale": "x"})
    g = gate(PROSTATE_PAPERS, judge)
    verdict = await g.evaluate("KLK3", KICH)
    assert judge.calls == []  # nothing mentions the tumor type: no LLM spend
    assert verdict.verdict == "insufficient" and verdict.supported is False
    assert verdict.pmids == [] and verdict.papers == []
    assert verdict.retrieved_count == 5
    assert "Kidney Chromophobe" in verdict.rationale and "other tumor types" in verdict.rationale
    assert g._retrieve.calls == [("KLK3", "chromophobe renal cell carcinoma")]


async def test_klk3_kich_passing_mention_judged_not_specific():
    judge = Judge(result={
        "papers": [{"pmid": "30000010", "tumor_specific": False,
                    "reason": "Differential diagnosis; KLK3 used as a prostate marker."}],
        "verdict": "insufficient",
        "rationale": "No paper reports KLK3 in chromophobe RCC.",
    })
    verdict = await gate(PROSTATE_PAPERS + [RENAL_DIFFERENTIAL], judge).evaluate("KLK3", KICH)
    # Only the paper that mentions the tumor type was sent to the judge.
    assert "PMID: 30000010" in judge.calls[0]["user"]
    assert "30000001" not in judge.calls[0]["user"]
    assert verdict.supported is False and verdict.pmids == []
    assert [p.pmid for p in verdict.papers] == ["30000010"]
    assert verdict.papers[0].matched_terms  # deterministic tumor-term match recorded


async def test_model_saying_supported_without_specific_papers_is_downgraded():
    judge = Judge(result={
        "papers": [{"pmid": "30000010", "tumor_specific": False}],
        "verdict": "supported", "rationale": "KLK3 is a well known cancer gene.",
    })
    verdict = await gate([RENAL_DIFFERENTIAL], judge).evaluate("KLK3", KICH)
    assert verdict.verdict == "insufficient" and not verdict.supported
    assert "No paper was judged specific" in verdict.rationale


async def test_paper_about_another_tumor_never_counts_even_if_model_marks_it():
    """The model calls a prostate-only paper (never sent: it doesn't mention
    KICH) tumor-specific: it is dropped, so the pair is not supported."""
    judge = Judge(result={
        "papers": [{"pmid": "30000001", "tumor_specific": True},
                   {"pmid": "30000010", "tumor_specific": False}],
        "verdict": "supported", "rationale": "PSA",
    })
    verdict = await gate(PROSTATE_PAPERS + [RENAL_DIFFERENTIAL], judge).evaluate("KLK3", KICH)
    assert verdict.pmids == [] and not verdict.supported
    assert "30000001" not in [p.pmid for p in verdict.papers]


# ------------------------------------------------------------ genuine case


async def test_genuine_case_cites_only_tumor_specific_pmids():
    judge = Judge(result={
        "papers": [
            {"pmid": "30000020", "tumor_specific": True, "reason": "KLK3 mRNA in chRCC tumors."},
            {"pmid": "30000010", "tumor_specific": False, "reason": "Differential only."},
        ],
        "verdict": "supported",
        "rationale": "KLK3 expression is reported in chromophobe RCC (PMID 30000020).",
    })
    records = PROSTATE_PAPERS + [RENAL_DIFFERENTIAL, KICH_PAPER]
    verdict = await gate(records, judge).evaluate("KLK3", KICH)
    assert verdict.supported and verdict.verdict == "supported"
    assert verdict.pmids == ["30000020"]  # only the tumor-specific paper is cited
    assert {p.pmid: p.tumor_specific for p in verdict.papers} == {"30000020": True, "30000010": False}
    assert verdict.oncotree_code == "KICH" and verdict.cancer_type_name == "Kidney Chromophobe"
    assert verdict.literature_gate == TUMOR_SPECIFIC_GATE


async def test_prad_supported_on_prostate_papers():
    judge = Judge(result={
        "papers": [{"pmid": p.pmid, "tumor_specific": p.pmid != "30000004"} for p in PROSTATE_PAPERS],
        "verdict": "supported", "rationale": "KLK3 is highly expressed in prostate cancer.",
    })
    verdict = await gate(PROSTATE_PAPERS, judge).evaluate("KLK3", PRAD)
    assert verdict.supported
    assert verdict.pmids == ["30000001", "30000002", "30000003", "30000005"]


# ------------------------------------------------------- PMID validation


async def test_hallucinated_pmids_are_dropped():
    judge = Judge(result={
        "papers": [
            {"pmid": "99999999", "tumor_specific": True},  # never retrieved
            {"pmid": "PMID:30000020", "tumor_specific": True},  # garbled
            {"pmid": "30000020", "tumor_specific": True},
            {"pmid": "30000020", "tumor_specific": False},  # duplicate: first wins
        ],
        "verdict": "supported", "rationale": "ok",
    })
    verdict = await gate([KICH_PAPER], judge).evaluate("KLK3", KICH)
    assert verdict.pmids == ["30000020"] and verdict.supported
    assert "99999999" not in {p.pmid for p in verdict.papers}


async def test_only_hallucinated_specific_pmid_means_not_supported():
    judge = Judge(result={
        "papers": [{"pmid": "99999999", "tumor_specific": True},
                   {"pmid": "30000010", "tumor_specific": False}],
        "verdict": "supported", "rationale": "see PMID 99999999",
    })
    verdict = await gate([RENAL_DIFFERENTIAL], judge).evaluate("KLK3", KICH)
    assert verdict.pmids == [] and not verdict.supported


async def test_unknown_verdict_value_is_insufficient():
    judge = Judge(result={"papers": [{"pmid": "30000020", "tumor_specific": True}],
                          "verdict": "probably", "rationale": "?"})
    verdict = await gate([KICH_PAPER], judge).evaluate("KLK3", KICH)
    assert verdict.verdict == "insufficient" and not verdict.supported


# ------------------------------------------------------- prompt / errors


async def test_prompt_names_the_tumor_type_and_uses_the_tool():
    judge = Judge(result={"papers": [], "verdict": "insufficient", "rationale": "none"})
    g = gate([KICH_PAPER], judge, abstract_chars=200)
    await g.evaluate("KLK3", KICH)
    call = judge.calls[0]
    assert call["tool"] is JUDGE_TOOL and call["model"] == "judge-model"
    assert call["model_purpose"] == JUDGE_MODEL_PURPOSE
    assert "KICH" in call["user"] and "Kidney Chromophobe" in call["user"]
    assert "chromophobe renal cell carcinoma" in call["user"]
    assert "different tumor type never" in call["system"].replace("\n", " ")


async def test_default_model_is_acgc_selection_model(monkeypatch):
    from src.config import settings as acgc_settings

    monkeypatch.setattr(acgc_settings, "selection_model", "acgc-selection")
    judge = Judge(result={"papers": [], "verdict": "insufficient", "rationale": "none"})
    await TumorSpecificGate(retrieve=Retriever([KICH_PAPER]), judge=judge).evaluate("KLK3", KICH)
    assert judge.calls[0]["model"] == "acgc-selection"


async def test_candidates_are_capped_and_stronger_mentions_first():
    weak = rec("40000001", "Kidney tumors", "Chromophobe kidney tumours and KLK3.")  # words only
    strong = [rec(str(40000010 + i), "x", "KLK3 in chromophobe renal cell carcinoma") for i in range(3)]
    judge = Judge(result={"papers": [], "verdict": "insufficient", "rationale": "none"})
    await gate([weak] + strong, judge, max_papers=3).evaluate("KLK3", KICH)
    sent = [line for line in judge.calls[0]["user"].splitlines() if line.startswith("PMID: ")]
    assert sent == ["PMID: 40000010", "PMID: 40000011", "PMID: 40000012"]


async def test_word_level_match_admits_alias_wordings():
    weak = rec("40000001", "Kidney tumors", "KLK3 in chromophobe kidney tumours.")
    judge = Judge(result={"papers": [{"pmid": "40000001", "tumor_specific": True}],
                          "verdict": "supported", "rationale": "ok"})
    verdict = await gate([weak], judge).evaluate("KLK3", KICH)
    assert verdict.pmids == ["40000001"]


@pytest.mark.parametrize("where", ["retrieve", "judge", "non_dict"])
async def test_failures_raise_gate_errors(where):
    async def broken_retrieve(gene, query):
        raise RuntimeError("ncbi down")

    judge = Judge(exc=RuntimeError("llm down")) if where == "judge" else Judge(result="nope")
    g = TumorSpecificGate(
        retrieve=broken_retrieve if where == "retrieve" else Retriever([KICH_PAPER]), judge=judge,
        model="m",
    )
    with pytest.raises(LiteratureGateError):
        await g.evaluate("KLK3", KICH)


# ------------------------------------------------- through the service


def _scripted_gate():
    """KLK3/PRAD: prostate papers; KLK3/KICH: the same prostate papers plus a
    differential-diagnosis mention — the preview's false positive."""
    async def retrieve(gene, query):
        return PROSTATE_PAPERS + ([RENAL_DIFFERENTIAL] if "chromophobe" in query else [])

    async def judge(**kwargs):
        user = kwargs["user"]
        if "PRAD" in user:
            return {"papers": [{"pmid": p.pmid, "tumor_specific": True} for p in PROSTATE_PAPERS[:3]],
                    "verdict": "supported", "rationale": "KLK3 in prostate cancer."}
        return {"papers": [{"pmid": "30000010", "tumor_specific": False}],
                "verdict": "insufficient", "rationale": "No KICH evidence."}

    return TumorSpecificGate(retrieve=retrieve, judge=judge, model="m")


async def _klk3_service(gate_, store=None):
    bundles = InMemoryBundleStore()
    await seed_bundle(bundles, build_bundle(("KLK3", "VIM"), release_id=M3_RELEASE_ID, subdir=M3))
    return make_service(gate=gate_, bundles=bundles, store=store)


async def test_klk3_kich_not_shown_prad_shown_with_tumor_specific_pmids():
    service = await _klk3_service(_scripted_gate())
    result, evaluated, _, errors = await service.evaluate(await service.resolve("PSA"), None)
    assert errors == [] and evaluated == 2
    assert [p.oncotree_code for p in result.surfaced] == ["PRAD"]
    prad = result.surfaced[0]
    assert prad.literature.pmids == ["30000001", "30000002", "30000003"]
    excluded = {p.oncotree_code: p for p in result.excluded}
    assert excluded["KICH"].exclusion_reason == "literature"
    assert excluded["KICH"].literature.pmids == []

    view = await service.stats_view("KLK3")
    pairs = {p.oncotree_code: p for p in view.pairs}
    assert pairs["PRAD"].status == "surfaced" and pairs["KICH"].status == "excluded"
    assert view.literature_gate == TUMOR_SPECIFIC_GATE


async def test_service_recomputes_a_lying_gate():
    """A gate claiming 'supported' while citing a non-tumor-specific PMID is
    not surfaced (the rule is re-applied by the service)."""
    class Lying:
        name = TUMOR_SPECIFIC_GATE

        async def evaluate(self, gene, tumor):
            return _verdict(
                tumor_type_query=tumor.tumor_type_query, verdict="supported",
                pmids=["30000001"], papers=[PaperJudgement(pmid="30000001", tumor_specific=False)],
            )

    service = await _klk3_service(Lying())
    result, _, _, _ = await service.evaluate(await service.resolve("KLK3"), None)
    assert result.surfaced == []


async def test_service_passes_code_name_and_query_to_the_gate():
    gate_ = FakeLiteratureGate(verdicts={})
    service = await _klk3_service(gate_)
    await service.evaluate(await service.resolve("KLK3"), None)
    assert sorted(gate_.tumors, key=lambda t: t.oncotree_code) == [KICH, PRAD]


# ------------------------------------------------- version bump / cache


def _v1_row(code: str, surfaced: bool = True) -> StoredPair:
    stats = next(ct for ct in gene_file("KLK3", M3)["cancer_types"] if ct["oncotree_code"] == code)
    return StoredPair(
        hugo_symbol="KLK3", oncotree_code=code, release_id=M3_RELEASE_ID,
        literature_gate="acgc_pipeline_v1", surfaced=surfaced, stats=stats,
        evidence={
            "literature_gate": "acgc_pipeline_v1", "tumor_type_query": "x", "supported": True,
            "insufficient_evidence": False, "pmids": ["30000001", "30000002", "30000003"],
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
        },
        computed_at=datetime.now(timezone.utc),
    )


async def test_v1_verdicts_are_not_reused_after_the_version_bump():
    service = await _klk3_service(_scripted_gate())
    # The preview's v1 verdicts: KICH and PRAD both "supported".
    await service.store.upsert_pairs([_v1_row("KICH"), _v1_row("PRAD")])

    # Before re-checking, the stats page doesn't show v1 verdicts at all.
    view = await service.stats_view("KLK3")
    assert {p.oncotree_code: p.status for p in view.pairs if p.literature_eligible} == {
        "PRAD": "literature_pending", "KICH": "literature_pending",
    }
    assert all(p.literature is None for p in view.pairs)

    result, evaluated, reused, _ = await service.evaluate(await service.resolve("KLK3"), None)
    assert evaluated == 2 and reused == 0  # recomputed, not reused
    assert [p.oncotree_code for p in result.surfaced] == ["PRAD"]
    # The v1 rows are left as they were (unreachable under the v2 key).
    assert service.store.rows[("KLK3", "KICH", M3_RELEASE_ID, "acgc_pipeline_v1")].surfaced
    v2 = service.store.rows[("KLK3", "KICH", M3_RELEASE_ID, TUMOR_SPECIFIC_GATE)]
    assert not v2.surfaced and v2.evidence["verdict"] == "insufficient"


async def test_v2_verdicts_are_reused():
    calls = []
    inner = _scripted_gate()

    class Counting:
        name = TUMOR_SPECIFIC_GATE

        async def evaluate(self, gene, tumor):
            calls.append(tumor.oncotree_code)
            return await inner.evaluate(gene, tumor)

    service = await _klk3_service(Counting())
    await service.evaluate(await service.resolve("KLK3"), None)
    _, evaluated, reused, _ = await service.evaluate(await service.resolve("KLK3"), None)
    assert evaluated == 0 and reused == 2 and sorted(calls) == ["KICH", "PRAD"]


def test_stats_api_shows_tumor_specific_citations_only():
    from tests.test_expression_context_api import PREFIX, Harness

    harness = Harness(
        gate=_scripted_gate(),
        bundle=build_bundle(("KLK3",), release_id=M3_RELEASE_ID, subdir=M3),
    )
    from tests.test_expression_context_api import wait_for_job

    with TestClient(harness.app) as client:
        accepted = client.post(f"{PREFIX}/jobs", json={"gene": "KLK3"}).json()
        job = wait_for_job(client, accepted["status_url"])
        assert job["status"] == "succeeded"
        body = client.get(f"{PREFIX}/stats/KLK3").json()
    pairs = {p["oncotree_code"]: p for p in body["pairs"]}
    assert pairs["PRAD"]["status"] == "surfaced"
    assert pairs["PRAD"]["literature"]["pmids"] == ["30000001", "30000002", "30000003"]
    assert pairs["KICH"]["status"] == "excluded"
    assert pairs["KICH"]["exclusion_reason"] == "literature"
    assert pairs["KICH"]["literature"]["pmids"] == []
    assert pairs["KICH"]["literature"]["papers"][0]["tumor_specific"] is False
