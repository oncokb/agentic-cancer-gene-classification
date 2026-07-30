"""Real (non-mocked) integration tests for local LLM backend CLIs.

Unlike tests/test_local_backends.py, these actually shell out to the
installed `claude`/`codex` binaries via complete_with_tool() — no
monkeypatching. They confirm the JSON-in-text local-mode prompting scheme
in src/pipeline/llm_client.py round-trips correctly against real CLI
output, not just that kwargs get forwarded correctly.

Skipped automatically wherever the corresponding CLI isn't installed
(e.g. CI), since these depend on locally authenticated tools.

Note: prompts here are deliberately domain-realistic (mirroring how the
pipeline's actual selection/synthesis prompts read) rather than generic
"echo this value" test scaffolding. The claude-code backend's own safety
heuristics can refuse prompts that read as meta "this is an automated
test suite" framing, since that pattern resembles an attempt to trick it
into emitting fake tool-call output — a real behavior discovered while
building this test, not a bug in the production prompts (which already
avoid that framing).
"""

from __future__ import annotations

import shutil

import pytest

from src.pipeline.llm_client import complete_with_tool

_CLASSIFY_GENE_TOOL = {
    "name": "classify_gene",
    "description": "Record whether the gene described is associated with cancer.",
    "input_schema": {
        "type": "object",
        "properties": {
            "gene": {"type": "string"},
            "cancer_associated": {"type": "boolean"},
        },
        "required": ["gene", "cancer_associated"],
    },
}

_SYSTEM_PROMPT = (
    "You are a biomedical annotation assistant. Given a short gene "
    "description, classify whether the gene is cancer-associated."
)
_USER_PROMPT = (
    "Gene: TP53. Description: TP53 encodes the tumor suppressor p53, "
    "one of the most frequently mutated genes across human cancers."
)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
async def test_claude_code_backend_real_round_trip():
    result = await complete_with_tool(
        model="claude-haiku-4-5-20251001",
        system=_SYSTEM_PROMPT,
        user=_USER_PROMPT,
        tool=_CLASSIFY_GENE_TOOL,
        local_backend="claude-code",
    )
    assert result.get("gene") == "TP53"
    assert result.get("cancer_associated") is True


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
async def test_codex_backend_real_round_trip():
    result = await complete_with_tool(
        model="claude-haiku-4-5-20251001",
        system=_SYSTEM_PROMPT,
        user=_USER_PROMPT,
        tool=_CLASSIFY_GENE_TOOL,
        local_backend="codex",
    )
    assert result.get("gene") == "TP53"
    assert result.get("cancer_associated") is True
