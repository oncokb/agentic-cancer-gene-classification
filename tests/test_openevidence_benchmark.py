"""Verify benchmark accounting independently of live API tests."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from benchmarks import run_openevidence_benchmark as benchmark
from src.models.schema import AnnotationResult, GeneAnnotation


async def test_concurrent_usage_attribution_and_live_oe_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(benchmark.settings, "anthropic_api_key", "test")
    monkeypatch.setattr(benchmark.settings, "openevidence_api_key", "test")
    monkeypatch.setattr(benchmark.llm_client, "record_llm_usage", lambda *args: None)
    monkeypatch.setattr(benchmark.settings, "openevidence_enabled", False)
    monkeypatch.setattr(benchmark.settings, "openevidence_timeout_seconds", 60)
    computes = []

    async def compute():
        computes.append(True)
        return {"value": len(computes)}

    async def annotate(gene):
        await asyncio.sleep(0 if gene == "B" else .01)
        reporter = (benchmark.literature if gene == "B" else benchmark.llm_client)
        reporter.record_llm_usage("test-model", "synthesis", SimpleNamespace(
            input_tokens=10 if gene == "A" else 20, output_tokens=3,
            cache_read_input_tokens=4, cache_creation_input_tokens=5,
        ))
        return GeneAnnotation(gene=gene)

    async def pipeline(genes, force_refresh, on_annotation):
        assert force_refresh
        # Reference cache is cold and isolated, OE always recomputes.
        first = await benchmark.literature.cached_call("same", compute)
        second = await benchmark.literature.cached_call("same", compute)
        assert first == second
        await benchmark.openevidence.cached_call("same", compute)
        await benchmark.openevidence.cached_call("same", compute)
        assert len(computes) == 3
        annotations = await asyncio.gather(*(
            benchmark.orchestrator._annotate_gene(gene=g) for g in ("A", "B")
        ))
        for annotation in annotations:
            await on_annotation(annotation)
        return AnnotationResult(run_id="test", timestamp="test", fusions_processed=2,
                                genes_annotated=2, annotations=annotations)

    monkeypatch.setattr(benchmark.orchestrator, "_annotate_gene", annotate)
    monkeypatch.setattr(benchmark.orchestrator, "run_pipeline", pipeline)
    await benchmark.run("enabled", tmp_path, 900)
    data = json.loads((tmp_path / "enabled.json").read_text())
    assert data["status"] == "complete"
    assert data["per_gene"]["A"]["llm_calls"][0]["input_tokens"] == 10
    assert data["per_gene"]["B"]["llm_calls"][0]["input_tokens"] == 20
    assert data["per_gene"]["A"]["llm_calls"][0]["cache_read_input_tokens"] == 4
    assert data["per_gene"]["B"]["annotation"]["gene"] == "B"
    assert benchmark.CURRENT_GENE.get() == "unattributed"


async def test_existing_run_is_never_overwritten(tmp_path):
    target = tmp_path / "disabled.json"
    target.write_text('{"sentinel": true}')
    with pytest.raises(SystemExit, match="Refusing to overwrite"):
        await benchmark.run("disabled", tmp_path, 900)
    assert json.loads(target.read_text()) == {"sentinel": True}


def test_token_accounting_includes_cache_input_and_escalation():
    from benchmarks.compare_openevidence import tokens
    result = tokens([
        {"input_tokens": 10, "cache_creation_input_tokens": 100,
         "cache_read_input_tokens": 0, "output_tokens": 20},
        {"input_tokens": 30, "cache_creation_input_tokens": 0,
         "cache_read_input_tokens": 200, "output_tokens": 40},
    ])
    assert result["total_input_tokens"] == 340
    assert result["output_tokens"] == 60
    assert result["calls"] == 2


def test_comparison_rejects_incomplete_arms():
    from benchmarks.compare_openevidence import compare
    with pytest.raises(ValueError, match="Both arms must be complete"):
        compare({"status": "running"}, {"status": "complete"}, {})
