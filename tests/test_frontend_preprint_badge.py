"""Runs tests/frontend/test_preprint_badge.js under pytest.

Uses the same node + vm harness as test_frontend_openevidence_gate.py (see
tests/frontend/openevidence_gate_harness.js) to exercise app.js's real
evidence-card rendering: a card flagged is_preprint shows a "Preprint – not
peer-reviewed" badge; peer-reviewed cards and cards cached before the field
existed show none.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_preprint_badge.js"


def test_preprint_badge_frontend():
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
        f"frontend preprint badge tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
