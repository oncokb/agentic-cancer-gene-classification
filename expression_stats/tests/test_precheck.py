from urllib.parse import parse_qs, urlparse

import pytest

from gene_expression_relevance import precheck


@pytest.mark.parametrize("key,rate", [("", 3), ("test-key", 3)])
def test_count_rate_cache_query_and_key(tmp_path, monkeypatch, key, rate):
    now, calls = [0.0], []

    def sleep(seconds):
        now[0] += seconds

    def get(url, before, **kwargs):
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
    with pytest.raises(precheck.PrecheckFailed, match=r"TP53\|BRCA"):
        client.count("TP53", "BRCA", "breast cancer")
    assert not client.cache


def test_concurrent_counts_are_persisted_without_lost_updates(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    now = [0.0]

    def sleep(seconds):
        now[0] += seconds

    def get(url, before, **kwargs):
        before()
        return {"esearchresult": {"count": "2"}}

    monkeypatch.setattr(precheck.http, "get_json", get)
    client = precheck.Precheck(tmp_path, api_key="key", clock=lambda: now[0], sleep=sleep)
    with ThreadPoolExecutor(max_workers=8) as pool:
        values = list(pool.map(lambda i: client.count(f"G{i}", "BRCA", "breast cancer"), range(20)))
    assert values == [2] * 20
    assert len(precheck.Precheck(tmp_path).cache) == 20
    assert now[0] >= 19 / 3


def test_ttl_expiry_refreshes_and_migrates_old_unfielded_query_cache(tmp_path, monkeypatch):
    import sqlite3

    with sqlite3.connect(tmp_path / "pubmed.sqlite3") as db:
        db.execute("CREATE TABLE counts (key TEXT PRIMARY KEY, query TEXT, count INTEGER)")
        db.execute("INSERT INTO counts VALUES (?, ?, ?)", ("ALB|LIHC", "liver cancer", 99))
    now, calls = [1000.0], []

    def get(url, before, **kwargs):
        calls.append(url)
        return {"esearchresult": {"count": "5"}}

    monkeypatch.setattr(precheck.http, "get_json", get)
    client = precheck.Precheck(tmp_path, wall_clock=lambda: now[0], ttl_days=30)
    assert client.count("ALB", "LIHC", "liver cancer") == 5
    now[0] = client.expires_at("ALB|LIHC", now[0]) - 1
    assert client.count("ALB", "LIHC", "liver cancer") == 5
    assert len(calls) == 1
    now[0] += 1
    assert client.count("ALB", "LIHC", "liver cancer") == 5
    assert len(calls) == 2


@pytest.mark.parametrize("rate", [1, 2.5, 3, 5, 10])
def test_keyed_rate_can_be_configured_without_changing_default(tmp_path, monkeypatch, rate):
    monkeypatch.setenv("EXPRESSION_STATS_PUBMED_REQUESTS_PER_SECOND", str(rate))
    client = precheck.Precheck(tmp_path, api_key="key")
    assert client.interval >= 1 / rate


@pytest.mark.parametrize("rate", [0, -1, float("nan"), float("inf"), 11])
def test_invalid_keyed_rate_is_rejected(tmp_path, rate):
    with pytest.raises(ValueError, match="requests_per_second"):
        precheck.Precheck(tmp_path, api_key="key", requests_per_second=rate)


def test_unkeyed_rate_cannot_exceed_three(tmp_path):
    with pytest.raises(ValueError, match="requests_per_second"):
        precheck.Precheck(tmp_path, api_key="", requests_per_second=4)


def test_expiries_are_deterministic_spread_and_never_extend_max_ttl(tmp_path):
    client = precheck.Precheck(tmp_path)
    start = 1000
    expiries = [client.expires_at(f"G{i}|BRCA", start) for i in range(100)]
    assert min(expiries) >= start + 15 * 86400
    assert max(expiries) <= start + 30 * 86400
    assert max(expiries) - min(expiries) > 12 * 86400
    restored = precheck.Precheck(tmp_path)
    assert expiries == [restored.expires_at(f"G{i}|BRCA", start) for i in range(100)]


def test_budget_stops_and_committed_cache_resumes(tmp_path, monkeypatch):
    now, requests = [0.0], []

    def get(url, before, **kwargs):
        before()
        requests.append(url)
        return {"esearchresult": {"count": "7"}}

    monkeypatch.setattr(precheck.http, "get_json", get)
    client = precheck.Precheck(tmp_path, clock=lambda: now[0], time_budget_seconds=1)
    client.start_budget(build_deadline=100)
    assert client.count("ALB", "LIHC", "liver") == 7
    now[0] = 1
    with pytest.raises(precheck.PrecheckBudgetExceeded):
        client.count("ERBB2", "BRCA", "breast")
    assert len(requests) == 1
    resumed = precheck.Precheck(tmp_path, clock=lambda: now[0], time_budget_seconds=10)
    resumed.start_budget(build_deadline=100)
    assert resumed.count("ALB", "LIHC", "liver") == 7
    assert len(requests) == 1  # Reopened SQLite has the successful count.
    assert resumed.count("ERBB2", "BRCA", "breast") == 7
    assert len(requests) == 2


def test_http_retry_after_and_socket_timeout_respect_budget(tmp_path, monkeypatch):
    from urllib.error import HTTPError

    now, timeouts = [0.0], []
    client = precheck.Precheck(tmp_path, clock=lambda: now[0], time_budget_seconds=5)
    client.start_budget(build_deadline=100)

    def open_url(*args, timeout):
        timeouts.append(timeout)
        raise HTTPError("https://test", 429, "rate limited", {"Retry-After": "86400"}, None)

    monkeypatch.setattr(precheck.http, "urlopen", open_url)
    monkeypatch.setattr(
        precheck.http.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay)
    )
    with pytest.raises(precheck.PrecheckBudgetExceeded):
        precheck.http.request("https://test", before=client.throttle, remaining=client.remaining)
    assert timeouts == [5]
    assert now[0] == 5


def test_build_headroom_overrides_long_phase_budget(tmp_path):
    client = precheck.Precheck(tmp_path, clock=lambda: 10, time_budget_seconds=1000)
    client.start_budget(build_deadline=15)
    assert client.remaining() == 5


@pytest.mark.parametrize(
    "payload",
    [
        {"esearchresult": {"ERROR": "API rate limit exceeded", "count": "0"}},
        {"error": "API rate limit exceeded"},
        {"ERROR": "Search backend unavailable"},
        {"esearchresult": {}},
        {"esearchresult": {"count": "unknown"}},
        {"esearchresult": {"count": 1.5}},
        {"esearchresult": {"count": True}},
        {"esearchresult": {"count": "-1"}},
        [],
    ],
)
def test_embedded_errors_retry_before_caching(tmp_path, monkeypatch, payload):
    import io
    import json

    now, calls = [0.0], []
    responses = iter([payload, {"esearchresult": {"count": "4"}}])
    client = precheck.Precheck(
        tmp_path, clock=lambda: now[0], sleep=lambda s: now.__setitem__(0, now[0] + s)
    )

    def open_url(*args, **kwargs):
        calls.append(now[0])
        assert not client.cache
        return io.BytesIO(json.dumps(next(responses)).encode())

    monkeypatch.setattr(precheck.http, "urlopen", open_url)
    monkeypatch.setattr(precheck.http.time, "sleep", client.sleep)
    assert client.count("ERBB2", "BRCA", "breast cancer") == 4
    assert len(calls) == 2 and calls[1] - calls[0] >= 1
    assert precheck.Precheck(tmp_path).cache["ERBB2|BRCA"]["count"] == 4


@pytest.mark.parametrize(
    "message", ["Query syntax error", "Empty term and query_key - nothing todo"]
)
def test_deterministic_query_error_is_unavailable_and_not_cached(
    tmp_path, monkeypatch, caplog, message
):
    import io
    import json

    calls = []
    client = precheck.Precheck(tmp_path, sleep=lambda _: None)
    monkeypatch.setattr(precheck.http.time, "sleep", lambda _: None)

    def open_url(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(json.dumps({"esearchresult": {"ERROR": message}}).encode())

    monkeypatch.setattr(precheck.http, "urlopen", open_url)
    assert client.count("ERBB2", "BRCA", "bad query") is None
    assert len(calls) == 4
    assert "ERBB2|BRCA" in caplog.text
    assert not client.cache and not precheck.Precheck(tmp_path).cache


def test_persistent_error_names_pair_and_preserves_other_workers(tmp_path, monkeypatch):
    import io
    from concurrent.futures import ThreadPoolExecutor

    client = precheck.Precheck(tmp_path, sleep=lambda _: None)
    monkeypatch.setattr(precheck.http.time, "sleep", lambda _: None)

    def open_url(request, **kwargs):
        term = parse_qs(urlparse(request.full_url).query)["term"][0]
        if '"ALB"' in term:
            return io.BytesIO(b'{"esearchresult":{"count":"7"}}')
        return io.BytesIO(b'{"esearchresult":{"ERROR":"Search backend unavailable"}}')

    monkeypatch.setattr(precheck.http, "urlopen", open_url)
    with ThreadPoolExecutor(max_workers=2) as pool:
        good = pool.submit(client.count, "ALB", "LIHC", "liver cancer")
        bad = pool.submit(client.count, "ERBB2", "BRCA", "breast cancer")
        assert good.result() == 7
        with pytest.raises(RuntimeError, match=r"ERBB2\|BRCA.*incomplete"):
            bad.result()
    assert list(precheck.Precheck(tmp_path).cache) == ["ALB|LIHC"]


def test_embedded_error_backoff_honors_deadline(tmp_path, monkeypatch):
    import io

    now, calls = [0.0], []
    client = precheck.Precheck(tmp_path, clock=lambda: now[0], time_budget_seconds=1)
    client.start_budget(build_deadline=100)
    monkeypatch.setattr(precheck.http.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))

    def open_url(*args, **kwargs):
        calls.append(1)
        return io.BytesIO(b'{"error":"API rate limit exceeded"}')

    monkeypatch.setattr(precheck.http, "urlopen", open_url)
    with pytest.raises(precheck.PrecheckBudgetExceeded):
        client.count("ERBB2", "BRCA", "breast cancer")
    assert len(calls) == 1 and now[0] == 1
    assert not precheck.Precheck(tmp_path).cache


@pytest.mark.parametrize(
    "messages",
    [
        ["Query syntax error", "Search backend unavailable"] * 2,
        ["Invalid query: backend temporarily unavailable"] * 4,
        ["Query syntax error", "Invalid term"] * 2,
    ],
)
def test_non_deterministic_or_backend_query_errors_fail_closed(tmp_path, monkeypatch, messages):
    import io
    import json

    responses = iter(messages)
    client = precheck.Precheck(tmp_path, sleep=lambda _: None)
    monkeypatch.setattr(precheck.http.time, "sleep", lambda _: None)
    monkeypatch.setattr(
        precheck.http,
        "urlopen",
        lambda *a, **k: io.BytesIO(
            json.dumps({"esearchresult": {"ERROR": next(responses)}}).encode()
        ),
    )
    with pytest.raises(precheck.PrecheckFailed, match=r"ERBB2\|BRCA"):
        client.count("ERBB2", "BRCA", "breast cancer")
    assert not precheck.Precheck(tmp_path).cache


def test_valid_zero_with_warnings_is_cached(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr(
        precheck.http,
        "urlopen",
        lambda *a, **k: io.BytesIO(
            b'{"esearchresult":{"count":"0","errorlist":{"phrasesnotfound":["C1ORF21"]}}}'
        ),
    )
    client = precheck.Precheck(tmp_path)
    assert client.count("C1ORF21", "BRCA", "breast cancer") == 0
    assert precheck.Precheck(tmp_path).cache["C1ORF21|BRCA"]["count"] == 0
