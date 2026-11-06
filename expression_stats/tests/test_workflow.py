"""Exercise the actual workflow credential guard without network or GitHub."""

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "genes,skip,key,allowed",
    [
        ("", "", "", False),
        ("", "false", "", False),
        (" , ", "false", "", False),
        ("", "false", "test-key", True),
        ("ALB", "false", "", False),
        ("", "true", "", True),
    ],
)
def test_full_precheck_guard(genes, skip, key, allowed):
    workflow = Path("../.github/workflows/expression-stats-release.yml").read_text()
    step = workflow.split("      - name: Validate full precheck credentials\n")[1].split(
        "      - uses: actions/setup-python"
    )[0]
    script = textwrap.dedent(step.split("        run: |\n")[1])
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        env={**os.environ, "INPUT_GENES": genes, "INPUT_SKIP": skip, "NCBI_API_KEY": key},
    )
    assert (result.returncode == 0) == allowed
    if not allowed:
        assert "require the NCBI_API_KEY secret" in result.stdout


@pytest.mark.parametrize(
    "genes,expected", [("", False), (" , ", False), (" \t ", False), (" ALB, TP53 ", True)]
)
def test_build_omits_empty_gene_argument(genes, expected):
    workflow = Path("../.github/workflows/expression-stats-release.yml").read_text()
    step = workflow.split("      - name: Build\n")[1].split("      - uses:")[0]
    script = textwrap.dedent(step.split("        run: |\n")[1])
    result = subprocess.run(
        ["bash", "-c", "python() { printf '%s\\n' \"$@\"; }\n" + script],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "INPUT_GENES": genes, "INPUT_SKIP": "true", "DATAHUB_COMMIT": "a" * 40},
    )
    args = result.stdout.splitlines()
    assert ("--genes" in args) == expected
    if expected:
        assert args[args.index("--genes") + 1] == genes


def test_release_workflow_limits_write_token_to_main_publisher():
    workflow = Path("../.github/workflows/expression-stats-release.yml").read_text()
    header, jobs = workflow.split("jobs:\n", 1)
    build, publish = jobs.split("  publish:\n", 1)
    assert "contents: read" in header and "contents: write" not in header + build
    assert "github.event_name" in header and "github.ref" in header
    assert (
        "concurrency:\n      group: expression-stats-pubmed\n      cancel-in-progress: false"
        in build
    )
    assert "contents: write" in publish
    assert "github.ref == 'refs/heads/main'" in publish
    assert "group: expression-stats-publish" in publish
    assert "actions/upload-artifact@v4" in build
    assert "actions/download-artifact@v4" in publish
    assert workflow.count("persist-credentials: false") == 2
    assert workflow.count("GH_TOKEN:") == 2
    check = build.split("      - name: Resolve pin and check published release")[1].split(
        "      - uses: actions/cache@v4"
    )[0]
    assert "GH_TOKEN:" in check
    assert "GH_TOKEN:" in publish.split("      - name: Publish verified full bundle")[1]


def test_pipeline_ci_checks_src_changes():
    workflow = Path("../.github/workflows/expression-stats-ci.yml").read_text()
    assert workflow.count("'src/**'") == 2


def test_branch_dispatch_always_builds_review_artifact(tmp_path, monkeypatch):
    import runpy

    checker = runpy.run_path("scripts/check_build.py")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/review")
    monkeypatch.setenv("INPUT_COMMIT", "a" * 40)
    monkeypatch.setenv("INPUT_GENES", "")
    monkeypatch.setenv("INPUT_SKIP", "false")
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    namespace = checker["main"].__globals__
    monkeypatch.setitem(namespace, "published_manifest", lambda: {})
    monkeypatch.setitem(namespace, "unchanged", lambda *args: True)
    checker["main"]()
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["changed"] == "true"
    assert values["deployable"] == "false"
