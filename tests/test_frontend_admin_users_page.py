"""Runs the /admin/users page's Node tests (admin-users.js and app.js's Users link)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_admin_users_page_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "frontend" / "test_admin_users_page.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"frontend admin users page tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
