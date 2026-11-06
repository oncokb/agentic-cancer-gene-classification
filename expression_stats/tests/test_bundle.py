import gzip
import importlib.util
import json
from pathlib import Path

import pytest


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
        release_id="datahub-aaaaaaaaaaaa-m2-bbbbbbbbbbbb",
        source=dict(generated_at="2026-11-06T12:00:00+00:00", precheck_expires_at="later"),
        build=dict(genes=None, skip_precheck=False),
    )
    (source / "manifest.json").write_text(json.dumps(manifest))
    (source / "aliases.json").write_text('{"P53":"TP53"}\n')
    gene = dict(release_id=manifest["release_id"], gene={"hugo_symbol": "TP53"})
    (source / "genes/TP53.json").write_text(json.dumps(gene) + "\n")
    destination = tmp_path / "bundle"
    result = module.bundle(source.parent, destination)
    return module, source, destination, result, gene


def test_bundle_preserves_wire_documents_and_aliases(bundled):
    module, source, destination, manifest, gene = bundled
    assert manifest["bundle_tag"].endswith("-pubmed-20261106")
    assert manifest["pubmed_snapshot_date"] == "2026-11-06"
    assert manifest["gene_count"] == 1
    payload = destination / "genes.jsonl.gz"
    assert manifest["sha256"] == module.sha256(payload)
    assert manifest["size_bytes"] == payload.stat().st_size
    with gzip.open(payload, "rb") as stream:
        data = stream.read()
    assert data == (source / "genes/TP53.json").read_bytes()
    assert [json.loads(line) for line in data.splitlines()] == [gene]
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
    ],
)
def test_atomic_release_policy(bundled, monkeypatch, failure):
    module, _, directory, manifest, _ = bundled
    monkeypatch.syspath_prepend(str(Path("scripts").resolve()))
    publisher = load("publish")
    monkeypatch.setenv("GITHUB_SHA", "a" * 40)
    if failure in {"subset", "skip"}:
        manifest["build"] = dict(
            genes=["TP53"] if failure == "subset" else None, skip_precheck=failure == "skip"
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
    if failure in {"upload", "corrupt", "cleanup", "stale", "subset", "skip"}:
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
