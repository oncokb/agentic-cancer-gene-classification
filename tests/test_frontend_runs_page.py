"""The /runs page: route (login redirect) mirrors the API keys page, and runs.js is safe."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from src import main
from src.auth import AuthenticatedUser
from src.config import settings
from tests.auth_helpers import session_token_for

_STATIC_DIR = Path(__file__).parent.parent / "src" / "static"


def test_runs_page_signed_out_redirects_through_login_back_to_page(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "runs-page-test-secret")
    client = TestClient(main.app)
    response = client.get("/runs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/login")
    assert "redirect_to=%2Fruns" in response.headers["location"]


def test_runs_page_signed_in_serves_page(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "runs-page-test-secret")
    client = TestClient(main.app)
    user = AuthenticatedUser(email="user@mskcc.org", name="User", domain="mskcc.org")
    client.cookies.set(settings.auth_cookie_name, session_token_for(user))
    response = client.get("/runs")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    assert '<script src="/static/runs.js"></script>' in response.text


def test_runs_page_auth_disabled_serves_page(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    client = TestClient(main.app)
    response = client.get("/runs", follow_redirects=False)
    assert response.status_code == 200


def test_runs_js_uses_textcontent_no_innerhtml():
    content = (_STATIC_DIR / "runs.js").read_text(encoding="utf-8")
    assert "innerHTML" not in content
    assert "textContent" in content


def test_runs_page_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(Path(__file__).parent / "frontend" / "test_runs_page.js")],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"frontend runs page tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
