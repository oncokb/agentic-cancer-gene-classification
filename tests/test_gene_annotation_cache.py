from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from src.models.schema import FusionInput, GeneAnnotation, OpenEvidenceAnalysis, ResolvedGene
from src.pipeline import orchestrator
from src.pipeline.orchestrator import run_pipeline


async def _no_retracted_pmids(_annotation):
    return set()


async def _retracted_pmid_12345(_annotation):
    return {"12345"}


class FakeGeneStore:
    def __init__(self, cached=None):
        self.cached = cached or {}
        self.saved = []
        self.checked = []

    def _key(self, gene, tumor_type=None):
        normalized_tumor_type = " ".join((tumor_type or "").strip().lower().split())
        return (gene, normalized_tumor_type)

    async def get_gene_annotation(self, gene, tumor_type=None):
        key = self._key(gene, tumor_type)
        if key in self.cached:
            return self.cached[key]
        if key[1] == "":
            return self.cached.get(gene)
        return None

    async def save_gene_annotation(self, annotation, updated_at, tumor_type=None):
        self.saved.append((annotation, updated_at, tumor_type))

    async def mark_gene_pubmed_checked(self, gene, checked_at, annotation=None, tumor_type=None):
        self.checked.append((gene, checked_at, annotation, tumor_type))


def _resolved_gene(gene: str) -> ResolvedGene:
    return ResolvedGene(input_symbol=gene, canonical_symbol=gene, resolved=True)


@pytest.fixture(autouse=True)
def _assume_cached_annotations_are_not_retracted(monkeypatch):
    monkeypatch.setattr(orchestrator, "find_retracted_annotation_pmids", _no_retracted_pmids)


async def test_run_pipeline_reuses_fresh_high_support_cached_annotation(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=10)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fail_if_called(**_kwargs):
        raise AssertionError("fresh cache should avoid recomputation")

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].gene == "BRAF"
    assert result.annotations[0].fusions == ["TP53::BRAF"]
    assert result.annotations[0].cache_status == "reused"
    assert result.annotations[0].cache_reason == "fresh_high_evidence_support"
    assert store.saved == []


async def test_run_pipeline_refreshes_cached_annotation_with_retracted_pmid(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=10)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=False,
            cancer_associated=True,
            citations=["67890"],
            insufficient_evidence=False,
            evidence_support_score=0.7,
            evidence_support_explanation="Refreshed without retracted citations.",
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "find_retracted_annotation_pmids", _retracted_pmid_12345)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].citations == ["67890"]
    assert store.saved[0][0].gene == "BRAF"


async def test_run_pipeline_reuses_stale_cache_when_pubmed_has_no_new_pmids(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=80)
    checked_at = datetime.now(timezone.utc) - timedelta(days=40)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": checked_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_recent_pmids(*_args, **_kwargs):
        return []

    async def fail_if_called(**_kwargs):
        raise AssertionError("clean PubMed freshness check should avoid recomputation")

    monkeypatch.setattr(orchestrator.settings, "gene_cache_final_annotation_days", 0)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "search_recent_pubmed_pmids", fake_recent_pmids)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "reused"
    assert result.annotations[0].cache_reason == "no_new_pubmed_pmids_since_last_check"
    assert store.checked[0][0] == "BRAF"


async def test_run_pipeline_refreshes_stale_cache_when_pubmed_has_new_pmids(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=80)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_recent_pmids(*_args, **_kwargs):
        return ["99999999"]

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=False,
            cancer_associated=True,
            citations=["99999999"],
            insufficient_evidence=False,
            evidence_support_score=0.6,
            evidence_support_explanation="Medium support.",
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator.settings, "gene_cache_final_annotation_days", 0)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "search_recent_pubmed_pmids", fake_recent_pmids)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].cache_reason == "cache_miss_or_stale"
    assert store.saved[0][0].gene == "BRAF"


async def test_run_pipeline_does_not_reuse_cache_for_different_tumor_type(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=10)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support for melanoma.",
    )
    store = FakeGeneStore(
        {
            ("BRAF", "melanoma"): {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=False,
            cancer_associated=True,
            citations=["99999999"],
            insufficient_evidence=False,
            evidence_support_score=0.7,
            evidence_support_explanation="Tumor-specific support for LUAD.",
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(
        [FusionInput(fusion="TP53::BRAF", tumor_type="LUAD")],
        run_store=store,
    )

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].evidence_support_explanation == "Tumor-specific support for LUAD."
    assert store.saved[0][0].gene == "BRAF"
    assert store.saved[0][2] == "LUAD"


async def test_run_pipeline_reuses_final_annotation_before_freshness_probe(monkeypatch):
    updated_at = datetime.now(timezone.utc) - timedelta(days=120)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345"],
        insufficient_evidence=False,
        evidence_support_score=0.6,
        evidence_support_explanation="Medium support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fail_recent_pmids(*_args, **_kwargs):
        raise AssertionError("final annotation cache should avoid PubMed freshness probe")

    async def fail_if_called(**_kwargs):
        raise AssertionError("final annotation cache should avoid recomputation")

    monkeypatch.setattr(orchestrator.settings, "gene_cache_final_annotation_days", 180)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "search_recent_pubmed_pmids", fail_recent_pmids)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "reused"
    assert result.annotations[0].cache_reason == "fresh_final_annotation"
    assert store.checked == []
    assert store.saved == []


async def test_run_pipeline_force_refresh_bypasses_cache(monkeypatch):
    updated_at = datetime.now(timezone.utc)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=True,
        evidence_support_score=1.0,
        evidence_support_explanation="OncoKB cached.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=True,
            evidence_support_score=0.8,
            evidence_support_explanation="Refreshed.",
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(["TP53::BRAF"], run_store=store, force_refresh=True)

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].cache_reason == "force_refresh"
    assert store.saved[0][0].gene == "BRAF"


# ---------------------------------------------------------------------------
# OpenEvidence-freshness staleness check: analogous to the PubMed-freshness
# check above, but for OpenEvidence supplementary evidence. If OpenEvidence
# data lands in cache AFTER a gene was already annotated (e.g. via
# benchmarks/warm_openevidence_cache.py, or a slow live call that finished
# after synthesis had already proceeded without it), the next read of that
# gene should pick up the more pertinent, OpenEvidence-informed annotation
# rather than serving the stale one — even if the cached annotation is
# otherwise still well within its normal age-based freshness window.
# ---------------------------------------------------------------------------


async def test_run_pipeline_refreshes_cached_annotation_when_openevidence_becomes_available(monkeypatch):
    """A cached annotation with no openevidence_supplementary, well within
    its normal freshness window (10 days old, high support), must still be
    refreshed when OPENEVIDENCE_ENABLED=true and an OpenEvidence cache entry
    has newly appeared for this gene — proving the check fires independent
    of and ahead of the ordinary age-based freshness windows."""
    updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    assert cached_annotation.openevidence_supplementary is None
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_has_cached_analysis(gene, tumor_type=None):
        assert gene == "BRAF"
        return True

    async def fake_annotate_gene(**kwargs):
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            in_oncokb=False,
            cancer_associated=True,
            citations=["12345", "67890", "24680"],
            insufficient_evidence=False,
            evidence_support_score=0.9,
            evidence_support_explanation="High support, now OpenEvidence-informed.",
            openevidence_supplementary=OpenEvidenceAnalysis(
                question="q", text="OpenEvidence-informed synthesis text."
            ),
            cache_status="refreshed",
        )

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "has_cached_analysis", fake_has_cached_analysis)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "refreshed"
    assert result.annotations[0].openevidence_supplementary is not None
    assert store.saved[0][0].gene == "BRAF"


async def test_run_pipeline_reuses_cache_when_no_new_openevidence_data_available(monkeypatch):
    """No OpenEvidence cache entry exists yet — nothing changed, so the
    freshness check must NOT fire: the cache is reused normally, exactly as
    it would be without the OpenEvidence check existing at all."""
    updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345", "67890", "24680"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_has_cached_analysis(gene, tumor_type=None):
        return False

    async def fail_if_called(**_kwargs):
        raise AssertionError("no new OpenEvidence data should avoid recomputation")

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "has_cached_analysis", fake_has_cached_analysis)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "reused"
    assert result.annotations[0].cache_reason == "fresh_high_evidence_support"
    assert store.saved == []


async def test_run_pipeline_does_not_recheck_openevidence_already_present_on_cached_annotation(monkeypatch):
    """The cached annotation already carries OpenEvidence supplementary
    evidence — there's nothing new to pick up, so the freshness check must
    not even bother peeking the cache (and certainly must not trigger
    recomputation)."""
    updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support, already OpenEvidence-informed.",
        openevidence_supplementary=OpenEvidenceAnalysis(question="q", text="Already have this."),
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fail_has_cached_analysis(gene, tumor_type=None):
        raise AssertionError(
            "should not peek the OpenEvidence cache when the annotation already has supplementary evidence"
        )

    async def fail_if_called(**_kwargs):
        raise AssertionError("already-present OpenEvidence evidence should avoid recomputation")

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "has_cached_analysis", fail_has_cached_analysis)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "reused"
    assert store.saved == []


async def test_run_pipeline_never_checks_openevidence_freshness_when_feature_disabled(monkeypatch):
    """OPENEVIDENCE_ENABLED=false must add zero overhead to the reuse check:
    it must not even call has_cached_analysis, let alone treat anything as
    stale because of it."""
    updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fail_has_cached_analysis(gene, tumor_type=None):
        raise AssertionError("should not check OpenEvidence freshness when the feature is disabled")

    async def fail_if_called(**_kwargs):
        raise AssertionError("fresh cache should avoid recomputation")

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", False)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "has_cached_analysis", fail_has_cached_analysis)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fail_if_called)

    result = await run_pipeline(["TP53::BRAF"], run_store=store)

    assert result.annotations[0].cache_status == "reused"
    assert store.saved == []


# ---------------------------------------------------------------------------
# Blocking-bug fix: a repeated-refresh loop when a cache entry exists but the
# refresh attempt keeps failing to actually persist supplementary evidence
# (e.g. a downstream synthesis error unrelated to OpenEvidence, which skips
# persisting the refreshed annotation entirely — see annotate_one). Without
# a cooldown, the SAME stale cached annotation would re-trigger the exact
# same freshness-driven refresh attempt on every single subsequent read,
# forever, regardless of whether OpenEvidence itself is fine.
# ---------------------------------------------------------------------------


async def test_run_pipeline_does_not_repeat_openevidence_refresh_when_downstream_synthesis_keeps_failing(
    monkeypatch,
):
    """Repeated-read reproduction of the bug: OpenEvidence data genuinely
    exists in cache, but the downstream synthesis step fails for an
    unrelated reason, so the refreshed annotation is never persisted. A
    second read of the same gene must NOT re-trigger another
    freshness-driven refresh attempt — the cooldown must stop it."""
    updated_at = datetime.now(timezone.utc) - timedelta(days=1)
    cached_annotation = GeneAnnotation(
        gene="BRAF",
        fusions=["OLD::BRAF"],
        in_oncokb=False,
        cancer_associated=True,
        citations=["12345"],
        insufficient_evidence=False,
        evidence_support_score=0.9,
        evidence_support_explanation="High support.",
    )
    assert cached_annotation.openevidence_supplementary is None
    store = FakeGeneStore(
        {
            "BRAF": {
                "annotation": cached_annotation.model_dump(),
                "updated_at": updated_at,
                "last_pubmed_checked_at": updated_at,
            }
        }
    )

    async def fake_normalize_fusions(_fusions):
        return {"BRAF": (_resolved_gene("BRAF"), ["TP53::BRAF"])}

    async def fake_has_cached_analysis(gene, tumor_type=None):
        return True  # OpenEvidence data genuinely exists in cache

    attempted = set()

    async def fake_was_refresh_recently_attempted(gene, tumor_type=None):
        return (gene, tumor_type) in attempted

    async def fake_mark_refresh_attempted(gene, tumor_type=None):
        attempted.add((gene, tumor_type))

    annotate_gene_calls = []

    async def fake_annotate_gene(**kwargs):
        annotate_gene_calls.append(kwargs["gene"])
        # _annotate_gene itself catches synthesis exceptions and returns an
        # error-bearing annotation rather than raising (see orchestrator.py)
        # — annotate_one then skips persistence entirely for annotations
        # with a non-None error.
        return GeneAnnotation(
            gene=kwargs["gene"],
            fusions=kwargs["fusions"],
            insufficient_evidence=True,
            evidence_support_score=0.0,
            evidence_support_explanation="Synthesis failed.",
            error="Synthesis error: simulated LLM failure",
        )

    monkeypatch.setattr(orchestrator.settings, "openevidence_enabled", True)
    monkeypatch.setattr(orchestrator, "normalize_fusions", fake_normalize_fusions)
    monkeypatch.setattr(orchestrator, "has_cached_analysis", fake_has_cached_analysis)
    monkeypatch.setattr(
        orchestrator, "was_refresh_recently_attempted", fake_was_refresh_recently_attempted
    )
    monkeypatch.setattr(orchestrator, "mark_refresh_attempted", fake_mark_refresh_attempted)
    monkeypatch.setattr(orchestrator, "_annotate_gene", fake_annotate_gene)

    # First "read": the freshness check fires (nothing attempted yet),
    # _annotate_gene runs but synthesis fails, so persistence is skipped —
    # the stale cached annotation remains exactly as it was.
    first_result = await run_pipeline(["TP53::BRAF"], run_store=store)
    assert first_result.annotations[0].error is not None
    assert len(annotate_gene_calls) == 1
    assert store.saved == []

    # Second "read" of the SAME gene: without the cooldown, the freshness
    # check would fire again (the cached annotation still lacks
    # openevidence_supplementary, and has_cached_analysis is still True) and
    # re-trigger _annotate_gene. With the cooldown, it must not — the stale
    # annotation is instead reused normally via its ordinary freshness window.
    second_result = await run_pipeline(["TP53::BRAF"], run_store=store)
    assert len(annotate_gene_calls) == 1  # no second attempt
    assert second_result.annotations[0].cache_status == "reused"
