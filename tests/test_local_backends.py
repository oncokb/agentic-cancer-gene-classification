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


def test_cli_accepts_kinase_truth_comparison_args(monkeypatch):
    monkeypatch.setattr(
        "sys.argv",
        [
            "prog",
            "--fusions",
            "ETV6::NTRK3",
            "--kinase-curation-csv",
            "kinase.csv",
            "--kinase-truth-csv",
            "truth.csv",
            "--kinase-comparison-csv",
            "comparison.csv",
        ],
    )
    args = parse_args()
    assert args.kinase_curation_csv == "kinase.csv"
    assert args.kinase_truth_csv == "truth.csv"
    assert args.kinase_comparison_csv == "comparison.csv"


def test_annotate_request_accepts_local_backend():
    request = AnnotateRequest(fusions=["TP53::BRAF"], local_backend="codex")
    assert request.local_backend == "codex"


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
