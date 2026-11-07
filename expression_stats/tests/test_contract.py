import importlib
import json
import runpy
from pathlib import Path

import jsonschema
import numpy as np
import pytest

from gene_expression_relevance import PARAMS
from gene_expression_relevance import build as builder

SCHEMAS = json.loads((Path(__file__).parent.parent / "docs/artifact-schema.json").read_text())


@pytest.fixture(autouse=True)
def synthetic_controls(monkeypatch):
    from gene_expression_relevance.controls import evaluate

    panel = {
        "controls": [
            {
                "id": "fixture-ALB-LIHC",
                "gene": "ALB",
                "tcga": "LIHC",
                "classification": "A",
                "expected": True,
            }
        ]
    }
    monkeypatch.setattr(
        builder, "evaluate_controls", lambda directory, **kw: evaluate(directory, panel=panel, **kw)
    )


@pytest.mark.parametrize("skip", [True, False])
@pytest.mark.parametrize("genes", [None, [], [" ", ""], " , "])
def test_build_contract_and_precheck_gating(tmp_path, monkeypatch, skip, genes):
    cache = tmp_path / "cache"
    cache.mkdir()
    snapshot = cache / "snapshot.tsv"
    snapshot.write_text("test snapshot")
    monkeypatch.setattr(builder, "hgnc_path", lambda cache: snapshot)
    paths, mapping = {}, []
    cohorts = [("LIHC", 31), ("PRAD", 0)] + [(f"T{i:02d}", 0) for i in range(2, 32)]
    for cohort_index, (code, value) in enumerate(cohorts):
        path = tmp_path / f"{code}.txt"
        samples = [f"TCGA-{cohort_index:02d}-{i:04d}-01" for i in range(9 if code == "T31" else 10)]
        # Reversed Entrez row order exercises alignment rather than row position.
        values = [(213, "ALB", value), (7157, "TP53", 1)]
        if code == "PRAD":
            values.reverse()
        path.write_text(
            "Hugo_Symbol\tEntrez_Gene_Id\t"
            + "\t".join(samples)
            + "\n"
            + "\n".join(
                f"{symbol}\t{entrez}\t" + "\t".join([str(v)] * len(samples))
                for entrez, symbol, v in values
            )
            + "\n"
        )
        paths[code] = path
        mapping.append(
            dict(
                study_id=code,
                oncotree_code=code,
                name=code,
                tumor_type_query=code + " cancer",
                import_date="today",
            )
        )
    monkeypatch.setattr(builder, "fetch_all", lambda *a: paths)
    monkeypatch.setattr(builder, "cancer_mapping", lambda *a: mapping)
    monkeypatch.setattr(
        builder, "hgnc", lambda *a: ({7157: "TP53"}, {"P53": "TP53", "ALB": "TP53"})
    )
    calls = []

    def count(self, gene, code, query):
        calls.append((gene, code))
        return 1

    monkeypatch.setattr(builder.Precheck, "count", count)
    out = tmp_path / "site"
    builder.build("a" * 40, out, cache, genes=genes, skip_precheck=skip)
    manifest = json.loads((out / "v1/manifest.json").read_text())
    jsonschema.validate(manifest, SCHEMAS["manifest"])
    assert manifest["build"]["genes"] is None
    aliases = json.loads((out / "v1/aliases.json").read_text())
    jsonschema.validate(aliases, SCHEMAS["aliases"])
    assert aliases == {"P53": "TP53"}
    for path in (out / "v1/genes").glob("*.json"):
        artifact = json.loads(path.read_text())
        jsonschema.validate(artifact, SCHEMAS["gene"])
        assert artifact["release_id"] == manifest["release_id"]
    alb = json.loads((out / "v1/genes/ALB.json").read_text())
    hit, excluded, *_ = alb["cancer_types"]
    assert len(alb["cancer_types"]) == 32
    assert alb["cancer_types"][-1]["flags"] == ["small_n"]
    assert hit["stats_pass"] and hit["log2fc"] == pytest.approx(5 - PARAMS["expression_floor_log2"])
    assert hit["flags"] == ["tissue_of_origin_risk"]
    assert hit["pubmed_count"] == (None if skip else 1)
    assert hit["literature_precheck_pass"] == (None if skip else True)
    assert excluded["pubmed_count"] is None
    assert calls == ([] if skip else [("ALB", "LIHC")])
    assert not (out / "index.html").exists()
    # Advancing the clock and precheck freshness must not change the input identity.
    from datetime import UTC, datetime

    class LaterDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2027, 1, 7, tzinfo=UTC)

    monkeypatch.setattr(builder, "datetime", LaterDateTime)
    builder.build("a" * 40, out, cache, genes=genes, skip_precheck=skip)
    refreshed = json.loads((out / "v1/manifest.json").read_text())
    assert refreshed["release_id"] == manifest["release_id"]
    assert refreshed["source"]["generated_at"] != manifest["source"]["generated_at"]
    if not skip:
        assert (
            refreshed["source"]["precheck_expires_at"] != manifest["source"]["precheck_expires_at"]
        )
    # A subset rebuild replaces old gene files and restricts aliases to published genes.
    builder.build("a" * 40, out, cache, genes=["ALB"], skip_precheck=True)
    subset_manifest = json.loads((out / "v1/manifest.json").read_text())
    assert subset_manifest["release_id"] != manifest["release_id"]
    assert not (out / "v1/genes/TP53.json").exists()
    assert json.loads((out / "v1/aliases.json").read_text()) == {}


def test_undefined_measurements_are_strict_json(tmp_path):
    builder.write_json(tmp_path / "ok.json", {"value": None})
    with pytest.raises(ValueError):
        builder.write_json(tmp_path / "bad.json", {"value": np.nan})


def test_schema_rejects_wrong_version():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"schema_version": "2"}, SCHEMAS["gene"])


def test_early_exit_checks_method_commit_options_hashes_and_ttl():
    from datetime import UTC, datetime, timedelta

    unchanged = runpy.run_path("scripts/check_build.py")["unchanged"]
    now = datetime(2026, 11, 5, tzinfo=UTC)
    manifest = dict(
        schema_version="1",
        method_version="3",
        params=PARAMS.copy(),
        source=dict(
            datahub_commit="a" * 40,
            code_sha256="code",
            hgnc_sha256="hgnc",
            generated_at=now.isoformat(),
        ),
        build=dict(genes=None, skip_precheck=False),
    )
    args = ["a" * 40, "3", None, False, "code", "hgnc"]
    assert unchanged(manifest, *args, now=now)
    for i, value in [
        (0, "b" * 40),
        (1, "4"),
        (2, ["ALB"]),
        (3, True),
        (4, "changed-code"),
        (5, "changed-hgnc"),
    ]:
        changed = args.copy()
        changed[i] = value
        assert not unchanged(manifest, *changed, now=now)
    assert not unchanged(manifest, *args, now=now + timedelta(days=30))
    manifest["source"]["precheck_expires_at"] = (now + timedelta(days=1)).isoformat()
    assert not unchanged(manifest, *args, now=now + timedelta(days=2))
    manifest["params"] = {**PARAMS, "log2fc": 2}
    assert not unchanged(manifest, *args, now=now)


def test_cli_dispatch(monkeypatch):
    cli = importlib.import_module("gene_expression_relevance.__main__")
    calls = []
    monkeypatch.setattr(cli, "build", lambda *args: calls.append(args))
    monkeypatch.setattr(
        "sys.argv",
        [
            "tool",
            "build",
            "--datahub-commit",
            "a" * 40,
            "--out",
            "site",
            "--genes",
            "ALB,TP53",
            "--skip-precheck",
        ],
    )
    cli.main()
    assert calls[0][0] == "a" * 40
    assert calls[0][-2:] == (["ALB", "TP53"], True)


def test_matrix_duplicate_entrez_averages_linear_values(tmp_path):
    path = tmp_path / "input.txt"
    path.write_text("Hugo_Symbol\tEntrez_Gene_Id\tTCGA-AA-0001-01\nALB\t213\t1\nALB\t213\t2\n")
    matrix, labels, ids = builder.matrix(
        {"study": path},
        [{"study_id": "study", "oncotree_code": "LIHC"}],
        {213: "ALB"},
        tmp_path / "matrix.bin",
    )
    assert matrix[0, 0] == 1.5
    assert ids == [213]
    assert list(labels) == ["LIHC"]


def test_sample_columns_remain_aligned_after_primary_filter_and_patient_dedupe(tmp_path):
    from gene_expression_relevance.stats import compute

    columns = {
        "A": [
            ("TCGA-AA-0002-01", 31),
            ("TCGA-AA-9999-11", 99999),
            ("TCGA-AA-0001-01B", 127),
            ("TCGA-AA-0001-01A", 7),
            ("TCGA-AB-0003-01", 15),
            ("TCGA-AA-0004-02", 99999),
        ],
        "B": [
            ("TCGA-BB-0002-01", 63),
            ("TCGA-AA-0002-01", 1023),
            ("TCGA-BB-0001-01B", 255),
            ("TCGA-BB-8888-11", 99999),
            ("TCGA-BB-0001-01A", 31),
        ],
        "LAML": [
            ("TCGA-CC-0003-03", 255),
            ("TCGA-CC-0001-03B", 511),
            ("TCGA-CC-0002-01", 99999),
            ("TCGA-CC-0001-03A", 127),
        ],
    }
    paths, mapping = {}, []
    for code, entries in columns.items():
        path = tmp_path / f"{code}.txt"
        rows = [(213, "ALB", 1), (7157, "TP53", 2)]
        if code == "B":
            rows.reverse()
        path.write_text(
            "Hugo_Symbol\tEntrez_Gene_Id\t"
            + "\t".join(s for s, v in entries)
            + "\n"
            + "\n".join(
                f"{symbol}\t{entrez}\t" + "\t".join(str(v * scale) for s, v in entries)
                for entrez, symbol, scale in rows
            )
            + "\n"
        )
        paths[code] = path
        mapping.append(dict(study_id=code, oncotree_code=code))
    matrix, labels, ids = builder.matrix(
        paths, mapping, {213: "ALB", 7157: "TP53"}, tmp_path / "matrix.bin"
    )
    expected = np.array([7, 31, 15, 31, 63, 127, 255])
    assert ids == [213, 7157]
    np.testing.assert_array_equal(matrix, [expected, expected * 2])
    np.testing.assert_array_equal(labels, ["A", "A", "A", "B", "B", "LAML", "LAML"])
    result = compute(matrix, labels, ["A", "B", "LAML"])
    np.testing.assert_array_equal(result["n"], [[3, 2, 2], [3, 2, 2]])
    np.testing.assert_array_equal(result["n_rest"], [[4, 5, 5], [4, 5, 5]])
    np.testing.assert_array_equal(result["median_log2"][0], [4, 5.5, 7.5])
    np.testing.assert_array_equal(result["rest_median_log2"][0], [6.5, 5, 5])


def test_nullable_wire_fields_preserve_schema_one():
    nullable = [
        "median_log2",
        "rest_median_log2",
        "log2fc",
        "log2fc_vs_median_of_type_medians",
        "cliffs_delta",
        "auc",
        "p_value",
        "q_value",
        "p90_log2",
        "rest_p90_log2",
        "pubmed_count",
        "literature_precheck_pass",
    ]
    record = dict.fromkeys(nullable)
    record.update(
        oncotree_code="GBM",
        n=0,
        n_rest=10,
        stats_pass=False,
        flags=["small_n"],
        pass_mode=None,
        lineage_blocked_by_broad=False,
        subgroup_high_count=None,
        subgroup_high_fraction=None,
        subgroup_high_fraction_ratio=None,
        p95_log2_diff=None,
        subgroup_q=None,
    )
    artifact = dict(
        schema_version="1",
        release_id="datahub-aaaaaaaaaaaa-m3-bbbbbbbbbbbb",
        gene=dict(hugo_symbol="ALB", entrez_gene_id=213),
        broadly_expressed=False,
        cancer_types=[record.copy() for _ in range(32)],
    )
    jsonschema.validate(artifact, SCHEMAS["gene"])
    artifact["cancer_types"][0]["n"] = None
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(artifact, SCHEMAS["gene"])


@pytest.mark.parametrize(
    "release_id",
    [
        "2026-11-05-datahub-aaaaaaaaaaaa-m3-bbbbbbbbbbbb",
        "datahub-aaaa-m3-bbbbbbbbbbbb",
        "datahub-aaaaaaaaaaaa-m1-bbbbbbbbbbbb",
    ],
)
def test_schema_rejects_noncanonical_release_ids(release_id):
    for schema in [SCHEMAS["manifest"], SCHEMAS["gene"]]:
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(release_id, schema["properties"]["release_id"])


def test_ttl_refresh_changes_precheck_false_to_true_without_new_release(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    from gene_expression_relevance.precheck import Precheck

    now = [1_800_000_000.0]

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now[0], UTC)

    monkeypatch.setattr(builder, "datetime", ClockDateTime)
    cache = tmp_path / "cache"
    cache.mkdir()
    snapshot = cache / "snapshot.tsv"
    snapshot.write_text("snapshot")
    monkeypatch.setattr(builder, "hgnc_path", lambda cache: snapshot)
    monkeypatch.setattr(builder, "hgnc", lambda cache: ({213: "ALB"}, {}))
    paths, mapping = {}, []
    for i in range(32):
        code = "LIHC" if i == 0 else f"T{i:02d}"
        path = tmp_path / f"{code}.txt"
        path.write_text(
            "Hugo_Symbol\tEntrez_Gene_Id\t"
            + "\t".join(f"TCGA-{i:02d}-{j:04d}-01" for j in range(10))
            + "\nALB\t213\t"
            + "\t".join(["63" if i == 0 else "0"] * 10)
            + "\n"
        )
        paths[code] = path
        mapping.append(
            dict(
                study_id=code,
                oncotree_code=code,
                name=code,
                tumor_type_query="liver cancer" if i == 0 else code,
                import_date="today",
            )
        )
    monkeypatch.setattr(builder, "fetch_all", lambda *a: paths)
    monkeypatch.setattr(builder, "cancer_mapping", lambda *a: mapping)
    monkeypatch.setattr(
        builder, "Precheck", lambda cache: Precheck(cache, api_key="", wall_clock=lambda: now[0])
    )
    calls = []
    counts = iter([0, 3])

    def get(url, before):
        before()
        calls.append(url)
        return {"esearchresult": {"count": str(next(counts))}}

    monkeypatch.setattr("gene_expression_relevance.precheck.http.get_json", get)
    out = tmp_path / "site"
    before = builder.build("a" * 40, out, cache)
    before_gene = json.loads((out / "v1/genes/ALB.json").read_text())
    assert before_gene["cancer_types"][0]["literature_precheck_pass"] is False
    # A non-expired rebuild reuses the zero count.
    builder.build("a" * 40, out, cache)
    assert len(calls) == 1
    now[0] += PARAMS["precheck_cache_ttl_days"] * 86400 + 1
    after = builder.build("a" * 40, out, cache)
    after_gene = json.loads((out / "v1/genes/ALB.json").read_text())
    assert len(calls) == 2
    assert before["release_id"] == after["release_id"]
    assert before_gene["release_id"] == after_gene["release_id"]
    assert before["source"]["precheck_expires_at"] != after["source"]["precheck_expires_at"]
    assert after_gene["cancer_types"][0]["pubmed_count"] == 3
    assert after_gene["cancer_types"][0]["literature_precheck_pass"] is True
