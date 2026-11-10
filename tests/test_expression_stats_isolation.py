"""Keep application imports independent of the removable statistics producer."""

import ast
from pathlib import Path


def test_application_does_not_import_expression_stats():
    repo = Path(__file__).resolve().parents[1]
    if not (repo / "expression_stats").is_dir():
        return
    forbidden = {"expression_stats", "gene_expression_relevance"}
    # Scan files regardless of their import name (including PYTHONPATH=src aliases).
    for path in (repo / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
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
                names = [str(node.args[0].value)]
            for name in names:
                assert name.lstrip(".").split(".")[0] not in forbidden, f"{path} imports {name}"
