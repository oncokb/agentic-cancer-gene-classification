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
    assert (report["positive_passed"], report["positive_total"]) == (51, 51)
    assert (report["negative_passed"], report["negative_total"]) == (0, 27)
    borderline = [r for r in report["controls"] if r["classification"] == "borderline"]
    assert len(borderline) == 1 and not borderline[0]["scored"]
    adc = [r for r in report["controls"] if r["classification"] == "D"]
    assert len(adc) == 16 and all(r["expected"] is True and r["matched"] for r in adc)
    # Candidate misses are measured (both fail the RNA rule) but never gated or counted.
    candidates = report["candidate_misses"]
    assert candidates["gating"] is False and candidates["total"] == 2
    assert [(r["id"], r["observed"], r["scored"]) for r in candidates["controls"]] == [
        ("M-F3-CESC", False, False),
        ("M-MET-LUAD", False, False),
    ]
    assert not {"M-F3-CESC", "M-MET-LUAD"} & {r["id"] for r in report["controls"]}
    assert report["failures"] == []


def test_candidate_misses_never_gate(fixture_artifacts):
    # A recovered or missing candidate miss changes neither status nor any counter.
    baseline = controls.evaluate(fixture_artifacts)
    (fixture_artifacts / "F3.json").unlink()
    document = json.loads((fixture_artifacts / "MET.json").read_text())
    for record in document["cancer_types"]:
        record["stats_pass"] = True
    (fixture_artifacts / "MET.json").write_text(json.dumps(document))
    report = controls.evaluate(fixture_artifacts)
    assert report["status"] == "passed" and report["failures"] == []
    keys = ("positive_passed", "positive_total", "negative_passed", "negative_total", "missing")
    assert {k: report[k] for k in keys} == {k: baseline[k] for k in keys}
    candidates = report["candidate_misses"]
    assert (candidates["recovered"], candidates["missing"]) == (1, 1)


@pytest.mark.parametrize("clash", ["id", "pair", "expected"])
def test_candidate_misses_cannot_duplicate_or_gate(clash):
    panel = json.loads(controls.PANEL_PATH.read_text())
    miss = dict(panel["expression_candidate_misses"][0])
    if clash == "id":
        miss.update(id=panel["controls"][0]["id"], gene="ZZZ", tcga="ACC")
    elif clash == "pair":
        miss.update(
            id="M-new", gene=panel["controls"][0]["gene"], tcga=panel["controls"][0]["tcga"]
        )
    else:
        miss.update(id="M-new", tcga="ACC", expected=False)
    panel["expression_candidate_misses"].append(miss)
    with pytest.raises(ValueError, match="Duplicate|never gated"):
        controls.expected_counts(panel)


def test_panel_counts_and_adc_sources():
    panel = json.loads(controls.PANEL_PATH.read_text())
    assert controls.expected_counts(panel) == (51, 27)
    adc = [r for r in panel["controls"] if r["classification"] == "D"]
    assert {(r["gene"], r["tcga"]) for r in adc} == {
        ("NECTIN4", "BLCA"), ("CD19", "DLBC"), ("CD79B", "DLBC"), ("TNFRSF8", "DLBC"),
        ("CEACAM5", "COADREAD"), ("MSLN", "MESO"), ("MSLN", "OV"), ("MSLN", "PAAD"),
        ("GPC3", "LIHC"), ("CA9", "KIRC"), ("CLDN6", "TGCT"), ("CLDN6", "OV"),
        ("KLK2", "PRAD"), ("STEAP1", "PRAD"), ("SLC34A2", "OV"), ("SLC34A2", "LUAD"),
    }  # fmt: skip
    assert all(r["source"] and "ADC/I-O" in r["note"] for r in adc)
    free = {r["gene"] for r in adc if "biomarker-free" in r["note"]}
    assert free == {"NECTIN4", "CD19", "CD79B", "TNFRSF8"}


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
