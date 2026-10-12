"""Runs tests/frontend/test_curation_extended_ui.js under pytest.

Drives src/static/curation.js and app.js's hooks under the fake DOM in
tests/frontend/curation_harness.js: the cancer-associated call is prefilled
from the re-evaluation draft, can be flipped or left to the pipeline, is sent
in the PUT body and shown as "Curator-set"; fusion-partner precedent lookups
get the same edit -> re-evaluate -> review -> save flow and refresh in place.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_curation_extended_ui.js"


def test_curation_extended_ui_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")

    result = subprocess.run([node, str(_TEST_SCRIPT)], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, (
        f"frontend curation extended UI tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
