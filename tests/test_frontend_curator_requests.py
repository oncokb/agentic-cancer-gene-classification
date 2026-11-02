"""Run the dependency-free frontend curator request suite."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_curator_requests_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    subprocess.run(
        [node, "tests/frontend/test_curator_requests.js"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
    )
