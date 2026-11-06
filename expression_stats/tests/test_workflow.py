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
