"""Unit tests for local LLM backend selection."""

import pytest

from benchmarks.run_benchmark import _run_pipeline
from src.cli import parse_args
from src.models.schema import AnnotateRequest
from src.pipeline.llm_client import DEFAULT_LOCAL_BACKEND, resolve_local_backend


def test_resolve_local_backend_defaults_for_legacy_bool():
    assert resolve_local_backend(local_mode=True) == DEFAULT_LOCAL_BACKEND


def test_resolve_local_backend_prefers_explicit_backend():
    assert resolve_local_backend(local_mode=False, local_backend="codex") == "codex"
    assert resolve_local_backend(local_mode=False, local_backend="copilot") == "copilot"
    assert (
        resolve_local_backend(local_mode=False, local_backend="antigravity")
        == "antigravity"
    )


def test_resolve_local_backend_rejects_unknown_backend():
    with pytest.raises(ValueError):
        resolve_local_backend(local_backend="unknown")


def test_cli_local_without_backend_defaults_to_claude_code(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local"],
    )
    args = parse_args()
    assert args.local == DEFAULT_LOCAL_BACKEND


def test_cli_local_accepts_codex(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local", "codex"],
    )
    args = parse_args()
    assert args.local == "codex"


def test_cli_local_accepts_copilot(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local", "copilot"],
    )
    args = parse_args()
    assert args.local == "copilot"


def test_cli_local_accepts_antigravity(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        ["prog", "--fusions", "TP53::BRAF", "--local", "antigravity"],
    )
    args = parse_args()
    assert args.local == "antigravity"


def test_cli_accepts_output_csv(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--fusions",
            "TP53::BRAF",
            "--output",
            "results.json",
            "--output-csv",
            "results.csv",
        ],
    )
    args = parse_args()
    assert args.output == "results.json"
    assert args.output_csv == "results.csv"


def test_annotate_request_accepts_local_backend():
    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="codex")
    assert request.local_backend == "codex"

    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="copilot")
    assert request.local_backend == "copilot"

    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="antigravity")
    assert request.local_backend == "antigravity"


async def test_benchmark_run_pipeline_passes_local_backend(monkeypatch):
    seen = {}

    async def fake_run_pipeline(fusions, local_backend=None):
        seen["fusions"] = fusions
        seen["local_backend"] = local_backend

        class Result:
            def model_dump(self):
                return {"annotations": []}

        return Result()

    monkeypatch.setattr("src.pipeline.orchestrator.run_pipeline", fake_run_pipeline)

    result = await _run_pipeline(["TP53::BRAF"], local_backend="codex")

    assert result == {"annotations": []}
    assert seen == {"fusions": ["TP53::BRAF"], "local_backend": "codex"}
