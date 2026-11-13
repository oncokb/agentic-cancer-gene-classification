"""R2 boundaries, sample identity, global BH and demotion-only behavior."""

import json

import numpy as np
import pytest

from gene_expression_relevance import PARAMS, purity


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "demoted"),
        ({"rho": -0.299999}, "pass"),
        ({"q": 0.05}, "pass"),
        ({"n": 29}, "unassessed"),
        ({"hp_n": 29}, "unassessed"),
        ({"hp_strong": True}, "pass"),
        ({"rho": None}, "unassessed"),
        *[({"code": code, "n": 0}, "not_applicable") for code in ["DLBC", "LAML", "THYM"]],
    ],
)
def test_r2_boundaries(changes, expected):
    args = dict(code="BRCA", rho=-0.30, q=0.049999, n=30, hp_n=30, hp_strong=False)
    assert purity.check(**(args | changes)) == expected


def test_purity_threshold_inclusive():
    assert purity.high_purity_mask(np.array([np.nan, 0.599999, 0.6, 1])).tolist() == [
        False,
        False,
        True,
        True,
    ]


@pytest.mark.parametrize("cached", [True, False])
def test_checksum_mismatch_fails_closed(tmp_path, monkeypatch, cached):
    target = tmp_path / "objects" / purity.SOURCE["sha256"]
    target.parent.mkdir()
    if cached:
        target.write_bytes(b"corrupt")
    monkeypatch.setattr(purity.http, "download", lambda url, path: path.write_bytes(b"corrupt"))
    with pytest.raises(ValueError, match="purity checksum mismatch"):
        purity.fetch(tmp_path)
    assert not target.exists()


def test_verified_cache_and_sample_join(tmp_path, monkeypatch):
    target = tmp_path / "objects" / purity.SOURCE["sha256"]
    target.parent.mkdir()
    target.write_bytes(b"verified")
    monkeypatch.setattr(purity, "digest", lambda path: purity.SOURCE["sha256"])
    assert purity.fetch(tmp_path) == target
    path = tmp_path / "purity.tsv"
    path.write_text(
        "sample\tcall status\tpurity\n"
        "TCGA-AA-0001-01A\tcalled\t0.6\n"
        "TCGA-AA-0001-02A\tcalled\t0.9\n"
        "TCGA-AA-0002-01A\tuncalled\t0.8\n"
        "TCGA-AA-0003-01A\tcalled\t1.1\n"
        "TCGA-AA-0004-01A\tcalled\tinf\n"
    )
    values = purity.load(path, [f"TCGA-AA-{i:04d}-01B" for i in range(1, 5)])
    assert values[0] == 0.6 and np.isnan(values[1:]).all()


def test_finalize_global_family_and_only_tier_mutates(tmp_path):
    records = [
        dict(oncotree_code=c, confidence_tier=t, stats_pass=True, log2fc=3)
        for c, t in [("BRCA", "high"), ("CHOL", "high"), ("DLBC", "high"), ("LGG", "candidate")]
    ]
    path = tmp_path / "G.json"
    doc = dict(gene=dict(hugo_symbol="G"), cancer_types=records)
    path.write_text(json.dumps(doc))
    tests = {
        ("G", c): dict(rho=-0.3, p=0.01, n=30, hp_n=30, hp_strong=False)
        for c in ["BRCA", "CHOL", "DLBC", "LGG"]
    }
    tests["G", "CHOL"]["hp_n"] = 29
    tests["G", "LGG"]["p"] = None  # unavailable still contributes p=1 to family
    counts = purity.finalize(tmp_path, tests)
    assert counts == dict(high_before=3, high_after=2, demoted=1)
    result = json.loads(path.read_text())["cancer_types"]
    assert [r["purity_check"] for r in result] == ["demoted", "unassessed", "not_applicable", None]
    assert result[0]["confidence_tier"] == "candidate"
    assert result[0]["purity_q"] == pytest.approx(0.04 / 3)
    for before, after in zip(records, result, strict=True):
        assert {
            k: v for k, v in after.items() if not k.startswith("purity_") and k != "confidence_tier"
        } == {k: v for k, v in before.items() if k != "confidence_tier"}
        if after["purity_check"] != "demoted":
            assert before["confidence_tier"] == after["confidence_tier"]


def test_frozen_broad_and_lineage_priority(monkeypatch):
    raw = np.ones((1, 60))
    labels = np.array(["A"] * 30 + ["B"] * 30)
    original = dict(
        confidence_tier=np.array([["high", "candidate"]]),
        stats_pass=np.array([[True, True]]),
        broadly_expressed=np.array([False]),
    )
    hp = {
        key: np.full((1, 2), value)
        for key, value in dict(
            n=30,
            n_rest=30,
            q_value=0.001,
            log2fc=1.5,
            cliffs_delta=0.8,
            median_log2=8,
            subgroup_high_count=10,
            subgroup_high_fraction=0.1,
            subgroup_high_fraction_ratio=6,
            p95_log2_diff=2,
            subgroup_q=0.001,
        ).items()
    }
    monkeypatch.setattr(purity, "compute", lambda *args: hp)
    result = purity.measure(raw, labels, ["A", "B"], original, np.full(60, 0.6))
    # Lineage passes v3 but fails its strong FC: subgroup cannot rescue the high tier.
    assert result[0, 0]["hp_strong"] is False
    original["broadly_expressed"][:] = True
    result = purity.measure(raw, labels, ["A", "B"], original, np.full(60, 0.6))
    assert result[0, 0]["hp_strong"] is True
    assert PARAMS["purity_min_target"] == 30


@pytest.mark.parametrize("key", [key for key in PARAMS if key.startswith("purity_")])
def test_every_purity_parameter_changes_release_identity(key):
    from gene_expression_relevance.provenance import build_fingerprint

    def identity(params):
        return build_fingerprint(params, None, True, "hgnc", "code", "mapping")

    changed = dict(PARAMS)
    changed[key] = ["OTHER"] if isinstance(PARAMS[key], list) else PARAMS[key] + 0.01
    assert identity(changed) != identity(PARAMS)


@pytest.mark.parametrize("key", ["url", "sha256"])
def test_purity_source_pin_changes_release_identity(monkeypatch, key):
    from gene_expression_relevance.provenance import build_fingerprint

    before = build_fingerprint(PARAMS, None, True, "hgnc", "code", "mapping")
    monkeypatch.setitem(purity.SOURCE, key, purity.SOURCE[key] + "changed")
    assert build_fingerprint(PARAMS, None, True, "hgnc", "code", "mapping") != before
