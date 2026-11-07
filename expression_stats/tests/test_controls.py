import json
from pathlib import Path

import pytest

from gene_expression_relevance import build as builder
from gene_expression_relevance import controls


@pytest.fixture
def fixture_artifacts(tmp_path):
    rows = json.loads((Path(__file__).parent / "fixtures/controls-results.json").read_text())
    for gene in {r["gene"] for r in rows}:
        (tmp_path / f"{gene}.json").write_text(
            json.dumps(
                dict(
                    cancer_types=[
                        dict(oncotree_code=r["tcga"], stats_pass=r["stats_pass"])
                        for r in rows
                        if r["gene"] == gene
                    ]
                )
            )
        )
    return tmp_path


def test_curated_controls_on_fixture_data(fixture_artifacts):
    report = controls.evaluate(fixture_artifacts)
    controls.require_passed(report)
    assert (report["positive_passed"], report["positive_total"]) == (35, 35)
    assert (report["negative_passed"], report["negative_total"]) == (0, 27)
    borderline = [r for r in report["controls"] if r["classification"] == "borderline"]
    assert len(borderline) == 1 and not borderline[0]["scored"]


@pytest.mark.parametrize(
    "problem", ["positive_miss", "negative_pass", "missing_negative", "subset"]
)
def test_control_gate_fails_closed(fixture_artifacts, problem):
    path = fixture_artifacts / ("ERBB2.json" if problem == "positive_miss" else "GAPDH.json")
    document = json.loads(path.read_text())
    if problem == "missing_negative":
        path.unlink()
    elif problem != "subset":
        document["cancer_types"][0]["stats_pass"] = problem == "negative_pass"
        path.write_text(json.dumps(document))
    report = controls.evaluate(fixture_artifacts, subset=problem == "subset")
    with pytest.raises(ValueError, match="Controls gate"):
        controls.require_passed(report)
    if problem == "subset":
        assert report["status"] == "skipped_subset" and report["failures"] == []
    else:
        assert report["status"] == "failed" and report["failures"]


def test_failed_build_retains_report_and_never_runs_pubmed(tmp_path, monkeypatch):
    # Reuse the real fixture build, substituting a deliberately failing control gate.
    from test_contract import test_build_contract_and_precheck_gating

    monkeypatch.setattr(
        builder,
        "ThreadPoolExecutor",
        lambda *args, **kwargs: pytest.fail("PubMed must not run after a failed gate"),
    )
    report = dict(status="failed", failures=["missing-control"])
    monkeypatch.setattr(builder, "evaluate_controls", lambda *args, **kwargs: report)
    with pytest.raises(ValueError, match="Controls gate failed"):
        test_build_contract_and_precheck_gating(tmp_path, monkeypatch, False, None)
    assert json.loads((tmp_path / "site-failed/controls-report.json").read_text()) == report
    assert not (tmp_path / "site").exists()


@pytest.mark.parametrize("duplicate", ["id", "pair"])
def test_duplicate_controls_are_rejected_even_for_subsets(tmp_path, duplicate):
    first = dict(id="first", gene="ALB", tcga="LIHC", expected=True)
    second = dict(first)
    if duplicate == "id":
        second.update(gene="ERBB2", tcga="BRCA")
    else:
        second["id"] = "second"
    for subset in (False, True):
        with pytest.raises(ValueError, match="Duplicate control"):
            controls.evaluate(tmp_path, subset=subset, panel={"controls": [first, second]})
