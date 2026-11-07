"""Enforce both import boundaries without importing either application."""

import ast
import sys
from pathlib import Path

import pytest


def imports(path, *, skip_relative=False):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif (
            isinstance(node, ast.ImportFrom)
            and node.module
            and (not skip_relative or node.level == 0)
        ):
            yield node.module
        elif (
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id in {"__import__", "import_module"}
                or isinstance(node.func, ast.Attribute)
                and node.func.attr == "import_module"
            )
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ):
            yield str(node.args[0].value)


def test_pipeline_and_application_imports_are_isolated():
    pipeline = Path(__file__).resolve().parents[1]
    repo = pipeline.parent
    # The wheel exports only src; adding src/ to sys.path also exposes its
    # immediate modules/packages by bare names. Reject those aliases as well.
    app_names = {"src"} | {
        path.stem
        for path in (repo / "src").iterdir()
        if not path.name.startswith("_")
        and path.stem not in sys.stdlib_module_names
        and (path.is_dir() or path.suffix == ".py")
    }
    for root, forbidden in [
        (pipeline, app_names),
        (
            repo / "src",
            {
                "expression_stats",
                "gene_expression_relevance",
            },
        ),
    ]:
        for path in root.rglob("*.py"):
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            for name in imports(path, skip_relative=root == pipeline):
                assert name.split(".")[0] not in forbidden, f"{path} imports {name}"


def test_root_guard_survives_producer_removal_and_rejects_imports(tmp_path):
    import runpy

    guard = runpy.run_path(
        str(Path(__file__).resolve().parents[2] / "tests/test_expression_stats_isolation.py")
    )["test_application_does_not_import_expression_stats"]
    guard.__globals__["__file__"] = str(tmp_path / "tests/test_isolation.py")
    source = tmp_path / "src"
    source.mkdir()
    module = source / "main.py"
    module.write_text("import expression_stats\n")
    guard()  # No producer directory: the root guard is independently removable.
    (tmp_path / "expression_stats").mkdir()
    for statement in [
        "import expression_stats",
        "from gene_expression_relevance import build",
        '__import__("expression_stats")',
        'importlib.import_module("gene_expression_relevance.build")',
        'import_module("expression_stats")',
    ]:
        module.write_text(statement + "\n")
        with pytest.raises(AssertionError):
            guard()


@pytest.mark.parametrize(
    "statement,allowed",
    [
        ("from .stats import x", True),
        ("import src", False),
        ("import json", True),
        ("import stats", False),
        ("from stats import x", False),
    ],
)
def test_pipeline_guard_distinguishes_relative_and_app_imports(
    tmp_path,
    monkeypatch,
    statement,
    allowed,
):
    pipeline = tmp_path / "expression_stats"
    (pipeline / "tests").mkdir(parents=True)
    (pipeline / "module.py").write_text(statement + "\n")
    source = tmp_path / "src"
    source.mkdir()
    (source / "stats.py").write_text("x = 1\n")
    (source / "json.py").write_text("# app name must not shadow the stdlib guard\n")
    monkeypatch.setitem(globals(), "__file__", str(pipeline / "tests/test_isolation.py"))
    if allowed:
        test_pipeline_and_application_imports_are_isolated()
    else:
        with pytest.raises(AssertionError, match="imports"):
            test_pipeline_and_application_imports_are_isolated()
