"""Shared fixtures/fakes for tests/test_expression_context_*.py."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.expression_context import lease as lease_module
from src.expression_context.models import LiteratureVerdict
from src.expression_context.redis_client import ModuleRedis
from tests.conftest import FakeRedis

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


class LeaseFakeRedis(FakeRedis):
    """tests.conftest.FakeRedis plus the module's owner-safe lease scripts.

    ``race`` (an async callable) simulates another pod acting at the worst
    moment: for EVAL it runs just before the (atomic) script; for a plain GET
    of a lease key it runs right after the value was read, i.e. between a
    non-atomic implementation's ownership check and its mutation.
    """

    race = None

    async def _run_race(self):
        if self.race is not None:
            race, self.race = self.race, None
            await race()

    async def get(self, key):
        value = await super().get(key)
        if key.startswith(lease_module.LEASE_PREFIX):
            await self._run_race()
        return value

    async def eval(self, script, numkeys, *keys_and_args):
        if script not in (lease_module.RENEW_SCRIPT, lease_module.RELEASE_SCRIPT):
            return await super().eval(script, numkeys, *keys_and_args)
        await self._run_race()
        keys, args = keys_and_args[:numkeys], keys_and_args[numkeys:]
        current = self._live(keys[0])
        if current is None or current.decode() != str(args[0]):
            return 0
        if script == lease_module.RENEW_SCRIPT:
            self._store[keys[0]] = (current, self.now + int(args[1]) / 1000)
            return 1
        del self._store[keys[0]]
        return 1

    def expires_at(self, key):
        return self._store[key][1]


def module_redis(fake):
    return ModuleRedis(factory=lambda: fake)


class DownRedis:
    """Every command fails like an unreachable Redis."""

    async def set(self, *a, **k):
        raise ConnectionError("down")

    async def get(self, *a, **k):
        raise ConnectionError("down")

    async def eval(self, *a, **k):
        raise ConnectionError("down")
