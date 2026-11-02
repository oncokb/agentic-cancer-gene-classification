"""Runs tests/frontend/test_gene_index_sidebar.js under pytest.

Uses the same node + vm harness as test_frontend_openevidence_gate.py (see
tests/frontend/openevidence_gate_harness.js) to exercise app.js's real
rebuildGeneIndex: every sidebar item carries a title tooltip, and the
.gene-index-item rule keeps flex-shrink: 0 so a long gene list scrolls
instead of squashing each item to a sliver.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_gene_index_sidebar.js"


def test_gene_index_sidebar_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")

    result = subprocess.run(
        [node, str(_TEST_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (
        f"frontend gene index sidebar tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
