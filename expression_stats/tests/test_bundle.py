import gzip
import importlib.util
import json
from pathlib import Path

import pytest

from gene_expression_relevance import controls
from gene_expression_relevance.controls import expected_alteration_counts

CATEGORIES = ("Point Mutation", "Structural Variant", "Copy Number Alteration")
PANEL = json.loads(Path("controls.yaml").read_text())


def panel_codes(panel):
    rows = panel["controls"] + panel.get("alteration_controls", [])
    codes = sorted({row["tcga"] for row in rows} - {controls.ALL_COHORTS})
    return codes + [f"T{i:02d}" for i in range(32 - len(codes))]


CODES = panel_codes(PANEL)


def synthesize(panel, codes, genes_dir, release_id):
    """One gene document per control gene whose calls exactly satisfy the panel."""
    expression = {(r["gene"], r["tcga"]) for r in panel["controls"] if r["expected"] is True}
    enriched = {
        (r["gene"], r["tcga"], r["category"]): [r["direction"]] if r.get("direction") else []
        for r in controls.expand_alteration_controls(panel, codes)
        if r["expected"] is True
    }
    genes_dir.mkdir(parents=True, exist_ok=True)
    for gene in sorted(controls.control_genes(panel)):
        records = []
        for code in codes:
            categories = {}
            for category in CATEGORIES:
                hit = (gene, code, category) in enriched
                categories[category] = dict(
                    status="enriched" if hit else "not_enriched",
                    enriched_directions=enriched.get((gene, code, category), []),
                    n_events=10 if hit else 0,
                    n_profiled=100,
                )
            records.append(
                dict(
                    oncotree_code=code,
                    stats_pass=(gene, code) in expression,
                    pass_mode="lineage" if (gene, code) in expression else None,
                    confidence_tier="high" if (gene, code) in expression else None,
                    alterations=dict(categories=categories),
                )
            )
        document = dict(release_id=release_id, gene={"hugo_symbol": gene}, cancer_types=records)
        (genes_dir / f"{gene}.json").write_text(json.dumps(document) + "\n")
    return controls.evaluate(genes_dir, panel=panel, codes=codes, alterations=True)


def alteration_report(panel_path=Path("controls.yaml"), codes=CODES):
    positives, negatives = expected_alteration_counts(json.loads(panel_path.read_text()), codes)
    return dict(
        status="passed",
        positive_passed=positives,
        positive_total=positives,
        negative_passed=0,
        negative_total=negatives,
        missing=0,
        failures=[],
    )


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path("scripts") / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundled(tmp_path):
    module = load("bundle")
    source = tmp_path / "artifacts" / "v1"
    (source / "genes").mkdir(parents=True)
    manifest = dict(
        release_id="datahub-aaaaaaaaaaaa-m3-bbbbbbbbbbbb",
        source=dict(generated_at="2026-11-06T12:00:00+00:00", precheck_expires_at="later"),
        build=dict(genes=None, skip_precheck=False, skip_alterations=False),
        cancer_types=[dict(oncotree_code=code) for code in CODES],
    )
    report = synthesize(PANEL, CODES, source / "genes", manifest["release_id"])
    assert report["status"] == "passed" and report["alterations"]["status"] == "passed"
    manifest["controls"] = {key: value for key, value in report.items() if key != "controls"}
    (source / "controls-report.json").write_text(json.dumps(report))
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "aliases.json").write_text('{"P53":"TP53"}\n')
    gene = json.loads((source / "genes/ERBB2.json").read_text())
    destination = tmp_path / "bundle"
    result = module.bundle(source.parent, destination)
    return module, source, destination, result, gene


def test_bundle_preserves_wire_documents_and_aliases(bundled):
    module, source, destination, manifest, gene = bundled
    assert manifest["bundle_tag"].endswith("-pubmed-20261106")
    assert manifest["pubmed_snapshot_date"] == "2026-11-06"
    paths = sorted((source / "genes").glob("*.json"))
    assert manifest["gene_count"] == len(paths)
    payload = destination / "genes.jsonl.gz"
    assert manifest["sha256"] == module.sha256(payload)
    assert manifest["size_bytes"] == payload.stat().st_size
    with gzip.open(payload, "rb") as stream:
        data = stream.read()
    assert data == b"".join(path.read_bytes() for path in paths)
    assert gene in [json.loads(line) for line in data.splitlines()]
    assert (source / "aliases.json").read_bytes() == (destination / "aliases.json").read_bytes()


def test_refresh_changes_tag_without_changing_release_id(bundled):
    module, source, destination, before, _ = bundled
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["source"]["generated_at"] = "2026-12-07T12:00:00+00:00"
    (source / "manifest.json").write_text(json.dumps(manifest))
    after = module.bundle(source.parent, destination)
    assert before["release_id"] == after["release_id"]
    assert before["bundle_tag"] != after["bundle_tag"]


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "upload",
        "corrupt",
        "cleanup",
        "existing",
        "stale",
        "subset",
        "skip",
        "skip_alterations",
    ],
)
def test_atomic_release_policy(bundled, monkeypatch, failure):
    module, _, directory, manifest, _ = bundled
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    if failure in {"subset", "skip", "skip_alterations"}:
        manifest["build"] = dict(
            genes=["TP53"] if failure == "subset" else None,
            skip_precheck=failure == "skip",
            skip_alterations=failure == "skip_alterations",
        )
        (directory / "manifest.json").write_text(json.dumps(manifest))
    calls = []

    def gh(*args):
        calls.append(args)
        if "--slurp" in args:
            return json.dumps(
                [[dict(tag_name=manifest["bundle_tag"], draft=failure == "stale", id=7)]]
                if failure in {"existing", "stale"}
                else [[]]
            )
        if "DELETE" in args and failure == "cleanup":
            raise RuntimeError("cleanup failed")
        if "upload" in args and failure in {"upload", "cleanup"}:
            raise RuntimeError("upload failed")
        if "download" in args:
            target = Path(args[-1])
            for asset in directory.iterdir():
                (target / asset.name).write_bytes(asset.read_bytes())
            if failure == "corrupt":
                (target / "aliases.json").write_text("corrupt")
        return '{"id":42}'

    monkeypatch.setattr(publisher, "gh", gh)
    if failure in {"upload", "corrupt", "cleanup", "stale", "subset", "skip", "skip_alterations"}:
        match = "stale draft 7; delete it" if failure == "stale" else None
        if failure == "cleanup":
            match = "upload failed"
        with pytest.raises((ValueError, RuntimeError), match=match):
            publisher.publish(directory)
    else:
        publisher.publish(directory)
    published = [call for call in calls if "draft=false" in call]
    assert bool(published) == (failure is None)
    for call in calls:
        if "draft=true" in call or "draft=false" in call:
            assert "make_latest=false" in call and "prerelease=false" in call
    deleted = [call for call in calls if "DELETE" in call]
    assert bool(deleted) == (failure in {"upload", "corrupt", "cleanup"})
    if deleted:
        assert deleted[0][-1].endswith("/releases/42")
    if failure in {"existing", "stale"}:
        assert len(calls) == 1
    created = [call for call in calls if "draft=true" in call]
    for call in created:
        assert f"target_commitish={'a' * 40}" in call


def test_discovery_paginates_filters_and_uses_published_at(monkeypatch):
    checker = load("check_build")
    pages = [
        [
            dict(tag_name="v99", draft=False, published_at="2027-01-01"),
            dict(tag_name="expression-stats-draft", draft=True, published_at="2027-01-01"),
            dict(
                tag_name="expression-stats-old",
                draft=False,
                published_at="2026-11-01",
                assets=[dict(name="manifest.json", id=1)],
            ),
        ],
        [
            dict(
                tag_name="expression-stats-refresh",
                draft=False,
                published_at="2026-12-01",
                assets=[dict(name="manifest.json", id=2)],
            )
        ],
    ]
    calls = []

    def output(args, **kwargs):
        calls.append(args)
        return json.dumps(pages if "--paginate" in args else {"release_id": "same-inputs"})

    monkeypatch.setattr(checker.subprocess, "check_output", output)
    assert checker.published_manifest() == {"release_id": "same-inputs"}
    assert calls[1][-1].endswith("/assets/2")


@pytest.mark.parametrize("status", ["failed", "skipped_subset", "missing"])
def test_publisher_rejects_unpassed_controls_before_any_github_call(bundled, monkeypatch, status):
    _, _, directory, manifest, _ = bundled
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    if status == "missing":
        manifest.pop("controls")
    else:
        manifest["controls"]["status"] = status
    (directory / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(publisher, "gh", lambda *args: pytest.fail("Must not contact GitHub"))
    with pytest.raises(ValueError, match="controls report"):
        publisher.publish(directory)


def rebundle(module, tmp_path, panel, manifest, edit=None):
    """Bundle synthetic gene documents for `panel`; `edit(genes_dir)` may tamper after the gate."""
    source = tmp_path / "custom" / "v1"
    codes = panel_codes(panel)
    report = synthesize(panel, codes, source / "genes", manifest["release_id"])
    if edit is not None:
        edit(source / "genes")
    manifest = dict(manifest, cancer_types=[dict(oncotree_code=code) for code in codes])
    for key in ("bundle_tag", "pubmed_snapshot_date", "gene_count", "sha256", "size_bytes"):
        manifest.pop(key, None)
    manifest["controls"] = {key: value for key, value in report.items() if key != "controls"}
    (source / "controls-report.json").write_text(json.dumps(report))
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "aliases.json").write_text("{}\n")
    destination = tmp_path / "custom-bundle"
    return destination, module.bundle(source.parent, destination), report


@pytest.mark.parametrize("invalid", [None, "positive", "negative", "missing", "total"])
def test_publisher_derives_required_counts_from_panel(bundled, monkeypatch, tmp_path, invalid):
    module, _, _, manifest, _ = bundled
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    panel = {
        "controls": [
            dict(id="p1", gene="A", tcga="TA", expected=True),
            dict(id="p2", gene="B", tcga="TA", expected=True),
            dict(id="n1", gene="C", tcga="TA", expected=False),
            dict(id="borderline", gene="D", tcga="TA", expected=None),
        ]
    }
    panel_path = tmp_path / "panel.yaml"
    panel_path.write_text(json.dumps(panel))
    monkeypatch.setattr(publisher, "PANEL_PATH", panel_path)
    directory, manifest, report = rebundle(module, tmp_path, panel, manifest)
    report["controls_sha256"] = module.sha256(panel_path)
    if invalid is not None:
        key = {
            "positive": "positive_passed",
            "negative": "negative_passed",
            "missing": "missing",
            "total": "negative_total",
        }[invalid]
        report[key] += 1
    manifest["controls"] = {key: value for key, value in report.items() if key != "controls"}
    (directory / "manifest.json").write_text(json.dumps(manifest))
    (directory / "controls-report.json").write_text(json.dumps(report))
    calls = []

    def gh(*args):
        calls.append(args)
        return json.dumps([[dict(tag_name=manifest["bundle_tag"], draft=False)]])

    monkeypatch.setattr(publisher, "gh", gh)
    if invalid:
        with pytest.raises(ValueError, match="controls report"):
            publisher.publish(directory)
        assert calls == []
    else:
        publisher.publish(directory)
        assert len(calls) == 1  # A valid differently sized panel reaches published-tag discovery.


def tamper_alteration(genes_dir):
    """Flip ERBB2/BRCA Copy Number Alteration to not enriched after the gate ran."""
    path = genes_dir / "ERBB2.json"
    document = json.loads(path.read_text())
    for record in document["cancer_types"]:
        if record["oncotree_code"] == "BRCA":
            entry = record["alterations"]["categories"]["Copy Number Alteration"]
            entry.update(status="not_enriched", enriched_directions=[])
    path.write_text(json.dumps(document) + "\n")


def tamper_expression(genes_dir):
    path = genes_dir / "ALB.json"
    document = json.loads(path.read_text())
    for record in document["cancer_types"]:
        if record["oncotree_code"] == "LIHC":
            record["stats_pass"] = False
    path.write_text(json.dumps(document) + "\n")


def tamper_negative(genes_dir):
    """Make a TTN Point Mutation negative control enriched."""
    path = genes_dir / "TTN.json"
    document = json.loads(path.read_text())
    entry = document["cancer_types"][3]["alterations"]["categories"]["Point Mutation"]
    entry.update(status="enriched")
    path.write_text(json.dumps(document) + "\n")


@pytest.mark.parametrize(
    "case",
    [None, "tampered_alteration", "tampered_expression", "tampered_negative", "missing_gene"],
)
def test_publisher_rederives_controls_from_bundled_genes(bundled, monkeypatch, tmp_path, case):
    """A passing-looking report never vouches for a bundle whose gene results disagree."""
    module, _, _, manifest, _ = bundled
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    edit = {
        None: None,
        "tampered_alteration": tamper_alteration,
        "tampered_expression": tamper_expression,
        "tampered_negative": tamper_negative,
        "missing_gene": lambda genes_dir: (genes_dir / "MDM2.json").unlink(),
    }[case]
    directory, manifest, report = rebundle(module, tmp_path, PANEL, manifest, edit)
    assert report["status"] == "passed"  # the copied report and counters still look clean
    calls = []

    def gh(*args):
        calls.append(args)
        return json.dumps([[dict(tag_name=manifest["bundle_tag"], draft=False)]])

    monkeypatch.setattr(publisher, "gh", gh)
    if case is None:
        publisher.publish(directory)
        assert len(calls) == 1
    else:
        with pytest.raises(ValueError, match="control recheck"):
            publisher.publish(directory)
        assert calls == []


def test_incomplete_precheck_cannot_publish_even_with_expiry(bundled, monkeypatch):
    _, _, directory, manifest, _ = bundled
    manifest["source"]["precheck_incomplete"] = True
    (directory / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    monkeypatch.setattr(publisher, "gh", lambda *args: pytest.fail("Must not contact GitHub"))
    with pytest.raises(ValueError, match="completed PubMed"):
        publisher.publish(directory)
