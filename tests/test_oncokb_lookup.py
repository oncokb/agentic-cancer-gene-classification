"""Tests for deterministic OncoKB membership lookup behavior."""

import pytest

from src.pipeline.db_lookups import OncoKBConfigurationError
from src.pipeline.db_lookups import check_oncokb_membership


class FakeLookup:
    def __init__(self, genes):
        self.genes = genes

    async def contains(self, gene_symbol):
        return gene_symbol in self.genes


async def test_check_oncokb_membership_true():
    assert await check_oncokb_membership("TP53", lookup=FakeLookup({"BRAF", "TP53"})) is True


async def test_check_oncokb_membership_false():
    assert await check_oncokb_membership("CLCN3P1", lookup=FakeLookup({"BRAF", "TP53"})) is False


async def test_check_oncokb_membership_raises_on_configuration_error():
    class FailingLookup:
        async def contains(self, gene_symbol):
            raise OncoKBConfigurationError("missing token")

    with pytest.raises(OncoKBConfigurationError):
        await check_oncokb_membership("TP53", lookup=FailingLookup())
