"""Runs tests/frontend/test_gene_index_filter.js (results sidebar filter box) under node."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_gene_index_filter.js"


def test_gene_index_filter_frontend():
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
        f"frontend gene index filter tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
