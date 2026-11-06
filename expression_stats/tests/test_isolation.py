"""Enforce both import boundaries without importing either application."""

import ast
from pathlib import Path


def imports(path):
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            yield node.module
        elif (
            isinstance(node, ast.Call)
            and (
                isinstance(node.func, ast.Name)
                and node.func.id == "__import__"
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
    for root, forbidden in [
        (pipeline, {"src"}),
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
            for name in imports(path):
                assert name.split(".")[0] not in forbidden, f"{path} imports {name}"
