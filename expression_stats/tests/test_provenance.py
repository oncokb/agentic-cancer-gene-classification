import pytest

from gene_expression_relevance import provenance


def test_fingerprint_tracks_source_and_override_content(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "ROOT", tmp_path)
    source = tmp_path / "stats.py"
    override = tmp_path / "query_overrides.json"
    source.write_text("source v1")
    override.write_text('{"brca":"breast cancer"}')
    first = provenance.code_fingerprint()
    old_override = provenance.overrides_fingerprint()
    source.write_text("source v2")
    assert provenance.code_fingerprint() != first
    assert provenance.overrides_fingerprint() == old_override
    override.write_text('{"brca":"breast carcinoma"}')
    assert provenance.overrides_fingerprint() != old_override


def test_build_identity_tracks_every_input():
    args = [dict(fdr=0.05), None, False, "hgnc", "code", "mapping"]
    expected = provenance.build_fingerprint(*args)
    for i, value in [
        (0, dict(fdr=0.01)),
        (1, ["ALB"]),
        (2, True),
        (3, "new-hgnc"),
        (4, "new-code"),
        (5, "new-mapping"),
    ]:
        updated = args.copy()
        updated[i] = value
        assert provenance.build_fingerprint(*updated) != expected


@pytest.mark.parametrize(
    "genes,skip,deployable",
    [("ALB", True, False), ("ALB", False, False), ("", True, False), ("", False, True)],
)
def test_workflow_deployment_policy(tmp_path, monkeypatch, genes, skip, deployable):
    import importlib.util
    from pathlib import Path

    from gene_expression_relevance import METHOD_VERSION, PARAMS

    spec = importlib.util.spec_from_file_location("check_build_test", "scripts/check_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = dict(
        schema_version="1",
        method_version=METHOD_VERSION,
        params=PARAMS,
        source=dict(
            datahub_commit="a" * 40,
            code_sha256=provenance.code_fingerprint(),
            hgnc_sha256=provenance.hgnc_pin()["sha256"],
        ),
        build=dict(genes=[genes] if genes else None, skip_precheck=skip),
    )
    monkeypatch.setattr(module, "published_manifest", lambda: manifest)
    output = tmp_path / "output"
    for key, value in dict(
        INPUT_COMMIT="a" * 40,
        INPUT_GENES=genes,
        INPUT_SKIP=str(skip).lower(),
        GITHUB_OUTPUT=str(output),
    ).items():
        monkeypatch.setenv(key, value)
    module.main()
    values = dict(line.split("=", 1) for line in Path(output).read_text().splitlines())
    assert values["changed"] == "true"
    assert values["deployable"] == str(deployable).lower()


@pytest.mark.parametrize("dependency", ["numpy", "scipy", "pandas"])
def test_resolved_dependency_versions_change_code_identity(monkeypatch, dependency):
    versions = dict.fromkeys(["numpy", "scipy", "pandas"], "1.0")
    monkeypatch.setattr(provenance, "version", versions.__getitem__)
    first = provenance.code_fingerprint()
    versions[dependency] = "2.0"
    assert provenance.code_fingerprint() != first


def test_pyproject_changes_code_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROJECT_ROOT", tmp_path)
    (tmp_path / "constraints.txt").write_text("numpy==2.5.4\n")
    (tmp_path / "controls.yaml").write_text('{"controls": []}\n')
    project = tmp_path / "pyproject.toml"
    project.write_text('[project]\nname = "test"\n')
    first = provenance.code_fingerprint()
    project.write_text('[project]\nname = "changed"\n')
    assert provenance.code_fingerprint() != first


@pytest.mark.parametrize("field", ["study_id", "oncotree_code", "name", "tumor_type_query"])
def test_cohort_metadata_changes_build_identity_but_import_dates_do_not(field):
    rows = [
        dict(
            study_id="brca",
            oncotree_code="BRCA",
            name="Breast",
            tumor_type_query="breast cancer",
            import_date="yesterday",
        ),
        dict(
            study_id="lihc",
            oncotree_code="LIHC",
            name="Liver",
            tumor_type_query="liver cancer",
            import_date="yesterday",
        ),
    ]
    original = provenance.mapping_fingerprint(rows)
    assert provenance.mapping_fingerprint(rows[::-1]) == original
    rows[0]["import_date"] = "today"
    assert provenance.mapping_fingerprint(rows) == original
    before = provenance.build_fingerprint({}, None, False, "hgnc", "code", original)
    rows[0][field] += " changed"
    after = provenance.mapping_fingerprint(rows)
    assert after != original
    assert provenance.build_fingerprint({}, None, False, "hgnc", "code", after) != before


@pytest.mark.parametrize("mapping_sha,changed", [("old-mapping", "false"), ("new-mapping", "true")])
def test_live_mapping_changes_invalidate_early_exit(tmp_path, monkeypatch, mapping_sha, changed):
    import importlib.util
    from datetime import UTC, datetime

    from gene_expression_relevance import METHOD_VERSION, PARAMS

    spec = importlib.util.spec_from_file_location("mapping_check_test", "scripts/check_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    manifest = dict(
        schema_version="1",
        method_version=METHOD_VERSION,
        params=PARAMS,
        source=dict(
            datahub_commit="a" * 40,
            code_sha256=provenance.code_fingerprint(),
            hgnc_sha256=provenance.hgnc_pin()["sha256"],
            generated_at=datetime.now(UTC).isoformat(),
            cohort_mapping_sha256="old-mapping",
        ),
        build=dict(genes=None, skip_precheck=False),
    )
    monkeypatch.setattr(module, "published_manifest", lambda: manifest)
    monkeypatch.setattr(module, "current_mapping_sha", lambda commit: mapping_sha)
    output = tmp_path / "output"
    for key, value in dict(
        INPUT_COMMIT="a" * 40,
        INPUT_GENES="",
        INPUT_SKIP="false",
        GITHUB_OUTPUT=str(output),
    ).items():
        monkeypatch.setenv(key, value)
    module.main()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["changed"] == changed


def test_controls_panel_changes_code_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(provenance, "PROJECT_ROOT", tmp_path)
    for name in ("pyproject.toml", "constraints.txt"):
        (tmp_path / name).write_text("fixture")
    panel = tmp_path / "controls.yaml"
    panel.write_text('{"controls": []}')
    before = provenance.code_fingerprint()
    panel.write_text('{"controls": [{"gene": "ERBB2"}]}')
    assert provenance.code_fingerprint() != before
