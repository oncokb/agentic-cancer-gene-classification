"""Shared fixtures/fakes for tests/test_expression_context_*.py."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.expression_context.models import LiteratureVerdict

FIXTURES = Path(__file__).parent / "fixtures" / "expression_context"
BASE_URL = "https://stats.example.test/expression"
RELEASE_ID = "2026-11-05-datahub-abc1234-m1"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def manifest(**overrides) -> dict:
    data = load_fixture("manifest.json")
    data.update(overrides)
    return data


def gene_file(symbol: str = "ERBB2", **overrides) -> dict:
    data = copy.deepcopy(load_fixture(f"genes/{symbol}.json"))
    data.update(overrides)
    return data


def aliases() -> dict:
    return load_fixture("aliases.json")


def url(path: str) -> str:
    return f"{BASE_URL}/v1/{path}"


def add_artifacts(httpx_mock, *, genes: Tuple[str, ...] = ("ERBB2",), reusable: bool = True):
    """Serve the v1 artifact contract from the fixture files."""
    httpx_mock.add_response(url=url("manifest.json"), json=manifest(), is_reusable=reusable)
    httpx_mock.add_response(url=url("aliases.json"), json=aliases(), is_reusable=reusable)
    for symbol in genes:
        httpx_mock.add_response(
            url=url(f"genes/{symbol}.json"), json=gene_file(symbol), is_reusable=reusable
        )


class FakeLiteratureGate:
    """Records calls; returns a scripted verdict per tumor_type_query."""

    name = "acgc_pipeline_v1"

    def __init__(self, verdicts: Optional[Dict[str, Tuple[bool, List[str]]]] = None) -> None:
        # tumor_type_query -> (insufficient_evidence, pmids)
        self.verdicts = verdicts if verdicts is not None else {
            "breast cancer": (False, ["12345678", "23456789"]),
            "gastric cancer": (True, ["34567890"]),
            "esophageal cancer": (False, []),
        }
        self.calls: List[Tuple[str, str]] = []
        self.fail_for: set[str] = set()

    async def evaluate(self, hugo_symbol: str, tumor_type_query: str) -> LiteratureVerdict:
        self.calls.append((hugo_symbol, tumor_type_query))
        if tumor_type_query in self.fail_for:
            raise RuntimeError("gate exploded")
        insufficient, pmids = self.verdicts.get(tumor_type_query, (True, []))
        return LiteratureVerdict(
            literature_gate=self.name,
            tumor_type_query=tumor_type_query,
            # Deliberately claim supported: the service must recompute it.
            supported=True,
            insufficient_evidence=insufficient,
            pmids=pmids,
            rationale=f"{hugo_symbol} in {tumor_type_query}",
            evidence_support_score=0.8,
            evaluated_at=datetime.now(timezone.utc),
        )
