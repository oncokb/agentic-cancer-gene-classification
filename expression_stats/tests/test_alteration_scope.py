"""Alteration method 2: CNA driver scope / direction-by-role and wider mutation eligibility."""

import csv
import hashlib
import json
import shutil

import pytest
from test_alterations import (
    CATALOG,
    CNA_DRIVER,
    DRIVER,
    IDS,
    ROW,
    category,
    records,
    sample,
    synthetic,
)

from gene_expression_relevance import (
    ALTERATION_METHOD_VERSION,
    ALTERATION_PARAMS,
    COPY_NUMBER_ALTERATION,
    POINT_MUTATION,
    alterations,
    drivers,
    provenance,
)


def cna_source(genes, oncogenes=(), tumor_suppressors=()):
    return drivers.CnaDriverSource(
        name="scope-test",
        sha256="2" * 64,
        genes=frozenset(genes),
        oncogenes=frozenset(oncogenes),
        tumor_suppressors=frozenset(tumor_suppressors),
    )


def run(tmp_path, cna_driver, driver=DRIVER, params=ALTERATION_PARAMS):
    tmp_path.mkdir(parents=True, exist_ok=True)
    inputs, mapping, selection = synthetic(tmp_path)
    return alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {}, driver, params, cna_driver=cna_driver
    )


# ---------------------------------------------------------------- CNA driver scope
# Synthetic signal: ERBB2 amplified in 10/30 AA; TP53 deep-deleted in 15/60 BB.


def test_gene_off_the_cna_driver_list_is_not_testable_with_reason(tmp_path):
    result = run(tmp_path, cna_source({"TP53"}))
    for code in ("AA", "BB"):
        entry = category(result, "ERBB2", code, COPY_NUMBER_ALTERATION)
        assert entry["status"] == "not_testable"
        assert entry["reason"] == "not a copy-number driver"
        assert entry["enriched_directions"] == [] and entry["allowed_directions"] == []
        assert entry["amp"] is None and entry["del"] is None and entry["q_value"] is None
    record = result.record(ROW["ERBB2"], result.codes.index("AA"))
    assert COPY_NUMBER_ALTERATION not in record["alteration_types_enriched"]
    # Cohort-level reasons keep precedence (SKCM CNA is metastatic-only).
    skcm = category(result, "ERBB2", "SKCM", COPY_NUMBER_ALTERATION)
    assert "non-primary" in skcm["reason"]
    assert category(result, "TP53", "BB", COPY_NUMBER_ALTERATION)["status"] == "enriched"
    assert result.summary["cna_driver_genes_resolved"] == 1


def test_oncogene_with_deletion_only_signal_is_not_enriched(tmp_path):
    result = run(tmp_path, cna_source({"ERBB2", "TP53"}, oncogenes={"ERBB2", "TP53"}))
    entry = category(result, "TP53", "BB", COPY_NUMBER_ALTERATION)
    assert entry["status"] == "not_enriched"
    assert entry["allowed_directions"] == ["amp"] and entry["enriched_directions"] == []
    assert entry["direction"] == "amp"
    assert "del: not called: oncogene role allows amplification only" in entry["reason"]
    # Deletion statistics remain visible and unchanged; only the call is withheld.
    assert entry["del"]["n_events"] == 15 and entry["del"]["q_value"] < 0.05
    assert entry["del"]["enriched"] is False
    amp = category(result, "ERBB2", "AA", COPY_NUMBER_ALTERATION)
    assert amp["status"] == "enriched" and amp["enriched_directions"] == ["amp"]


def test_tumor_suppressor_with_amplification_only_signal_is_not_enriched(tmp_path):
    result = run(tmp_path, cna_source({"ERBB2", "TP53"}, tumor_suppressors={"ERBB2", "TP53"}))
    entry = category(result, "ERBB2", "AA", COPY_NUMBER_ALTERATION)
    assert entry["status"] == "not_enriched"
    assert entry["allowed_directions"] == ["del"] and entry["direction"] == "del"
    assert "amp: not called: tumor suppressor role allows deep deletion only" in entry["reason"]
    assert entry["amp"]["n_events"] == 10 and entry["amp"]["enriched"] is False
    deletion = category(result, "TP53", "BB", COPY_NUMBER_ALTERATION)
    assert deletion["status"] == "enriched" and deletion["enriched_directions"] == ["del"]


def test_unknown_or_dual_role_allows_both_and_bh_families_are_unchanged(tmp_path):
    unrestricted = run(tmp_path / "a", CNA_DRIVER)
    dual = run(
        tmp_path / "b",
        cna_source({"ERBB2", "TP53"}, oncogenes={"TP53"}, tumor_suppressors={"TP53"}),
    )
    for gene, code in (("ERBB2", "AA"), ("TP53", "BB")):
        before = category(unrestricted, gene, code, COPY_NUMBER_ALTERATION)
        after = category(dual, gene, code, COPY_NUMBER_ALTERATION)
        assert after["allowed_directions"] == ["amp", "del"]
        assert after == before  # identical statuses, statistics and per-direction q
    # Amplification and deletion remain separate 32-cohort-style BH families.
    single = run(tmp_path / "c", cna_source({"ERBB2"}, oncogenes={"ERBB2"}))
    for direction in ("amp", "del"):
        assert (
            category(single, "ERBB2", "AA", COPY_NUMBER_ALTERATION)[direction]["q_value"]
            == category(unrestricted, "ERBB2", "AA", COPY_NUMBER_ALTERATION)[direction]["q_value"]
        )


def test_role_labels_from_two_symbols_of_one_gene_combine(tmp_path):
    # An oncogene label on one symbol and a TSG label on an alias allow both directions.
    inputs, mapping, selection = synthetic(tmp_path)
    source = cna_source({"TP53", "P53"}, oncogenes={"TP53"}, tumor_suppressors={"P53"})
    result = alterations.compute(
        inputs, mapping, selection, IDS, CATALOG, {"P53": "TP53"}, DRIVER, cna_driver=source
    )
    entry = category(result, "TP53", "BB", COPY_NUMBER_ALTERATION)
    assert entry["allowed_directions"] == ["amp", "del"] and entry["status"] == "enriched"


def test_real_cna_driver_list_scope_and_roles():
    source = drivers.load_cna(ALTERATION_PARAMS["cna_driver_source"])
    bailey = drivers.load_bailey2018()
    curated, _ = drivers.load_curated("cna")
    pathways, _ = drivers.load_sanchezvega2018_cna()
    assert len(curated) == 38 and bailey.genes <= source.genes
    assert source.genes == bailey.genes | set(curated) | set(pathways)
    assert len(source.genes) == 378
    assert source.directions("ERBB2") == ("amp",)  # Bailey + curated oncogene
    assert source.directions("CDK4") == ("amp",)
    # MDM2 is in neither Bailey nor the curator sheet: recurrent SARC/GBM GISTIC amp target.
    assert "MDM2" in pathways and source.directions("MDM2") == ("amp",)
    # Pathway membership without a role-consistent GISTIC target does not qualify.
    assert "CDKN1A" not in pathways and "CDKN1B" in pathways
    assert source.directions("CDK12") == ("del",)  # Bailey and curated TSG
    assert source.directions("CDKN2A") == ("del",)  # Bailey TSG
    assert source.directions("STK11") == ("del",)  # curated TSG
    assert source.directions("CAMK1D") == ("amp", "del")  # curated, no role
    assert source.directions("GAPDH") == ()
    assert ALTERATION_PARAMS["cna_direction_by_role"] is True


# ---------------------------------------------------------------- mutation eligibility


def test_real_mutation_eligibility_adds_the_46_curated_genes():
    source = drivers.load(ALTERATION_PARAMS["mutation_driver_source"])
    bailey = drivers.load_bailey2018()
    curated, _ = drivers.load_curated("mutation")
    assert len(bailey.genes) == 299 and len(curated) == 46 and len(source.genes) == 345
    assert source.genes == bailey.genes | set(curated)
    # Bailey genes keep their Bailey truncation rule; additions follow the curated role.
    assert source.tumor_suppressors & bailey.genes == bailey.tumor_suppressors
    added_tsg = source.tumor_suppressors - bailey.genes
    assert added_tsg == {g for g, roles in curated.items() if drivers.TSG in roles}
    assert {"BMPR1A", "CHEK1", "WEE1", "EPHB1"} <= added_tsg  # TSG and OG/TSG
    for gene in ("TTN", "OBSCN", "BMPR2", "BTK", "ROS1", "NTRK1"):  # unknown or OG role
        assert gene in source.genes and gene not in source.tumor_suppressors


def test_added_gene_with_recurrent_missense_is_enriched(tmp_path):
    # BB carries TTN p.A5T in all 60 tumours: recurrent missense once TTN is eligible.
    widened = drivers.DriverSource(
        name="widened",
        sha256="3" * 64,
        genes=DRIVER.genes | {"TTN"},
        tumor_suppressors=DRIVER.tumor_suppressors,
    )
    before = category(run(tmp_path / "a", CNA_DRIVER), "TTN", "BB", POINT_MUTATION)
    assert before["status"] == "not_testable"
    assert before["reason"] == "gene not in mutation driver list (test)"
    after = category(run(tmp_path / "b", CNA_DRIVER, widened), "TTN", "BB", POINT_MUTATION)
    assert after["status"] == "enriched" and after["n_events"] == 60


def test_added_gene_with_unknown_role_ignores_truncating_mutations():
    truncating = [
        ("TTN", sample("AA", i), "Nonsense_Mutation", "T1", 100 + i, "") for i in range(12)
    ]
    frameshift = [("TTN", sample("BB", i), "Frame_Shift_Del", "T1", 300 + i, "") for i in range(12)]
    missense = [("TTN", sample("CC", i), "Missense_Mutation", "T1", 7, "") for i in range(10)]
    rows = truncating + frameshift + missense
    retained = {r[1] for r in rows}
    unknown = {ROW["TTN"]}
    events = alterations.point_mutation_events(records(rows), retained, unknown, set())
    assert events == {(ROW["TTN"], r[1]) for r in missense}
    # The same gene with a TSG role counts its truncating calls.
    as_tsg = alterations.point_mutation_events(records(rows), retained, unknown, unknown)
    assert len(as_tsg) == 34


# ---------------------------------------------------------------- pinning and identity


def copy_package(tmp_path, monkeypatch):
    root = tmp_path / "pkg"
    shutil.copytree(drivers.ROOT, root, ignore=shutil.ignore_patterns("__pycache__"))
    monkeypatch.setattr(drivers, "ROOT", root)
    return root


@pytest.mark.parametrize("kind", ["cna", "mutation"])
def test_curated_lists_are_sha256_pinned(tmp_path, monkeypatch, kind):
    root = copy_package(tmp_path, monkeypatch)
    pin = drivers.curated_pin()["lists"][kind]
    path = root / pin["file"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == pin["sha256"]
    path.write_text(path.read_text().replace("\tOG\t", "\tTSG\t", 1))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        drivers.load_curated(kind)
    loader = drivers.load_cna if kind == "cna" else drivers.load
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        loader(ALTERATION_PARAMS[f"{'cna' if kind == 'cna' else 'mutation'}_driver_source"])


def test_pathway_list_is_sha256_pinned(tmp_path, monkeypatch):
    root = copy_package(tmp_path, monkeypatch)
    path = root / drivers.sanchezvega_pin()["file"]
    path.write_text(path.read_text().replace("MDM2\tTP53\tOG", "MDM2\tTP53\tTSG"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        drivers.load_cna(ALTERATION_PARAMS["cna_driver_source"])


def test_curated_list_gene_count_and_rows_are_validated(tmp_path, monkeypatch):
    root = copy_package(tmp_path, monkeypatch)
    pin_path = root / "curated-lists-pin.json"
    pin = json.loads(pin_path.read_text())
    pin["lists"]["cna"]["genes"] = 39
    pin_path.write_text(json.dumps(pin))
    with pytest.raises(ValueError, match="pin says 39"):
        drivers.load_curated("cna")


def identity(cna, mutation):
    return dict(
        method_version=ALTERATION_METHOD_VERSION,
        params=ALTERATION_PARAMS,
        inputs_sha256="0" * 64,
        **mutation.pin(),
        **cna.pin(),
    )


def fingerprint(alteration_identity, code_sha="code"):
    return provenance.build_fingerprint(
        {}, None, False, "hgnc", code_sha, "mapping", alterations=alteration_identity
    )


@pytest.mark.parametrize("kind", ["cna", "mutation"])
def test_changing_a_pinned_list_changes_the_release_identity(tmp_path, monkeypatch, kind):
    params = ALTERATION_PARAMS
    original = identity(
        drivers.load_cna(params["cna_driver_source"]),
        drivers.load(params["mutation_driver_source"]),
    )
    assert original["cna_driver_files"]["new_framework_cna_genes.tsv"]
    assert original["mutation_driver_files"]["new_framework_mutation_genes.tsv"]
    root = copy_package(tmp_path, monkeypatch)
    monkeypatch.setattr(provenance, "ROOT", root)
    code_before = provenance.code_fingerprint()
    # Drop one gene from the list and re-pin it, as a reviewed list change would.
    pin_path = root / "curated-lists-pin.json"
    pin = json.loads(pin_path.read_text())
    path = root / pin["lists"][kind]["file"]
    path.write_text("".join(path.read_text().splitlines(keepends=True)[:-1]))
    pin["lists"][kind].update(
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(), genes=pin["lists"][kind]["genes"] - 1
    )
    pin_path.write_text(json.dumps(pin))
    changed = identity(
        drivers.load_cna(params["cna_driver_source"]),
        drivers.load(params["mutation_driver_source"]),
    )
    key = f"{'cna' if kind == 'cna' else 'mutation'}_driver_sha256"
    assert changed[key] != original[key]
    assert fingerprint(changed) != fingerprint(original)
    # The TSVs are also part of the code fingerprint.
    assert provenance.code_fingerprint() != code_before


def test_parameters_record_the_new_scope():
    assert ALTERATION_METHOD_VERSION == "2"
    assert ALTERATION_PARAMS["mutation_driver_source"] == "bailey2018+new_framework"
    assert ALTERATION_PARAMS["cna_driver_source"] == "bailey2018+new_framework+sanchezvega2018"
    with open(drivers.ROOT / "new_framework_cna_genes.tsv") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    assert {r["role"] for r in rows} <= set(drivers.CURATED_ROLES)
