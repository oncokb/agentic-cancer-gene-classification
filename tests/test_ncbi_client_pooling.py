"""Tests that NCBI E-utilities calls reuse a single pooled httpx client and a
single token-bucket rate limiter."""

from __future__ import annotations

import httpx
from aiolimiter import AsyncLimiter

from src.pipeline import literature


def test_get_ncbi_client_returns_same_instance_across_calls():
    first = literature._get_ncbi_client()
    second = literature._get_ncbi_client()
    assert first is second
    assert isinstance(first, httpx.AsyncClient)


def test_get_ncbi_rate_limiter_returns_same_instance_across_calls():
    first = literature._get_ncbi_rate_limiter()
    second = literature._get_ncbi_rate_limiter()
    assert first is second
    assert isinstance(first, AsyncLimiter)


async def test_ncbi_rate_limiter_allows_a_burst_up_to_capacity_without_delay():
    """A burst within the configured rate should go through immediately —
    the whole point of a token-bucket limiter over a flat per-call sleep."""
    limiter = literature._get_ncbi_rate_limiter()
    for _ in range(literature._NCBI_CONCURRENCY):
        assert limiter.has_capacity(1)
        async with limiter:
            pass
