"""Runs tests/frontend/test_curated_actionability_badge.js under pytest (same
node + vm harness as test_frontend_preprint_badge.py): curator-verified
clinical actionability shows a "Curator-verified" badge instead of a score."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_curated_actionability_badge.js"


def test_curated_actionability_badge_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")

    result = subprocess.run([node, str(_TEST_SCRIPT)], capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, (
        f"frontend curated badge tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
