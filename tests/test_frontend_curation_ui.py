"""Runs tests/frontend/test_curation_ui.js under pytest.

Uses tests/frontend/curation_harness.js (a fake DOM with a real element tree)
to drive src/static/curation.js and app.js's renderers: edit controls are
hidden from viewers and shown to curators (even on empty sections); the PMID
editor validates, dedupes, reorders and caps; a re-evaluation job notifies,
polls and prefills the draft; invalid PMIDs render inline; save sends
expected_version and re-renders in place; 409/422 are handled; the curated
badge, curation flags, history and revert render; and fusions use the same flow.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_curation_ui.js"


def test_curation_ui_frontend():
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
        f"frontend curation UI tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
