"""Runs tests/frontend/test_enrichment_tumor_context.js under pytest (same
node + vm harness as test_frontend_preprint_badge.py): tumor context from a
running annotation job is kept and sent with mid-run lazy enrichment."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_enrichment_tumor_context.js"


def test_enrichment_tumor_context_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")

    result = subprocess.run([node, str(_TEST_SCRIPT)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, (
        f"frontend enrichment tumor context tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
