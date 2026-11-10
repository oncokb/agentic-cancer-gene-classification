"""Guard tests keeping the expression-context module optional and removable.

(a) Import boundary: nothing under src/ outside src/expression_context/
    references the module, except the single import + registration call in
    src/main.py.
(b) Flag off (the default): the app boots, the API routes and the stats page
    404, the main page has no "Expression stats" nav entry, none of the
    module's tables is created, and nothing calls out (GitHub included) — the
    module's internals aren't even imported.
(c) The core UI (src/static/) never references the module; its page lives in
    src/expression_context/static/ and is removed with the package.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.expression_context import bundle_store as bundle_store_module
from src.expression_context import register_expression_context
from src.expression_context.bundle_store import InMemoryBundleStore
from src.expression_context.settings import ExpressionContextSettings
from src.expression_context.store import InMemoryExpressionContextStore, MySQLExpressionContextStore
from src.expression_context.web import NAV_LINK_ID
from tests.expression_context_helpers import LIST_URL

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_DIR = SRC / "expression_context"
MODULE = "src.expression_context"
PREFIX = "/v1/expression-context"
PAGE = "/expression-context"
INDEX_HTML = SRC / "static" / "index.html"


def _module_paths(app) -> list[str]:
    # app.router.routes can hold lazily-included routers without a .path, so
    # use the generated OpenAPI paths (rebuilt fresh, never the cached schema).
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(title="t", version="0", routes=app.routes)
    return [path for path in schema.get("paths", {}) if path.startswith(PREFIX)]


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imported_modules(path: Path) -> list[tuple[int, str]]:
    """(lineno, absolute module) for every import in ``path``, relative ones resolved."""
    tree = ast.parse(path.read_text(), filename=str(path))
    package = _module_name(path)
    if path.name != "__init__.py":
        package = package.rpartition(".")[0]
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found.append((node.lineno, module))
            found += [(node.lineno, f"{module}.{alias.name}") for alias in node.names]
        elif isinstance(node, ast.Call):
            # importlib.import_module("src.expression_context...") / __import__(...)
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append((node.lineno, arg.value))
    return found


def _references_module(name: str) -> bool:
    return name == MODULE or name.startswith(MODULE + ".")


def test_import_boundary():
    offenders = []
    main_refs = []
    for path in sorted(SRC.rglob("*.py")):
        if MODULE_DIR in path.parents:
            continue
        refs = [(line, name) for line, name in _imported_modules(path) if _references_module(name)]
        if path == SRC / "main.py":
            main_refs = refs
        elif refs:
            offenders.append((str(path.relative_to(ROOT)), refs))
        # Belt and braces: no textual reference either (e.g. dynamic imports).
        if path != SRC / "main.py" and "expression_context" in path.read_text():
            offenders.append((str(path.relative_to(ROOT)), "mentions expression_context"))
    assert offenders == [], f"modules outside {MODULE} import it: {offenders}"

    # main.py: exactly `from src.expression_context import register_expression_context`.
    assert {name for _, name in main_refs} == {MODULE, f"{MODULE}.register_expression_context"}
    assert len({line for line, _ in main_refs}) == 1


def _is_private(name: str) -> bool:
    return name.startswith("_") and not (name.startswith("__") and name.endswith("__"))


def _is_acgc_core(module: str) -> bool:
    return (module == "src" or module.startswith("src.")) and not _references_module(module)


def _dotted(node: ast.AST) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _private_core_uses(path: Path) -> list[tuple[int, str]]:
    """Private (underscore) names the module takes from ACGC core: imported
    directly (``from src.x import _y``), or reached through an imported core
    module/object (``from src.pipeline import cache; cache._get_client()``,
    ``import src.pipeline.cache; src.pipeline.cache._client``)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    core_bindings: dict[str, str] = {}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and not node.level and _is_acgc_core(node.module or ""):
            for alias in node.names:
                if _is_private(alias.name):
                    found.append((node.lineno, f"{node.module}.{alias.name}"))
                core_bindings[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_acgc_core(alias.name):
                    if any(_is_private(part) for part in alias.name.split(".")):
                        found.append((node.lineno, alias.name))
                    core_bindings[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0]
                    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_private(node.attr):
            dotted = _dotted(node)
            if dotted is None:
                continue
            root = dotted.split(".")[0]
            if root in core_bindings:
                found.append((node.lineno, dotted))
    return found


def test_module_uses_no_private_acgc_names():
    """The module may use ACGC internals only through public names."""
    offenders = {
        str(path.relative_to(ROOT)): uses
        for path in sorted(MODULE_DIR.rglob("*.py"))
        if (uses := _private_core_uses(path))
    }
    assert offenders == {}, f"private ACGC names used by {MODULE}: {offenders}"


def test_private_name_check_catches_violations(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "from src.pipeline import cache\n"
        "from src.pipeline.run_store import _mysql_connection_kwargs\n"
        "import src.pipeline.cache as core_cache\n"
        "from src.expression_context.store import _utc\n"
        "def f(self):\n"
        "    cache._get_client()\n"
        "    core_cache._client\n"
        "    self._private_is_fine\n"
        "    _utc\n"
    )
    names = sorted(name for _, name in _private_core_uses(probe))
    assert names == [
        "cache._get_client",
        "core_cache._client",
        "src.pipeline.run_store._mysql_connection_kwargs",
    ]


def test_main_has_exactly_one_registration_call():
    source = (SRC / "main.py").read_text()
    tree = ast.parse(source)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "register_expression_context"
    ]
    assert len(calls) == 1
    assert [arg.id for arg in calls[0].args if isinstance(arg, ast.Name)] == ["app"]
    # Exactly two lines (import + call) mention the module in main.py.
    assert len([ln for ln in source.splitlines() if "expression_context" in ln]) == 2


def test_core_config_and_dependencies_untouched():
    from src.config import Settings

    assert not [name for name in Settings.model_fields if "expression" in name]
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    all_deps = " ".join(deps["dependencies"] + sum(deps["optional-dependencies"].values(), []))
    for heavy in ("numpy", "scipy", "pandas"):
        assert heavy not in all_deps.lower()
    for path in MODULE_DIR.rglob("*.py"):
        for _, name in _imported_modules(path):
            assert name.split(".")[0] not in {"numpy", "scipy", "pandas"}, (path, name)


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv("EXPRESSION_CONTEXT_ENABLED", raising=False)
    assert ExpressionContextSettings(_env_file=None).enabled is False


def test_register_is_noop_when_disabled():
    app = FastAPI()
    before_routes = list(app.router.routes)
    before_lifespan = app.router.lifespan_context
    assert register_expression_context(app, settings=ExpressionContextSettings(enabled=False)) is False
    assert app.router.routes == before_routes
    assert app.router.lifespan_context is before_lifespan


def test_core_ui_does_not_reference_the_module():
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((SRC / "static").rglob("*"))
        if path.is_file() and path.suffix in {".html", ".js", ".css"}
        and any(word in path.read_text(errors="ignore") for word in ("expression-context", "expression_context"))
    ]
    assert offenders == []
    assert sorted(p.name for p in (MODULE_DIR / "static").iterdir()) == [
        "expression-context.css", "expression-context.html", "expression-context.js",
    ]


@pytest.fixture
def ddl_recorder(monkeypatch):
    """Record any attempt by the module to open its pool or run any of its DDL."""
    calls = []

    async def create(cls, maxsize=2):
        calls.append("create_pool")
        store = InMemoryExpressionContextStore()
        store.pool = None
        original = store.ensure_schema

        async def ensure_schema():
            calls.append("ensure_schema")
            await original()

        store.ensure_schema = ensure_schema
        return store

    async def ensure_schema(self):
        calls.append("ensure_schema")

    class RecordingBundleStore(InMemoryBundleStore):
        def __init__(self, pool=None):
            super().__init__()

        async def ensure_schema(self):
            calls.append("ensure_bundle_schema")
            await super().ensure_schema()

    async def bundle_ensure_schema(self):
        calls.append("ensure_bundle_schema")

    monkeypatch.setattr(MySQLExpressionContextStore, "create", classmethod(create))
    monkeypatch.setattr(MySQLExpressionContextStore, "ensure_schema", ensure_schema)
    monkeypatch.setattr(bundle_store_module.MySQLBundleStore, "ensure_schema", bundle_ensure_schema)
    monkeypatch.setattr(bundle_store_module, "MySQLBundleStore", RecordingBundleStore)
    return calls


def _boot_flag_off_app(monkeypatch):
    """An app wired exactly like src.main (its lifespan + the registration
    call), with the flag forced off whatever the surrounding env says."""
    from src import main

    monkeypatch.setenv("EXPRESSION_CONTEXT_ENABLED", "false")
    fake_run_store = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(main, "RunStore", SimpleNamespace(create=AsyncMock(return_value=fake_run_store)))
    app = FastAPI(lifespan=main.lifespan)

    @app.get("/")
    async def index():
        from fastapi.responses import FileResponse

        return FileResponse(INDEX_HTML)

    assert register_expression_context(app) is False
    return app


@pytest.mark.httpx_mock(assert_all_requests_were_expected=True)
def test_flag_off_app_boots_routes_404_no_ddl_no_outbound(monkeypatch, ddl_recorder, httpx_mock):
    # No responses are registered: any outbound httpx request during boot or
    # the requests below fails the test (pytest-httpx asserts at teardown).
    app = _boot_flag_off_app(monkeypatch)
    assert _module_paths(app) == []
    with TestClient(app) as client:
        assert client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).status_code == 404
        assert client.get(f"{PREFIX}/jobs/abc").status_code == 404
        assert client.get(f"{PREFIX}/genes/ERBB2").status_code == 404
        assert client.get(f"{PREFIX}/genes").status_code == 404
        assert client.get(f"{PREFIX}/stats/ERBB2").status_code == 404
        assert client.get(f"{PREFIX}/bundle").status_code == 404
        assert client.get(PAGE).status_code == 404
        assert client.get(f"{PAGE}/static/expression-context.js").status_code == 404
        home = client.get("/")
        assert home.status_code == 200 and NAV_LINK_ID not in home.text
        assert getattr(app.state, "expression_context", None) is None
    assert ddl_recorder == []
    assert httpx_mock.get_requests() == []  # no GitHub (or any other) call


def test_flag_on_default_factory_creates_tables_and_checks_github(ddl_recorder, httpx_mock):
    """Counterpart of the flag-off tests: with the flag on (and no URL or
    token configured) the tables are created and the first bundle check runs
    at startup — against the mocked GitHub API."""
    import time

    httpx_mock.add_response(url=LIST_URL, json=[])  # repo has no bundles yet
    app = FastAPI()
    settings = ExpressionContextSettings(_env_file=None, enabled=True)
    assert register_expression_context(app, settings=settings)
    assert sorted(_module_paths(app)) == [
        f"{PREFIX}/bundle", f"{PREFIX}/genes", f"{PREFIX}/genes/{{gene}}", f"{PREFIX}/jobs",
        f"{PREFIX}/jobs/{{job_id}}", f"{PREFIX}/stats/{{gene}}",
    ]
    with TestClient(app) as client:
        assert ddl_recorder == ["create_pool", "ensure_schema", "ensure_bundle_schema"]
        assert app.state.expression_context is not None
        deadline = time.monotonic() + 5
        while not httpx_mock.get_requests() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert [str(r.url) for r in httpx_mock.get_requests()] == [LIST_URL]
        assert client.get(f"{PREFIX}/stats/ERBB2").status_code == 503
        assert client.get(PAGE).status_code in (200, 303)


_REAL_MAIN_FLAG_OFF_SCRIPT = r"""
import json, sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

outbound = []
for transport in (httpx.HTTPTransport, httpx.AsyncHTTPTransport):
    def _record(self, request, *a, **k):
        outbound.append(str(request.url))
        raise httpx.ConnectError("outbound disabled in test", request=request)
    if transport is httpx.HTTPTransport:
        transport.handle_request = _record
    else:
        async def _arecord(self, request, *a, **k):
            return _record(self, request)
        transport.handle_async_request = _arecord

from src import main
# Snapshot before this script imports the store module for patching.
loaded = sorted(m for m in sys.modules if m.startswith("src.expression_context"))

from src.expression_context import store as ec_store
from src.expression_context import bundle_store as ec_bundle_store
ddl = []
async def _create(cls, maxsize=2):
    ddl.append("create_pool")
async def _ensure(self):
    ddl.append("ensure_schema")
ec_store.MySQLExpressionContextStore.create = classmethod(_create)
ec_store.MySQLExpressionContextStore.ensure_schema = _ensure
ec_bundle_store.MySQLBundleStore.ensure_schema = _ensure

main.RunStore = SimpleNamespace(create=AsyncMock(return_value=SimpleNamespace(close=AsyncMock())))
main.settings.auth_enabled = False  # so "/" serves the page instead of redirecting
from fastapi.testclient import TestClient
prefix = "/v1/expression-context"
with TestClient(main.app) as client:
    codes = [
        client.post(prefix + "/jobs", json={"gene": "ERBB2"}).status_code,
        client.get(prefix + "/jobs/abc").status_code,
        client.get(prefix + "/genes/ERBB2").status_code,
        client.get(prefix + "/genes").status_code,
        client.get(prefix + "/stats/ERBB2").status_code,
        client.get(prefix + "/bundle").status_code,
        client.get("/expression-context").status_code,
        client.get("/expression-context/static/expression-context.js").status_code,
    ]
    home = client.get("/")
    nav = {"status": home.status_code, "has_link": "nav-expression-context" in home.text,
           "has_tabs": 'class="app-tabs"' in home.text}
print(json.dumps({"codes": codes, "ddl": ddl, "outbound": outbound, "loaded": loaded, "nav": nav}))
"""


def test_real_main_flag_off_boots_404_no_ddl_no_outbound_no_internals():
    """src.main itself, imported fresh in a subprocess with the flag forced off
    (so this never depends on, or skips because of, the test runner's env)."""
    import json

    env = {k: v for k, v in os.environ.items() if not k.startswith("EXPRESSION_CONTEXT_")}
    env["EXPRESSION_CONTEXT_ENABLED"] = "false"  # beats any .env value
    out = subprocess.run(
        [sys.executable, "-c", _REAL_MAIN_FLAG_OFF_SCRIPT], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=180,
    )
    assert out.returncode == 0, out.stderr[-3000:]
    report = json.loads(out.stdout.strip().splitlines()[-1])
    assert report["codes"] == [404] * 8
    assert report["nav"] == {"status": 200, "has_link": False, "has_tabs": True}
    assert report["ddl"] == []
    assert report["outbound"] == []
    assert report["loaded"] == ["src.expression_context", "src.expression_context.settings"]
