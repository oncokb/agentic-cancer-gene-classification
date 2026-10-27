import copy
import json
import logging
import time
from uuid import uuid1, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import observability, ui_events
from src.auth import AuthenticatedUser
from src.config import settings
from src.main import app


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "ui_events_enabled", True)
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "redis_url", "")
    monkeypatch.setattr(settings, "redis_sentinel_enabled", False)
    monkeypatch.setattr(settings, "ui_events_rate_limit_per_minute", 120)
    monkeypatch.setattr(settings, "ui_events_metric_flag_keys", "")
    monkeypatch.setattr(settings, "public_app_base_url", "http://testserver")
    ui_events._local_rate.clear()
    ui_events._local_ids.clear()
    return TestClient(app)


def event(**changes):
    value = {
        "event_id": str(uuid4()), "ts": int(time.time() * 1000), "type": "impression",
        "surface": "results", "section": "gene_card", "state": None,
        "subject_kind": "gene", "subject_id": "TP53", "run_id": str(uuid4()),
        "item_id": "123456", "position": 0, "display_position": 0,
        "tumor_type_given": False, "flags": None,
    }
    value.update(changes)
    return value


def envelope(events):
    return {"schema_version": 1, "page_view_id": str(uuid4()), "events": events}


def send(client, events, **kwargs):
    headers = {"X-ACGC-Events": "1", **kwargs.pop("headers", {})}
    return client.post("/v1/events", json=envelope(events), headers=headers, **kwargs)


def test_happy_path_metrics_and_log_privacy(client, monkeypatch, caplog):
    calls = []

    class FakeStatsd:
        def increment(self, name, value=1, tags=None):
            calls.append((name, tags))

    monkeypatch.setattr(observability, "_statsd", lambda: FakeStatsd())
    monkeypatch.setattr(settings, "ui_events_metric_flag_keys", "experiment:a|b")
    item = event(flags={"experiment": {"variant": "a", "version": 1},
                        "unlisted": {"variant": "secret", "version": 2}})
    with caplog.at_level(logging.INFO, logger="src.ui_events"):
        response = send(client, [item])
    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "rejected": 0, "duplicates": 0}
    tags = next(tags for name, tags in calls if name == "ui.events")
    assert tags == ["type:impression", "surface:results", "section:gene_card", "variant:experiment.a"]
    assert not any(term in tag for tag in tags for term in ("TP53", item["run_id"], "123456", "user", "unlisted"))
    record = next(record for record in caplog.records if record.name == "src.ui_events" and record.message.startswith("ui_event "))
    assert "@" not in record.message
    assert record.__dict__["usr.email"] == "-"
    assert record.__dict__["usr.id"] == observability.stable_user_key("local@internal")
    assert json.loads(record.message.removeprefix("ui_event "))["auth_provider"] == "local"


def test_rejection_reason_metrics(client, monkeypatch):
    calls = []

    class FakeStatsd:
        def increment(self, name, value=1, tags=None):
            calls.append((name, tags))

    monkeypatch.setattr(observability, "_statsd", lambda: FakeStatsd())
    now = int(time.time() * 1000)
    response = send(client, [event(type="invalid"), event(ts=now - 24 * 3600 * 1000 - 1),
                             event(ts=now + 5 * 60 * 1000 + 1000)])
    assert response.json() == {"accepted": 0, "rejected": 3, "duplicates": 0}
    assert [tags for name, tags in calls if name == "ui.events.rejected"] == [
        ["reason:invalid_event"], ["reason:too_old"], ["reason:future"],
    ]


@pytest.mark.parametrize("change", [
    {"type": "bogus"}, {"surface": "bogus"}, {"section": "bogus"},
    {"state": "ready"}, {"type": "state", "section": "gene_card", "state": "ready"},
    {"type": "action", "state": "ready"}, {"subject_kind": "bogus"},
    {"subject_id": "x" * 65}, {"run_id": "x" * 65}, {"item_id": "x" * 33},
    {"position": -1}, {"display_position": -1}, {"tumor_type_given": "yes"},
    {"flags": {str(i): {"variant": "a", "version": 1} for i in range(11)}},
    {"flags": {"test": {"variant": "x" * 33, "version": 1}}},
    {"flags": {"test": {"variant": "a", "version": "1"}}},
    {"event_id": str(uuid1())},
    {"subject_id": "patient jane doe"},
    {"subject_kind": "fusion", "subject_id": "patient jane doe::TP53"},
    {"subject_kind": "run", "subject_id": "not-a-uuid"},
    {"run_id": "patient jane doe"},
    {"item_id": "patient jane doe"},
    {"flags": {"patient jane doe": {"variant": "a", "version": 1}}},
    {"flags": {"safe": {"variant": "patient jane doe", "version": 1}}},
    {"unexpected": "x"}, {"ts": "123"},
])
def test_invalid_event_rejects_only_it(client, change):
    response = send(client, [event(**change), event()])
    assert response.status_code == 202
    assert response.json() == {"accepted": 1, "rejected": 1, "duplicates": 0}


@pytest.mark.parametrize("change", [
    {"ts": lambda: int(time.time() * 1000) - 24 * 3600 * 1000 - 1000},
    {"ts": lambda: int(time.time() * 1000) + 5 * 60 * 1000 + 1000},
])
def test_timestamp_window(client, change):
    response = send(client, [event(ts=change["ts"]()), event()])
    assert response.json() == {"accepted": 1, "rejected": 1, "duplicates": 0}


def test_dedupe(client):
    item = event()
    assert send(client, [item, copy.deepcopy(item)]).json() == {"accepted": 1, "rejected": 0, "duplicates": 1}
    assert send(client, [item]).json() == {"accepted": 0, "rejected": 0, "duplicates": 1}


def test_envelope_and_body_limits(client):
    assert send(client, [event() for _ in range(50)]).status_code == 202
    assert send(client, [event() for _ in range(51)]).status_code == 422
    for body in (envelope([]), {"schema_version": 2, "page_view_id": str(uuid4()), "events": [event()]},
                 {**envelope([event()]), "extra": 1}):
        assert client.post("/v1/events", json=body, headers={"X-ACGC-Events": "1"}).status_code == 422
    assert client.post("/v1/events", content=b"x" * (32 * 1024 + 1), headers={"X-ACGC-Events": "1", "Content-Type": "application/json"}).status_code == 413


def test_invalid_json_shapes_and_deep_nesting(client):
    for body in (b"not-json", b"[]", b"[" * 30000):
        response = client.post("/v1/events", content=body, headers={
            "X-ACGC-Events": "1", "Content-Type": "application/json",
        })
        assert response.status_code == 422


def test_streaming_body_limit(client):
    def chunks():
        yield b" " * 20000
        yield b" " * 20000

    response = client.post("/v1/events", content=chunks(), headers={
        "X-ACGC-Events": "1", "Content-Type": "application/json",
        "Transfer-Encoding": "chunked",
    })
    assert response.status_code == 413


def test_invalid_batches_are_rate_limited(client, monkeypatch):
    monkeypatch.setattr(settings, "ui_events_rate_limit_per_minute", 2)
    for status_code in (422, 422, 429):
        response = client.post("/v1/events", content=b"invalid", headers={
            "X-ACGC-Events": "1", "Content-Type": "application/json",
        })
        assert response.status_code == status_code


def test_valid_action_state_and_fusion(client):
    response = send(client, [
        event(type="action", state="load_more", subject_kind="fusion", subject_id="TP53::BRAF"),
        event(type="state", section="openevidence", state="ready"),
    ])
    assert response.json() == {"accepted": 2, "rejected": 0, "duplicates": 0}


def test_variant_allowlist_is_per_key(client, monkeypatch):
    calls = []

    class FakeStatsd:
        def increment(self, name, value=1, tags=None):
            calls.append((name, tags))

    monkeypatch.setattr(observability, "_statsd", lambda: FakeStatsd())
    monkeypatch.setattr(settings, "ui_events_metric_flag_keys", "flag_a:control|treatment,flag_b:a|b")
    assert send(client, [event(flags={
        "flag_a": {"variant": "a", "version": 1},
        "flag_b": {"variant": "a", "version": 1},
    })]).status_code == 202
    tags = next(tags for name, tags in calls if name == "ui.events")
    assert "variant:flag_b.a" in tags
    assert not any(tag.startswith("variant:flag_a") for tag in tags)


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(settings, "ui_events_rate_limit_per_minute", 1)
    assert send(client, [event()]).status_code == 202
    response = send(client, [event()])
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1


def test_redis_down_drops_batch(client, monkeypatch):
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")

    def fail():
        raise ConnectionError("down")

    monkeypatch.setattr(ui_events, "_get_client", fail)
    response = send(client, [event()])
    assert response.status_code == 202
    assert response.json() == {"accepted": 0, "rejected": 0, "duplicates": 0}


def test_beacon_and_origin_guard(client):
    body = json.dumps(envelope([event()]))
    response = client.post("/v1/events?beacon=1", content=body, headers={"Content-Type": "text/plain", "Origin": "http://testserver"})
    assert response.status_code == 202
    assert client.post("/v1/events?beacon=1", content=body, headers={"Content-Type": "text/plain"}).status_code == 403
    assert client.post("/v1/events?beacon=1", content=body, headers={"Content-Type": "text/plain", "Sec-Fetch-Site": "same-origin"}).status_code == 202
    assert client.post("/v1/events?beacon=1", content=body, headers={"Content-Type": "text/plain", "Origin": "https://evil.example"}).status_code == 403
    assert client.post("/v1/events", json=envelope([event()])).status_code == 403
    assert client.post("/v1/events", json=envelope([event()]), headers={"X-ACGC-Events": "1", "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_origin_parse_and_configured_public_origin(client, monkeypatch):
    for origin in ("null", "garbage", "https://evil.example"):
        assert send(client, [event()], headers={"Origin": origin}).status_code == 403
    assert send(client, [event()], headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    monkeypatch.setattr(settings, "public_app_base_url", "https://acgc.example/app/path")
    assert send(client, [event()], headers={"Origin": "https://acgc.example"}).status_code == 202
    assert send(client, [event()], headers={"Origin": "http://testserver"}).status_code == 403
    monkeypatch.setattr(settings, "public_app_base_url", "")
    assert send(client, [event()], headers={"Origin": "null"}).status_code == 403


def test_text_plain_requires_beacon(client):
    response = client.post("/v1/events", content=json.dumps(envelope([event()])), headers={
        "X-ACGC-Events": "1", "Content-Type": "text/plain",
    })
    assert response.status_code == 422


def test_batch_outcome_metrics_for_error_statuses(client, monkeypatch):
    calls = []

    class FakeStatsd:
        def increment(self, name, value=1, tags=None):
            calls.append((name, tags))

    monkeypatch.setattr(observability, "_statsd", lambda: FakeStatsd())
    assert send(client, [event()], headers={"Origin": "null"}).status_code == 403
    assert client.post("/v1/events", content=b"x" * 32769, headers={
        "X-ACGC-Events": "1", "Content-Type": "application/json",
    }).status_code == 413
    assert client.post("/v1/events", content=b"bad", headers={
        "X-ACGC-Events": "1", "Content-Type": "application/json",
    }).status_code == 422
    monkeypatch.setattr(settings, "ui_events_rate_limit_per_minute", 2)
    assert send(client, [event()]).status_code == 429
    monkeypatch.setattr(settings, "ui_events_enabled", False)
    assert send(client, [event()]).status_code == 204
    outcomes = [tags for name, tags in calls if name == "ui.batches"]
    assert outcomes == [["outcome:403"], ["outcome:413"], ["outcome:422"],
                        ["outcome:429"], ["outcome:204"]]


def test_disabled_and_status(client, monkeypatch):
    monkeypatch.setattr(settings, "ui_events_enabled", False)
    assert client.post("/v1/events", content=b"invalid").status_code == 204
    assert client.get("/v1/dev/status").json()["ui_events_enabled"] is False


def test_api_key_forbidden(client, monkeypatch):
    assert send(client, [event()], headers={"Authorization": "Bearer acgc_test"}).status_code == 403

    async def api_key_user(_request):
        return AuthenticatedUser(email="curator@mskcc.org", name="Curator", domain="mskcc.org", provider="api_key")

    monkeypatch.setattr(ui_events, "require_auth", api_key_user)
    assert send(client, [event()]).status_code == 403


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.fail_dedupe = False
        self.pipeline_calls = []

    def pipeline(self, transaction=True):
        self.pipeline_calls.append(transaction)
        redis = self

        class Pipeline:
            def __init__(self):
                self.ops = []

            def incr(self, key):
                self.ops.append(("incr", key))
                return self

            def expire(self, key, ttl):
                self.ops.append(("expire", key, ttl))
                return self

            def set(self, key, value, nx, ex):
                self.ops.append(("set", key, value, nx, ex))
                return self

            async def execute(self):
                if redis.fail_dedupe and any(op[0] == "set" for op in self.ops):
                    raise ConnectionError("pipeline failed before commit")
                results = []
                for op in self.ops:
                    if op[0] == "incr":
                        redis.values[op[1]] = redis.values.get(op[1], 0) + 1
                        results.append(redis.values[op[1]])
                    elif op[0] == "expire":
                        results.append(True)
                    else:
                        exists = op[1] in redis.values
                        if not exists:
                            redis.values[op[1]] = op[2]
                        results.append(not exists)
                return results

        return Pipeline()


def test_redis_rate_dedupe_and_cross_pod(client, monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(settings, "ui_events_rate_limit_per_minute", 2)
    monkeypatch.setattr(ui_events, "_get_client", lambda: redis)
    pod_one = FastAPI()
    pod_one.include_router(ui_events.router)
    pod_two = FastAPI()
    pod_two.include_router(ui_events.router)
    first = event()
    assert send(TestClient(pod_one), [first]).json() == {"accepted": 1, "rejected": 0, "duplicates": 0}
    assert send(TestClient(pod_two), [first]).json() == {"accepted": 0, "rejected": 0, "duplicates": 1}
    response = send(TestClient(pod_two), [event()])
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    assert redis.pipeline_calls == [True, True, True, True, True]


def test_redis_dedupe_pipeline_failure_drops_batch(client, monkeypatch):
    redis = FakeRedis()
    redis.fail_dedupe = True
    monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
    monkeypatch.setattr(ui_events, "_get_client", lambda: redis)
    item = event()
    response = send(client, [item])
    assert response.status_code == 202
    assert response.json() == {"accepted": 0, "rejected": 0, "duplicates": 0}
    assert not any(":event:" in key for key in redis.values)
    redis.fail_dedupe = False
    assert send(client, [item]).json()["accepted"] == 1
