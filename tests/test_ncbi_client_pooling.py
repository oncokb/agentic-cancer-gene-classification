"""Tests that NCBI E-utilities calls reuse a single pooled httpx client."""

from __future__ import annotations

import httpx

from src.pipeline import literature


def test_get_ncbi_client_returns_same_instance_across_calls():
    first = literature._get_ncbi_client()
    second = literature._get_ncbi_client()
    assert first is second
    assert isinstance(first, httpx.AsyncClient)
