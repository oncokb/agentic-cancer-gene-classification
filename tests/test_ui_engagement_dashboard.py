"""Keep dashboard queries tied to the actual emitters, without importing the app."""

import ast
import copy
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = ROOT / "docs/datadog/ui_engagement_dashboard.json"


def source(path):
    return ast.parse((ROOT / path).read_text())


def string_constants(tree):
    return {
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def metric_contract():
    """Resolve metric constants and each emitter's tags, including local tag lists.

    Follow assignments/appends and helper return expressions rather than sharing
    one tag allowlist across unrelated metrics. DD_SERVICE is injected by the
    DogStatsD library; the deployment service is also the log factory's default.
    """
    oe = source("src/pipeline/openevidence_metrics.py")
    constants = {
        node.targets[0].id: node.value.value
        for node in oe.body if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    helpers = {node.name: node for node in oe.body if isinstance(node, ast.FunctionDef)}
    contract = {}
    for path in ("src/ui_events.py", "src/main.py", "src/pipeline/openevidence.py"):
        tree = source(path)
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            def tag_keys(expr, seen=frozenset()):
                if isinstance(expr, ast.Name):
                    if expr.id in seen:
                        return set()
                    values = []
                    for node in ast.walk(function):
                        if isinstance(node, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == expr.id for t in node.targets
                        ):
                            values.append(node.value)
                        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                and isinstance(node.func.value, ast.Name)
                                and node.func.value.id == expr.id and node.func.attr == "append"):
                            values.extend(node.args)
                    return set().union(*(tag_keys(v, seen | {expr.id}) for v in values))
                if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute):
                    helper = helpers.get(expr.func.attr)
                    if helper:
                        return set().union(*(
                            tag_keys(n.value, seen) for n in ast.walk(helper)
                            if isinstance(n, ast.Return) and n.value is not None
                        ))
                if isinstance(expr, ast.JoinedStr):
                    expr = expr.values[0]
                if isinstance(expr, ast.Constant) and isinstance(expr.value, str):
                    match = re.match(r"^([a-z_]+):", expr.value)
                    return {match[1]} if match else set()
                return set().union(*(tag_keys(child, seen) for child in ast.iter_child_nodes(expr)))

            for call in ast.walk(function):
                if not isinstance(call, ast.Call) or not call.args:
                    continue
                name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
                if name not in {"increment", "distribution"}:
                    continue
                arg = call.args[0]
                metric = arg.value if isinstance(arg, ast.Constant) else constants.get(getattr(arg, "attr", ""))
                if not isinstance(metric, str) or not metric.startswith(("ui.", "openevidence.")):
                    continue
                tags = next((kw.value for kw in call.keywords if kw.arg == "tags"), None)
                keys = tag_keys(tags) if tags is not None else set()
                contract.setdefault("acgc." + metric, set()).update(keys | {"service"})
    return contract


def walk_objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_objects(child)


def validate_dashboard(dashboard):
    contract = metric_contract()
    ui = source("src/ui_events.py")
    enums = {
        node.targets[0].id: set(string_constants(node.value))
        for node in ui.body if isinstance(node, ast.Assign)
        and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Subscript)
    }
    enum_tags = {tag: enums[name] for tag, name in (
        ("type", "EventType"), ("surface", "Surface"), ("section", "Section"), ("state", "State")
    )}
    event = next(node for node in ui.body if isinstance(node, ast.ClassDef) and node.name == "Event")
    log_fields = {node.target.id for node in event.body if isinstance(node, ast.AnnAssign)}
    variables = {v["name"]: v["prefix"] for v in dashboard["template_variables"]}
    assert variables == {"variant": "variant", "tab": "@item_id", "section": "section"}
    assert dashboard["layout_type"] == "ordered" and dashboard["widgets"]
    assert "agentic-cancer-gene-classification" in string_constants(source("src/main.py"))
    for obj in walk_objects(dashboard):
        if obj.get("data_source") == "metrics":
            query = obj["query"]
            match = re.fullmatch(r"sum:([\w.]+)\{([^}]+)\}(?: by \{([^}]+)\})?\.as_count\(\)", query)
            assert match, query
            metric, filters, groups = match.groups()
            assert metric in contract, metric
            keys = set((groups or "").split(",")) - {""}
            for term in filters.split(","):
                if term.startswith("$"):
                    keys.add(variables[term[1:]])
                else:
                    key, value = term.split(":", 1)
                    keys.add(key)
                    if metric == "acgc.ui.events" and key in enum_tags:
                        assert value in enum_tags[key], (key, value)
                    if key == "service":
                        assert value == "agentic-cancer-gene-classification"
            assert keys <= contract[metric], (metric, keys - contract[metric])
        elif obj.get("data_source") == "logs":
            query = obj["search"]["query"]
            assert '"ui_event"' in query
            fields = set(re.findall(r"@([\w.]+):", query))
            fields.update(group["facet"].removeprefix("@") for group in obj["group_by"])
            assert fields <= log_fields, fields - log_fields
            for variable in re.findall(r"\$(\w+)\.value", query):
                assert variable in variables


@pytest.mark.no_real_redis
def test_dashboard_queries_match_emitters():
    validate_dashboard(json.loads(DASHBOARD.read_text()))


@pytest.mark.no_real_redis
@pytest.mark.parametrize("replacement", [
    "sum:acgc.ui.imaginary{service:agentic-cancer-gene-classification}.as_count()",
    "sum:acgc.ui.events{tab:results}.as_count()",
    "sum:acgc.openevidence.lookups.started{section:openevidence}.as_count()",
    "sum:acgc.ui.events{type:expand}.as_count()",
    "sum:acgc.ui.events{service:agentic-cancer-gene-classification} by {gene}.as_count()",
])
def test_validator_rejects_invented_metrics_and_tags(replacement):
    dashboard = copy.deepcopy(json.loads(DASHBOARD.read_text()))
    query = next(obj for obj in walk_objects(dashboard) if obj.get("data_source") == "metrics")
    query["query"] = replacement
    with pytest.raises((AssertionError, ValueError)):
        validate_dashboard(dashboard)
