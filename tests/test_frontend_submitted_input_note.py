"""Runs tests/frontend/test_submitted_input_note.js under pytest.

Uses the same node + vm harness as test_frontend_openevidence_gate.py (see
tests/frontend/openevidence_gate_harness.js) to exercise app.js's real card
note: an input the server rewrote (e.g. an Ensembl transcript ID resolved to
TP53) is shown beside the resolved gene or fusion, with its warnings, as text.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_submitted_input_note.js"


def test_submitted_input_note_frontend():
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
        f"frontend submitted-input note tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
