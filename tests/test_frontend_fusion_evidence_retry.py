"""Runs the failed fusion-evidence lookup retry UI integration tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_TEST_SCRIPT = Path(__file__).parent / "frontend" / "test_fusion_evidence_retry.js"


def test_fusion_evidence_retry_frontend():
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
        f"frontend fusion evidence retry tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
