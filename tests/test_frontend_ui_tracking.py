"""Runs tests/frontend/test_ui_tracking.js under pytest.

Exercises src/static/tracking.js (contract: docs/ui-events.md) with a fake
clock, IntersectionObserver, DOM, fetch and sendBeacon, plus app.js's call
sites through the node + vm harness (tests/frontend/openevidence_gate_harness.js).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_ui_tracking.js"


def test_ui_tracking_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")

    result = subprocess.run(
        [node, str(_TEST_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 0, (
        f"frontend ui tracking tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
