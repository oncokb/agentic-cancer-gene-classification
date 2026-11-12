"""Alteration-type statistics: classifiers, denominators, statuses and detail-only guarantees."""

import json
from pathlib import Path

import jsonschema
import numpy as np
import pytest
from alteration_fixtures import maf
from scipy.stats import fisher_exact

from gene_expression_relevance import (
    ALTERATION_CATEGORIES,
    ALTERATION_PARAMS,
    COPY_NUMBER_ALTERATION,
    POINT_MUTATION,
    STRUCTURAL_VARIANT,
    alterations,
    controls,
    drivers,
)
from gene_expression_relevance import build as builder
from gene_expression_relevance.stats import bh

SCHEMAS = json.loads((Path(__file__).parent.parent / "docs/artifact-schema.json").read_text())
DRIVER = drivers.DriverSource(
    name="test",
    sha256="0" * 64,
    genes=frozenset({"KRAS", "TP53", "EGFR", "ROS1", "ERBB2"}),
    tumor_suppressors=frozenset({"TP53"}),
)
# Every fixture gene is a copy-number driver with no role: both directions callable.
CNA_DRIVER = drivers.CnaDriverSource(
    name="test-cna",
    sha256="1" * 64,
    genes=frozenset({"KRAS", "TP53", "EGFR", "ROS1", "ERBB2", "TTN"}),
    oncogenes=frozenset(),
    tumor_suppressors=frozenset(),
)
CATALOG = {3845: "KRAS", 7157: "TP53", 1956: "EGFR", 6098: "ROS1", 2064: "ERBB2", 7273: "TTN"}
IDS = sorted(CATALOG)
ROW = {symbol: IDS.index(entrez) for entrez, symbol in CATALOG.items()}


def sample(cohort, i, kind="01"):
    return f"TCGA-{cohort}-{i:04d}-{kind}"


# ---------------------------------------------------------------- point mutation rule


def records(rows):
    """rows: (gene, sample, classification, transcript, position, hgvsp)."""
    columns = list(zip(*rows, strict=True)) if rows else [[]] * 6
    return dict(
        row=np.array([ROW[g] for g in columns[0]], dtype=np.int64),
        sample=np.array(columns[1], dtype=str),
        cls=np.array([alterations.CLASS[c] for c in columns[2]], dtype=np.int8),
        transcript=np.array(columns[3], dtype=str),
        position=np.array(columns[4], dtype=np.int32),
        hgvsp=np.array(columns[5], dtype=str),
    )


def driver_rows():
    return {ROW[g] for g in DRIVER.genes}, {ROW[g] for g in DRIVER.tumor_suppressors}


def test_missense_counts_only_at_positions_recurrent_in_ten_tumors():
    hot = [("KRAS", sample("AA", i), "Missense_Mutation", "T1", 12, "") for i in range(10)]
    cold = [("KRAS", sample("BB", i), "Missense_Mutation", "T1", 61, "") for i in range(9)]
    retained = {r[1] for r in hot + cold}
    events = alterations.point_mutation_events(records(hot + cold), retained, *driver_rows())
    assert events == {(ROW["KRAS"], r[1]) for r in hot}


def test_recurrence_is_per_transcript_and_counts_tumors_not_rows():
    rows = [("KRAS", sample("AA", 0), "Missense_Mutation", "T1", 12, "")] * 10
    rows += [("KRAS", sample("AA", i), "Missense_Mutation", "T2", 12, "") for i in range(1, 6)]
    rows += [("KRAS", sample("AA", i), "Missense_Mutation", "T1", 12, "") for i in range(6, 10)]
    retained = {r[1] for r in rows}
    assert alterations.point_mutation_events(records(rows), retained, *driver_rows()) == set()


def test_truncating_counts_in_tumor_suppressors_but_not_oncogenes():
    rows = [
        ("TP53", sample("AA", 0), "Nonsense_Mutation", "T", 196, ""),
        ("TP53", sample("AA", 1), "Frame_Shift_Del", "T", 100, ""),
        ("TP53", sample("AA", 2), "Splice_Site", "T", -1, ""),
        ("KRAS", sample("AA", 3), "Nonsense_Mutation", "T", 5, ""),
        ("KRAS", sample("AA", 4), "Frame_Shift_Ins", "T", 9, ""),
    ]
    retained = {r[1] for r in rows}
    events = alterations.point_mutation_events(records(rows), retained, *driver_rows())
    assert events == {(ROW["TP53"], sample("AA", i)) for i in range(3)}


def test_recurrent_in_frame_indels_count_and_rare_ones_do_not():
    change = "p.E746_A750del"
    rows = [("EGFR", sample("AA", i), "In_Frame_Del", "T", 746, change) for i in range(5)]
    rows += [
        ("EGFR", sample("BB", i), "In_Frame_Ins", "T", 770, "p.D770_N771insG") for i in range(4)
    ]
    retained = {r[1] for r in rows}
    events = alterations.point_mutation_events(records(rows), retained, *driver_rows())
    assert events == {(ROW["EGFR"], sample("AA", i)) for i in range(5)}


def test_hypermutators_are_excluded_from_events_and_recurrence():
    rows = [("KRAS", sample("AA", i), "Missense_Mutation", "T", 12, "") for i in range(10)]
    retained = {r[1] for r in rows} - {sample("AA", 0)}
    # Without the hypermutated tumor the position recurs in only 9 retained tumors.
    assert alterations.point_mutation_events(records(rows), retained, *driver_rows()) == set()


def test_genes_outside_the_driver_list_never_count():
    rows = [("TTN", sample("AA", i), "Missense_Mutation", "T", 5, "") for i in range(20)]
    retained = {r[1] for r in rows}
    assert alterations.point_mutation_events(records(rows), retained, *driver_rows()) == set()


# ---------------------------------------------------------------- parsers


def test_parsers_read_datahub_formats(tmp_path):
    path = tmp_path / "data_mutations.txt"
    path.write_text(
        maf(
            [
                ("KRAS", 3845, "Missense_Mutation", "TCGA-AA-0001-01A-11D", "T1", "p.G12D"),
                ("EGFR", 1956, "In_Frame_Del", "TCGA-AA-0002-01", "T2", "p.E746_A750del"),
                ("TTN", 7273, "Silent", "TCGA-AA-0001-01", "T3", "p.A5A"),
                ("TP53", 7157, "Splice_Site", "TCGA-AA-0003-01", "T4", ""),
            ]
        )
    )
    data = alterations.parse_mutations(path)
    assert data["sample_table"][data["sample"]].tolist() == [
        "TCGA-AA-0001-01",
        "TCGA-AA-0002-01",
        "TCGA-AA-0003-01",
    ]
    # Positions serve the missense rule only; in-frame indels match on the full HGVSp.
    assert data["position"].tolist() == [12, -1, -1]
    assert data["hgvsp_table"][data["hgvsp"]].tolist() == ["", "p.E746_A750del", ""]

    cna = tmp_path / "data_cna.txt"
    cna.write_text(
        "Hugo_Symbol\tEntrez_Gene_Id\tTCGA-AA-0001-01\tTCGA-AA-0002-01\n"
        "ERBB2\t2064\t2\tNA\nTP53\t7157\t-2\t0\n"
    )
    parsed = alterations.parse_cna(cna)
    assert parsed["calls"].tolist() == [[2, alterations.CNA_MISSING], [-2, 0]]

    sv = tmp_path / "data_sv.txt"
    sv.write_text(
        "Sample_Id\tSite1_Hugo_Symbol\tSite2_Hugo_Symbol\tSV_Status\n"
        "TCGA-AA-0001-01\tCD74\tROS1\tSOMATIC\nTCGA-AA-0002-01\tEML4\tALK\tGERMLINE\n"
    )
    parsed = alterations.parse_sv(sv)
    assert parsed["site2"].tolist() == ["ROS1"]
    lists = "cancer_study_identifier: x\ncase_list_ids: TCGA-AA-0001-01\tTCGA-AA-0002-01\n"
    assert alterations.parse_case_list(lists) == {"TCGA-AA-0001-01", "TCGA-AA-0002-01"}


def test_summaries_are_cached_and_raw_downloads_deleted(tmp_path, monkeypatch):
    raw = tmp_path / "objects" / "oid"
    calls = []

    def fetch_lfs(*args):
        calls.append(args)
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("Sample_Id\tSite1_Hugo_Symbol\tSite2_Hugo_Symbol\nTCGA-AA-0001-01\tA\tB\n")
        return raw

    monkeypatch.setattr(alterations.fetching, "locate", lambda c, p: ("f" * 64, 10, None))
    monkeypatch.setattr(alterations.fetching, "fetch_lfs", fetch_lfs)
    first = alterations.summary("a" * 40, tmp_path, "s", "sv", delete_downloads=True)
    assert not raw.exists()
    second = alterations.summary("a" * 40, tmp_path, "s", "sv", delete_downloads=True)
    assert len(calls) == 1 and first[0] == second[0]
    assert second[1]["site2"].tolist() == ["B"]


# ---------------------------------------------------------------- end-to-end compute


def synthetic(tmp_path):
    """Cohorts AA (30 selected), BB (60) and SKCM (25, CNA metastatic-only)."""
    cohorts = {"AA": 30, "BB": 60, "SKCM": 25}
    selection = {c: [sample(c[:2], i) for i in range(n)] for c, n in cohorts.items()}
    inputs = {}
    for code, selected in selection.items():
        rows = []
        if code == "AA":
            rows += [("KRAS", 3845, "Missense_Mutation", s, "T", "p.G12D") for s in selected[:12]]
            # One hypermutated tumor (501 nonsynonymous rows), which also carries G12D.
            rows += [("FILLER", 1, "Missense_Mutation", selected[0], "F", "p.A1T")] * 501
        if code == "BB":
            rows += [("KRAS", 3845, "Missense_Mutation", selected[0], "T", "p.G12D")]
            rows += [("TTN", 7273, "Missense_Mutation", s, "T", "p.A5T") for s in selected]
        mutation_path = tmp_path / f"{code}_mut.txt"
        mutation_path.write_text(maf(rows))
        mutation_cases = set(selected[:25]) if code == "AA" else set(selected)

        if code == "SKCM":
            cna_samples = [sample("SK", i, "06") for i in range(40)]
            calls = np.zeros((2, 40), dtype=int)
        else:
            cna_samples = selected
            calls = np.zeros((2, len(selected)), dtype=int)
            if code == "AA":
                calls[0, :10] = 2  # ERBB2 amplification
            else:
                calls[1, :15] = -2  # TP53 deep deletion
        cna_path = tmp_path / f"{code}_cna.txt"
        cna_path.write_text(
            "Hugo_Symbol\tEntrez_Gene_Id\t"
            + "\t".join(cna_samples)
            + "\n"
            + "\n".join(
                f"{symbol}\t{entrez}\t" + "\t".join(map(str, row))
                for (symbol, entrez), row in zip(
                    [("ERBB2", 2064), ("TP53", 7157)], calls, strict=True
                )
            )
            + "\n"
        )
        sv_path = tmp_path / f"{code}_sv.txt"
        fusions = [("CD74", "ROS1", s) for s in selected[:2]] if code == "AA" else []
        sv_path.write_text(
            "Sample_Id\tSite1_Hugo_Symbol\tSite2_Hugo_Symbol\n"
            + "".join(f"{s}\t{a}\t{b}\n" for a, b, s in fusions)
        )
        inputs[code] = dict(
            mutation=dict(data=alterations.parse_mutations(mutation_path), cases=mutation_cases),
            cna=dict(data=alterations.parse_cna(cna_path), cases=set(cna_samples)),
            sv=dict(data=alterations.parse_sv(sv_path), cases=set(selected)),
        )
    mapping = [dict(study_id=c, oncotree_code=c) for c in cohorts]
    return inputs, mapping, selection


def low_threshold_params():
    return {**ALTERATION_PARAMS, "missense_min_recurrence": 10}


@pytest.fixture
def result(tmp_path):
    inputs, mapping, selection = synthetic(tmp_path)
    return alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {}, DRIVER, cna_driver=CNA_DRIVER
    )


def category(result, gene, code, name):
    return result.record(ROW[gene], result.codes.index(code))["categories"][name]


def test_profiled_denominators_and_hypermutator_exclusion(result):
    kras = category(result, "KRAS", "AA", POINT_MUTATION)
    # 30 selected, 25 sequenced, one hypermutator excluded: 24 retained profiled patients.
    assert kras["n_profiled"] == 24
    assert result.summary["cohorts"]["AA"][POINT_MUTATION] == dict(
        selected=30, profiled=25, hypermutators_excluded=1, n_profiled=24
    )
    # G12D recurs in 11 retained tumors (AA 1-11 plus BB 0); AA 0 is excluded.
    assert kras["n_events"] == 11 and kras["rest_n_events"] == 1
    assert kras["status"] == "enriched"
    table = [[11, 24 - 11], [1, 60 + 25 - 1]]
    assert kras["p_value"] == pytest.approx(fisher_exact(table, alternative="greater")[1])
    assert category(result, "KRAS", "BB", POINT_MUTATION)["status"] == "not_enriched"


def test_non_driver_genes_are_not_testable_with_reason(result):
    ttn = category(result, "TTN", "BB", POINT_MUTATION)
    assert ttn["status"] == "not_testable"
    assert ttn["reason"] == "gene not in mutation driver list (test)"
    assert ttn["n_events"] is None and ttn["q_value"] is None


def test_cna_amplification_and_deletion_are_tested_separately(result):
    erbb2 = category(result, "ERBB2", "AA", COPY_NUMBER_ALTERATION)
    assert erbb2["status"] == "enriched" and erbb2["direction"] == "amp"
    assert erbb2["enriched_directions"] == ["amp"]
    assert (erbb2["n_events"], erbb2["n_profiled"]) == (10, 30)
    assert erbb2["amp"]["enriched"] and not erbb2["del"]["enriched"]
    tp53 = category(result, "TP53", "BB", COPY_NUMBER_ALTERATION)
    assert tp53["status"] == "enriched" and tp53["direction"] == "del"
    assert tp53["del"]["n_events"] == 15 and tp53["amp"]["n_events"] == 0
    assert category(result, "TP53", "AA", COPY_NUMBER_ALTERATION)["status"] == "not_enriched"


def test_duplicate_gistic_columns_count_one_patient_once(tmp_path):
    inputs, mapping, selection = synthetic(tmp_path)
    data = inputs["AA"]["cna"]["data"]
    first = selection["AA"][0]
    # Repeat patient AA-0000's amplified column, plus a column for another sample (-06)
    # of the same patient: still one patient in numerator and denominator.
    extra = np.array([[2, 2], [0, 0]], dtype=np.int8)
    data["calls"] = np.concatenate([data["calls"], extra], axis=1)
    data["samples"] = np.concatenate([data["samples"], [first, first[:13] + "06"]])
    inputs["AA"]["cna"]["cases"] |= {first[:13] + "06"}
    result = alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {}, DRIVER, cna_driver=CNA_DRIVER
    )
    erbb2 = category(result, "ERBB2", "AA", COPY_NUMBER_ALTERATION)
    assert (erbb2["n_events"], erbb2["n_profiled"]) == (10, 30)
    assert result.summary["cohorts"]["AA"][COPY_NUMBER_ALTERATION]["n_profiled"] == 30


def test_selection_with_two_samples_of_one_patient_is_rejected(tmp_path):
    inputs, mapping, selection = synthetic(tmp_path)
    selection["BB"].append(selection["AA"][0][:13] + "06")
    with pytest.raises(ValueError, match="one sample per patient"):
        alterations.compute(
            inputs, mapping, selection, IDS, CATALOG, {}, DRIVER, cna_driver=CNA_DRIVER
        )


def test_cna_bh_is_per_direction(result):
    amp = result.results[COPY_NUMBER_ALTERATION]["amp"]
    deletion = result.results[COPY_NUMBER_ALTERATION]["del"]
    for part in (amp, deletion):
        expected = np.where(np.isfinite(part["p_value"]), bh(part["p_value"]), np.nan)
        np.testing.assert_array_equal(part["q_value"], expected)
    erbb2 = category(result, "ERBB2", "AA", COPY_NUMBER_ALTERATION)
    assert erbb2["amp"]["q_value"] == pytest.approx(
        bh(amp["p_value"][ROW["ERBB2"] : ROW["ERBB2"] + 1])[0, 0]
    )


def test_metastatic_only_cna_is_not_testable(result):
    for gene in ("ERBB2", "TP53", "KRAS"):
        entry = category(result, gene, "SKCM", COPY_NUMBER_ALTERATION)
        assert entry["status"] == "not_testable"
        assert "metastatic" in entry["reason"] and "06" in entry["reason"]
        assert entry["amp"] is None and entry["del"] is None
    # SKCM primaries are excluded from the CNA rest denominator, not counted as wild type.
    erbb2 = category(result, "ERBB2", "AA", COPY_NUMBER_ALTERATION)
    assert erbb2["rest_n_profiled"] == 60


def test_cna_absent_gene_is_not_testable(result):
    entry = category(result, "KRAS", "AA", COPY_NUMBER_ALTERATION)
    assert entry["status"] == "not_testable"
    assert entry["reason"] == "gene not present in GISTIC copy-number data"


def test_rare_fusions_are_insufficient_data_not_negative(result):
    ros1 = category(result, "ROS1", "AA", STRUCTURAL_VARIANT)
    assert ros1["status"] == "insufficient_data" and ros1["n_events"] == 2
    assert "too rare" in ros1["reason"]
    none = category(result, "ROS1", "BB", STRUCTURAL_VARIANT)
    assert none["status"] == "insufficient_data" and none["n_events"] == 0


def test_every_pair_has_an_explicit_status_and_reason(result):
    for i in range(len(IDS)):
        for j, _ in enumerate(result.codes):
            record = result.record(i, j)
            assert list(record["categories"]) == list(ALTERATION_CATEGORIES)
            for entry in record["categories"].values():
                assert entry["status"] in alterations.STATUSES
                assert entry["status"] == "enriched" or entry["reason"]
            assert record["alteration_types_enriched"] == [
                c for c in ALTERATION_CATEGORIES if record["categories"][c]["status"] == "enriched"
            ]


def test_enriched_types_follow_sheet_order(tmp_path):
    inputs, mapping, selection = synthetic(tmp_path)
    data = inputs["AA"]["sv"]
    # Give ERBB2 a frequent fusion in AA so SV and CNA are both enriched.
    data["data"] = dict(
        sample=np.array(selection["AA"][:8]),
        site1=np.array(["ERBB2"] * 8),
        site2=np.array(["GRB7"] * 8),
    )
    result = alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {}, DRIVER, cna_driver=CNA_DRIVER
    )
    record = result.record(ROW["ERBB2"], 0)
    assert record["alteration_types_enriched"] == [STRUCTURAL_VARIANT, COPY_NUMBER_ALTERATION]


def test_too_few_profiled_patients_is_insufficient_data(tmp_path):
    inputs, mapping, selection = synthetic(tmp_path)
    inputs["AA"]["mutation"]["cases"] = set(selection["AA"][1:12])
    result = alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {}, DRIVER, cna_driver=CNA_DRIVER
    )
    entry = category(result, "KRAS", "AA", POINT_MUTATION)
    assert entry["status"] == "insufficient_data" and entry["n_profiled"] == 11


def test_bh_is_across_cohorts_per_gene_and_category():
    events = np.array([[10, 0, 1], [3, 3, 3]])
    profiled = np.array([30, 60, 25])
    out = alterations.fisher(events, profiled)
    assert out["p_value"].shape == (2, 3)
    for i in range(2):
        np.testing.assert_allclose(bh(out["p_value"][i : i + 1])[0], bh(out["p_value"])[i])
    # Undefined comparisons enter BH as p=1 and serialize as null.
    out = alterations.fisher(np.array([[1, 0]]), np.array([5, 0]))
    assert np.isnan(out["p_value"][0, 0])


def test_unbounded_ratio_with_zero_rest_passes_ratio_gate():
    out = alterations.fisher(np.array([[10, 0, 0]]), np.array([20, 100, 100]))
    out["q_value"] = bh(out["p_value"])
    passed, failed = alterations.gate(out, 0, 0, POINT_MUTATION)
    assert passed and failed == [] and np.isnan(out["ratio"][0, 0])


# ---------------------------------------------------------------- detail only + contract


def build_fixture(tmp_path, monkeypatch, alteration_inputs):
    """Two-gene expression fixture (as in test_contract) with configurable alterations."""
    cache = tmp_path / "cache"
    cache.mkdir()
    snapshot = cache / "snapshot.tsv"
    snapshot.write_text("snapshot")
    monkeypatch.setattr(builder, "hgnc_path", lambda cache: snapshot)
    paths, mapping = {}, []
    cohorts = [("LIHC", 31), ("PRAD", 0)] + [(f"T{i:02d}", 0) for i in range(2, 32)]
    for index, (code, value) in enumerate(cohorts):
        path = tmp_path / f"{code}.txt"
        samples = [f"TCGA-{index:02d}-{i:04d}-01" for i in range(20)]
        path.write_text(
            "Hugo_Symbol\tEntrez_Gene_Id\t"
            + "\t".join(samples)
            + "\n"
            + f"ALB\t213\t{chr(9).join([str(value)] * 20)}\n"
            + f"TP53\t7157\t{chr(9).join(['1'] * 20)}\n"
        )
        paths[code] = path
        mapping.append(
            dict(
                study_id=code,
                oncotree_code=code,
                name=code,
                tumor_type_query=code,
                import_date="today",
            )
        )
    monkeypatch.setattr(builder, "fetch_all", lambda *a: paths)
    monkeypatch.setattr(builder, "cancer_mapping", lambda *a: mapping)
    monkeypatch.setattr(builder, "hgnc", lambda *a: ({7157: "TP53", 213: "ALB"}, {}))
    monkeypatch.setattr(alterations, "load_inputs", alteration_inputs)
    monkeypatch.setattr(builder.drivers, "load", lambda name: DRIVER)
    monkeypatch.setattr(builder.drivers, "load_cna", lambda name: CNA_DRIVER)
    panel = {"controls": [dict(id="c", gene="ALB", tcga="LIHC", expected=True)]}
    evaluate = controls.evaluate
    monkeypatch.setattr(
        builder,
        "evaluate_controls",
        lambda directory, **kw: evaluate(directory, panel=panel, **kw),
    )
    return cache


def rich_inputs(commit, cache, studies, **kwargs):
    """Every study fully profiled; TP53 deep-deleted and truncated in LIHC."""
    inputs = {}
    for index, study in enumerate(studies):
        samples = [f"TCGA-{index:02d}-{i:04d}-01" for i in range(20)]
        lihc = study == "LIHC"
        mutation_rows = [
            ("TP53", 7157, "Nonsense_Mutation", s, "T", "p.R196*") for s in samples[:15] if lihc
        ]
        path = Path(cache) / f"{study}_mut.txt"
        path.write_text(maf(mutation_rows))
        calls = [-2 if lihc and i < 15 else 0 for i in range(20)]
        cna = dict(
            samples=np.array(samples),
            symbol=np.array(["TP53"]),
            entrez=np.array([7157]),
            calls=np.array([calls], dtype=np.int8),
        )
        sv = dict(
            sample=np.zeros(0, dtype=str),
            site1=np.zeros(0, dtype=str),
            site2=np.zeros(0, dtype=str),
        )
        inputs[study] = dict(
            mutation=dict(data=alterations.parse_mutations(path), cases=set(samples)),
            cna=dict(data=cna, cases=set(samples)),
            sv=dict(data=sv, cases=set(samples)),
        )
    return inputs, {s: {} for s in studies}, "1" * 64


def expression_only(directory):
    out = {}
    for path in sorted((directory / "v1/genes").glob("*.json")):
        document = json.loads(path.read_text())
        document.pop("release_id")
        for record in document["cancer_types"]:
            record.pop("alterations")
        out[path.name] = document
    return out


def test_alterations_are_detail_only(tmp_path, monkeypatch):
    cache = build_fixture(tmp_path, monkeypatch, rich_inputs)
    with_alterations, without = tmp_path / "with", tmp_path / "without"
    builder.build("a" * 40, with_alterations, cache, skip_precheck=True)
    builder.build("a" * 40, without, cache, skip_precheck=True, skip_alterations=True)
    tp53 = json.loads((with_alterations / "v1/genes/TP53.json").read_text())
    lihc = tp53["cancer_types"][0]
    assert lihc["oncotree_code"] == "LIHC"
    assert lihc["alterations"]["alteration_types_enriched"] == [
        POINT_MUTATION,
        COPY_NUMBER_ALTERATION,
    ]
    # Strongly enriched alterations change no expression field, pass, flag or label.
    assert expression_only(with_alterations) == expression_only(without)
    assert not lihc["stats_pass"]
    skipped = json.loads((without / "v1/genes/TP53.json").read_text())
    assert all(record["alterations"] is None for record in skipped["cancer_types"])
    manifest = json.loads((with_alterations / "v1/manifest.json").read_text())
    jsonschema.validate(manifest, SCHEMAS["manifest"])
    assert manifest["contract_version"] == "1.2" and manifest["method_version"] == "3"
    assert manifest["source"]["mutation_driver_source"] == "test"
    assert manifest["source"]["cna_driver_source"] == "test-cna"
    assert manifest["source"]["cna_driver_sha256"] == "1" * 64
    cna = lihc["alterations"]["categories"][COPY_NUMBER_ALTERATION]
    assert cna["allowed_directions"] == ["amp", "del"]
    other = json.loads((without / "v1/manifest.json").read_text())
    assert other["release_id"] != manifest["release_id"]
    assert manifest["build"]["skip_alterations"] is False


def test_gene_documents_validate_and_reject_unknown_status(tmp_path, monkeypatch):
    cache = build_fixture(tmp_path, monkeypatch, rich_inputs)
    builder.build("a" * 40, tmp_path / "site", cache, skip_precheck=True)
    document = json.loads((tmp_path / "site/v1/genes/TP53.json").read_text())
    jsonschema.validate(document, SCHEMAS["gene"])
    assert all("alterations" in record for record in document["cancer_types"])
    entry = document["cancer_types"][0]["alterations"]["categories"][POINT_MUTATION]
    entry["status"] = "maybe"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, SCHEMAS["gene"])
    entry.update(status="not_enriched", reason=None)
    with pytest.raises(jsonschema.ValidationError, match="reason|None"):
        jsonschema.validate(document, SCHEMAS["gene"])


def test_contract_1_0_documents_without_alterations_still_validate(tmp_path, monkeypatch):
    cache = build_fixture(tmp_path, monkeypatch, rich_inputs)
    builder.build("a" * 40, tmp_path / "site", cache, skip_precheck=True)
    document = json.loads((tmp_path / "site/v1/genes/TP53.json").read_text())
    for record in document["cancer_types"]:
        record.pop("alterations")
    jsonschema.validate(document, SCHEMAS["gene"])


# ---------------------------------------------------------------- control gate


CODES = ["BRCA", "LUAD", "SARC"]


def write_gene(directory, gene, statuses):
    """statuses: {code: {category: (status, enriched_directions)}}."""
    records = []
    for code in CODES:
        categories = {}
        for name in ALTERATION_CATEGORIES:
            status, directions = statuses.get(code, {}).get(name, ("not_enriched", []))
            categories[name] = dict(status=status, enriched_directions=directions, n_events=1)
        records.append(
            dict(oncotree_code=code, stats_pass=False, alterations=dict(categories=categories))
        )
    (directory / f"{gene}.json").write_text(json.dumps(dict(cancer_types=records)))


PANEL = {
    "controls": [],
    "alteration_controls": [
        dict(
            id="amp",
            gene="ERBB2",
            tcga="BRCA",
            category=COPY_NUMBER_ALTERATION,
            direction="amp",
            expected=True,
        ),
        dict(id="ttn", gene="TTN", tcga="*", category=POINT_MUTATION, expected=False),
        dict(id="alk", gene="ALK", tcga="LUAD", category=STRUCTURAL_VARIANT, expected=None),
    ],
}


@pytest.fixture
def gate_dir(tmp_path):
    write_gene(tmp_path, "ERBB2", {"BRCA": {COPY_NUMBER_ALTERATION: ("enriched", ["amp"])}})
    write_gene(tmp_path, "TTN", {})
    write_gene(tmp_path, "ALK", {"LUAD": {STRUCTURAL_VARIANT: ("enriched", [])}})
    return tmp_path


def test_alteration_controls_pass_and_expand_all_cohorts(gate_dir):
    report = controls.evaluate(gate_dir, panel=PANEL, codes=CODES)
    controls.require_passed(report)
    alteration = report["alterations"]
    assert (alteration["positive_passed"], alteration["positive_total"]) == (1, 1)
    assert (alteration["negative_passed"], alteration["negative_total"]) == (0, 3)
    ids = [row["id"] for row in alteration["controls"]]
    assert ids == ["amp", "ttn:BRCA", "ttn:LUAD", "ttn:SARC", "alk"]
    alk = alteration["controls"][-1]
    assert alk["observed"] == "enriched" and not alk["scored"]


@pytest.mark.parametrize(
    "problem", ["positive_miss", "wrong_direction", "negative_pass", "missing"]
)
def test_alteration_control_failures_fail_the_gate(gate_dir, problem):
    if problem == "positive_miss":
        write_gene(gate_dir, "ERBB2", {})
    elif problem == "wrong_direction":
        write_gene(gate_dir, "ERBB2", {"BRCA": {COPY_NUMBER_ALTERATION: ("enriched", ["del"])}})
    elif problem == "negative_pass":
        write_gene(gate_dir, "TTN", {"SARC": {POINT_MUTATION: ("enriched", [])}})
    else:
        (gate_dir / "TTN.json").unlink()
    report = controls.evaluate(gate_dir, panel=PANEL, codes=CODES)
    assert report["status"] == "failed" and report["alterations"]["status"] == "failed"
    with pytest.raises(ValueError, match="Controls gate"):
        controls.require_passed(report)
    if problem == "missing":
        assert report["alterations"]["missing"] == 3


def test_skipped_alterations_and_subsets_do_not_score(gate_dir):
    write_gene(gate_dir, "ERBB2", {})
    skipped = controls.evaluate(gate_dir, panel=PANEL, codes=CODES, alterations=False)
    assert skipped["alterations"]["status"] == "skipped" and skipped["status"] == "passed"
    subset = controls.evaluate(gate_dir, panel=PANEL, codes=CODES, subset=True)
    assert subset["alterations"]["status"] == "skipped_subset"


@pytest.mark.parametrize("duplicate", ["id", "pair", "expanded"])
def test_duplicate_alteration_controls_are_rejected(gate_dir, duplicate):
    first = dict(id="a", gene="KRAS", tcga="LUAD", category=POINT_MUTATION, expected=True)
    second = dict(first, id="b") if duplicate != "id" else dict(first, tcga="BRCA")
    if duplicate == "expanded":
        second["tcga"] = "*"
    panel = {"controls": [], "alteration_controls": [first, second]}
    with pytest.raises(ValueError, match="Duplicate alteration control"):
        controls.evaluate(gate_dir, panel=panel, codes=CODES)


def test_real_panel_has_the_required_alteration_controls():
    panel = json.loads(controls.PANEL_PATH.read_text())
    rows = {
        (r["gene"], r["tcga"], r["category"]): r
        for r in controls.expand_alteration_controls(panel, CODES + ["COADREAD"])
    }
    for gene in ("TTN", "MUC16"):
        for code in CODES + ["COADREAD"]:
            assert rows[(gene, code, POINT_MUTATION)]["expected"] is False
    assert rows[("ERBB2", "BRCA", COPY_NUMBER_ALTERATION)]["direction"] == "amp"
    assert rows[("ALK", "LUAD", STRUCTURAL_VARIANT)]["expected"] is None
    positives, negatives = controls.expected_alteration_counts(panel, [f"C{i}" for i in range(32)])
    assert positives == 13
    assert negatives == 2 * 32 + 2
    assert {r["category"] for r in rows.values()} == set(ALTERATION_CATEGORIES)
