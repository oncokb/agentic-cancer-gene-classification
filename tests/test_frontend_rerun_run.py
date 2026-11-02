"""Runs tests/frontend/test_rerun_run.js under pytest.

Uses the node + vm harness in tests/frontend/openevidence_gate_harness.js to
exercise app.js's real Rerun controls on an opened saved run and the My runs
page hand-off.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_rerun_run.js"


def test_rerun_run_frontend():
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
        f"frontend rerun tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
