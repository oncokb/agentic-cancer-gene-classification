from urllib.parse import parse_qs, urlparse

import pytest

from gene_expression_relevance import precheck


@pytest.mark.parametrize("key,rate", [("", 3), ("test-key", 10)])
def test_count_rate_cache_query_and_key(tmp_path, monkeypatch, key, rate):
    now, calls = [0.0], []

    def sleep(seconds):
        now[0] += seconds

    def get(url, before):
        before()
        calls.append((now[0], parse_qs(urlparse(url).query)))
        return {"esearchresult": {"count": "4"}}

    monkeypatch.setattr(precheck.http, "get_json", get)
    client = precheck.Precheck(tmp_path, api_key=key, clock=lambda: now[0], sleep=sleep)
    assert client.count("ERBB2", "BRCA", "breast cancer") == 4
    assert client.count("ERBB2", "BRCA", "breast cancer") == 4
    assert client.count("ALB", "LIHC", "hepatocellular carcinoma") == 4
    assert len(calls) == 2
    assert calls[1][0] - calls[0][0] >= 1 / rate
    assert calls[0][1]["term"] == ['"ERBB2"[tiab] AND (breast cancer)']
    assert calls[0][1].get("api_key") == ([key] if key else None)
    assert precheck.Precheck(tmp_path, api_key=key).count("ERBB2", "BRCA", "breast cancer") == 4
    assert len(calls) == 2
    client.count("ERBB2", "BRCA", "breast carcinoma")
    assert len(calls) == 3


def test_failure_not_cached(tmp_path, monkeypatch):
    client = precheck.Precheck(tmp_path, api_key="")
    monkeypatch.setattr(precheck.http, "get_json", lambda *a, **k: {"error": "failure"})
    with pytest.raises(KeyError):
        client.count("TP53", "BRCA", "breast cancer")
    assert not client.cache


def test_concurrent_counts_are_persisted_without_lost_updates(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    def get(url, before):
        before()
        return {"esearchresult": {"count": "2"}}

    monkeypatch.setattr(precheck.http, "get_json", get)
    client = precheck.Precheck(tmp_path, api_key="key", clock=lambda: now[0], sleep=sleep)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda i: client.count(f"G{i}", "BRCA", "breast cancer"), range(20)))
    assert values == [2] * 20
    assert len(precheck.Precheck(tmp_path).cache) == 20
    assert now[0] >= 19 / 10


def test_ttl_expiry_refreshes_and_migrates_old_unfielded_query_cache(tmp_path, monkeypatch):
    import sqlite3

    with sqlite3.connect(tmp_path / "pubmed.sqlite3") as db:
        db.execute("CREATE TABLE counts (key TEXT PRIMARY KEY, query TEXT, count INTEGER)")
        db.execute("INSERT INTO counts VALUES (?, ?, ?)", ("ALB|LIHC", "liver cancer", 99))
    now, calls = [1000.0], []

    def get(url, before):
        calls.append(url)
        return {"esearchresult": {"count": "5"}}

    monkeypatch.setattr(precheck.http, "get_json", get)
    client = precheck.Precheck(tmp_path, wall_clock=lambda: now[0], ttl_days=30)
    assert client.count("ALB", "LIHC", "liver cancer") == 5
    now[0] += 30 * 86400 - 1
    assert client.count("ALB", "LIHC", "liver cancer") == 5
    assert len(calls) == 1
    now[0] += 1
    assert client.count("ALB", "LIHC", "liver cancer") == 5
    assert len(calls) == 2
