"""The "Expression stats" page: route + login redirect, module-owned assets,
the nav tab injected into ACGC's main page (only when enabled), and the
page's JS (Node fake-DOM test, like the other frontend tests)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.testclient import TestClient

from src.auth import AuthenticatedUser
from src.config import settings
from src.expression_context.web import NAV_LINK, NAV_LINK_ID, inject_nav_link
from tests.auth_helpers import session_token_for
from tests.test_expression_context_api import Harness

ROOT = Path(__file__).resolve().parents[1]
INDEX_HTML = ROOT / "src" / "static" / "index.html"

pytestmark = pytest.mark.no_real_redis


def _harness_with_home() -> Harness:
    harness = Harness()

    @harness.app.get("/")
    async def index():
        return FileResponse(INDEX_HTML)

    @harness.app.get("/runs")
    async def runs():
        return HTMLResponse('<nav><button id="nav-benchmark">B</button></nav>')

    @harness.app.get("/broken")
    async def broken():
        return HTMLResponse("nope", status_code=500)

    return harness


# --------------------------------------------------------------------- page


def test_page_signed_out_redirects_through_login(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "expression-context-page-secret")
    with TestClient(Harness().app) as client:
        response = client.get("/expression-context?gene=ERBB2", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login?redirect_to=%2Fexpression-context%3Fgene%3DERBB2"


def test_page_signed_in_serves_page(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "auth_secret_key", "expression-context-page-secret")
    user = AuthenticatedUser(email="user@mskcc.org", name="User", domain="mskcc.org")
    with TestClient(Harness().app) as client:
        client.cookies.set(settings.auth_cookie_name, session_token_for(user))
        response = client.get("/expression-context")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-cache"
    assert '<script src="/expression-context/static/expression-context.js"></script>' in response.text
    assert 'href="/static/styles.css"' in response.text


def test_page_auth_disabled(monkeypatch):
    monkeypatch.setattr(settings, "auth_enabled", False)
    with TestClient(Harness().app) as client:
        assert client.get("/expression-context").status_code == 200


@pytest.mark.parametrize(
    "asset,content_type",
    [("expression-context.js", "text/javascript"), ("expression-context.css", "text/css")],
)
def test_assets_served_with_no_cache(asset, content_type):
    with TestClient(Harness().app) as client:
        response = client.get(f"/expression-context/static/{asset}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith(content_type)
    assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize(
    "path",
    [
        "/expression-context/static/expression-context.html",
        "/expression-context/static/service.py",
        "/expression-context/static/..%2Fservice.py",
        "/expression-context/static/%2E%2E%2F__init__.py",
    ],
)
def test_only_allow_listed_assets_are_served(path):
    with TestClient(Harness().app) as client:
        assert client.get(path).status_code == 404


def test_page_js_builds_dom_with_text_nodes_only():
    content = (ROOT / "src/expression_context/static/expression-context.js").read_text()
    assert "innerHTML" not in content and "insertAdjacentHTML" not in content
    assert "textContent" in content


# ---------------------------------------------------------------- nav entry


def test_main_page_gets_the_nav_tab_when_enabled():
    harness = _harness_with_home()
    original = INDEX_HTML.read_text()
    with TestClient(harness.app) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.text.count(NAV_LINK) == 1
    benchmark = response.text.index('id="nav-benchmark"')
    tab = response.text.index(NAV_LINK)
    assert benchmark < tab < response.text.index("</nav>", benchmark)
    assert int(response.headers["content-length"]) == len(response.content)
    assert "etag" not in response.headers
    # Everything else in the page is byte-for-byte ACGC's index.html.
    assert response.text.replace("\n          " + NAV_LINK, "", 1) == original


def test_nav_injection_touches_only_get_slash_html_200():
    harness = _harness_with_home()
    with TestClient(harness.app) as client:
        assert NAV_LINK_ID not in client.get("/runs").text  # another page
        assert client.get("/broken").text == "nope"  # non-200 passes through
        head = client.head("/")
        assert head.status_code in (200, 405) and NAV_LINK_ID not in head.text


def test_inject_nav_link_is_idempotent_and_safe():
    html = b'<nav><button id="nav-benchmark" type="button">B</button></nav>'
    once = inject_nav_link(html)
    assert once.count(NAV_LINK_ID.encode()) == 1
    assert inject_nav_link(once) == once
    assert inject_nav_link(b"<p>no tabs</p>") == b"<p>no tabs</p>"
    assert inject_nav_link(b"\xff\xfe") == b"\xff\xfe"


def test_streamed_and_oversized_bodies():
    app = FastAPI()
    chunks = [b'<nav><button id="nav-benchmark">B</button>', b"</nav>"]

    @app.get("/")
    async def index():
        async def body():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(body(), media_type="text/html")

    from src.expression_context.web import NavLinkMiddleware

    app.add_middleware(NavLinkMiddleware)
    with TestClient(app) as client:
        assert client.get("/").text.count(NAV_LINK_ID) == 1
        chunks[:] = [b'<button id="nav-benchmark">B</button>' + b"x" * (3 * 1024 * 1024)]
        big = client.get("/")
    assert big.status_code == 200 and NAV_LINK_ID not in big.text
    assert len(big.content) == len(chunks[0])


# ----------------------------------------------------------------- frontend


def test_expression_context_page_frontend():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on PATH")
    result = subprocess.run(
        [node, str(ROOT / "tests" / "frontend" / "test_expression_context_page.js")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, (
        f"expression-context page tests failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
