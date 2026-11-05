"""Guard tests keeping the expression-context module optional and removable.

(a) Import boundary: nothing under src/ outside src/expression_context/
    references the module, except the single import + registration call in
    src/main.py.
(b) Flag off (the default): the app boots, the routes 404, the module's table
    DDL is never executed and no outbound/artifact code is even imported.
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

from src.expression_context import register_expression_context
from src.expression_context.settings import ExpressionContextSettings
from src.expression_context.store import InMemoryExpressionContextStore, MySQLExpressionContextStore

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
MODULE_DIR = SRC / "expression_context"
MODULE = "src.expression_context"
PREFIX = "/v1/expression-context"


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


@pytest.fixture
def ddl_recorder(monkeypatch):
    """Record any attempt by the module to open its pool or run its DDL."""
    calls = []

    async def create(cls, maxsize=2):
        calls.append("create_pool")
        store = InMemoryExpressionContextStore()
        original = store.ensure_schema

        async def ensure_schema():
            calls.append("ensure_schema")
            await original()

        store.ensure_schema = ensure_schema
        return store

    async def ensure_schema(self):
        calls.append("ensure_schema")

    monkeypatch.setattr(MySQLExpressionContextStore, "create", classmethod(create))
    monkeypatch.setattr(MySQLExpressionContextStore, "ensure_schema", ensure_schema)
    return calls


def _expression_context_flag_set() -> bool:
    return os.environ.get("EXPRESSION_CONTEXT_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}


@pytest.mark.skipif(_expression_context_flag_set(), reason="EXPRESSION_CONTEXT_ENABLED set in env")
def test_flag_off_app_boots_routes_404_and_no_ddl(monkeypatch, ddl_recorder):
    from src import main

    fake_run_store = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(main, "RunStore", SimpleNamespace(create=AsyncMock(return_value=fake_run_store)))

    assert _module_paths(main.app) == []
    with TestClient(main.app) as client:
        assert client.get("/health").status_code in (200, 404)  # booted
        assert client.post(f"{PREFIX}/jobs", json={"gene": "ERBB2"}).status_code == 404
        assert client.get(f"{PREFIX}/jobs/abc").status_code == 404
        assert client.get(f"{PREFIX}/genes/ERBB2").status_code == 404
        assert client.get(f"{PREFIX}/genes").status_code == 404
        assert getattr(main.app.state, "expression_context", None) is None
    assert ddl_recorder == []


def test_flag_on_default_factory_creates_table(ddl_recorder, monkeypatch):
    """Counterpart of the flag-off test: the DDL path exists and runs only when enabled."""
    app = FastAPI()
    settings = ExpressionContextSettings(enabled=True, artifact_base_url="https://stats.test")
    assert register_expression_context(app, settings=settings)
    assert sorted(_module_paths(app)) == [
        f"{PREFIX}/genes", f"{PREFIX}/genes/{{gene}}", f"{PREFIX}/jobs", f"{PREFIX}/jobs/{{job_id}}",
    ]
    with TestClient(app):
        assert ddl_recorder == ["create_pool", "ensure_schema"]
        assert app.state.expression_context is not None


@pytest.mark.skipif(_expression_context_flag_set(), reason="EXPRESSION_CONTEXT_ENABLED set in env")
def test_flag_off_imports_no_module_internals():
    """Importing the app with the flag off loads only the module's settings."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("EXPRESSION_CONTEXT_")}
    code = (
        "import sys, src.main; "
        "print(sorted(m for m in sys.modules if m.startswith('src.expression_context')))"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
        timeout=120,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    loaded = out.stdout.strip().splitlines()[-1]
    assert loaded == "['src.expression_context', 'src.expression_context.settings']"
