"""`GET /v1/usage`：窗口口径、剩余量、越界 400 与不泄露池级信息。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src.api.usage import _remaining, _since, usage_payload
from src.engine.limits import day_start
from src.key_quota import KeyQuota, KeyUsage
from src.main import build_app
from tests.helpers.auth import isolated_settings
from tests.helpers.events import TRAE_GOOD
from tests.helpers.providers import ScriptedProvider

# 北京时间 2026-10-18 12:00:00
NOON = 1_792_296_000
TODAY = day_start(NOON)


@pytest.fixture()
def provider():
    return ScriptedProvider([TRAE_GOOD])


@pytest.fixture()
def app(tmp_path, provider):
    return build_app(isolated_settings(tmp_path), providers={"trae": provider})


@pytest.fixture()
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def _key(app, **kwargs) -> dict:
    return app.state.api_keys.create("root", "k", **kwargs)


def _record(app, key_id: str, ts: int, *, ok: bool = True, input_tokens: int = 10,
            output_tokens: int = 5, cost_usd: float | None = None) -> None:
    db = app.state.services.key_usage._db  # noqa: SLF001 - 直接写明细造数据
    db.connect().execute(
        "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok, "
        "error_type, input_tokens, output_tokens, reasoning_tokens, cached_tokens, credit, "
        "credit_estimated, cost_usd, cost_cny, latency_ms, ttfb_ms, request_id, api_key_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (f"evt_{key_id}_{ts}", ts, "root", "trae", "cred_secret", "glm-5.2", int(ok),
         None, input_tokens, output_tokens, 0, None, None, 0, cost_usd, None, 1, 1,
         "rid", key_id))
    db.connect().commit()


def _get(client, key: str, **params):
    return client.get("/v1/usage", headers={"Authorization": f"Bearer {key}"},
                      params=params)


# ------------------------------------------------------------ 纯函数


def test_since_spans_inclusive_natural_days():
    assert _since(1, NOON) == TODAY
    assert _since(7, NOON) == TODAY - 6 * 86400
    assert _since(30, NOON) == TODAY - 29 * 86400


def test_remaining_marks_unconfigured_as_none():
    assert _remaining(KeyQuota(), KeyUsage()) == {
        "requests": None, "tokens": None, "cost_usd": None}


def test_remaining_clamps_at_zero():
    quota = KeyQuota(daily_requests=5, daily_tokens=100, daily_cost_usd=1.0)
    usage = KeyUsage(requests=7, input_tokens=80, output_tokens=40, cost_usd=2.0)
    assert _remaining(quota, usage) == {"requests": 0, "tokens": 0, "cost_usd": 0.0}
    assert _remaining(quota, KeyUsage(requests=1, input_tokens=10,
                                      output_tokens=5, cost_usd=0.25)) == {
        "requests": 4, "tokens": 85, "cost_usd": 0.75}


def test_payload_shape_with_injected_clock(app):
    created = _key(app, quota=json.dumps({"daily_requests": 10, "daily_tokens": 100,
                                          "rpm": 5}))
    _record(app, created["id"], TODAY + 60, ok=True, cost_usd=0.5)
    _record(app, created["id"], TODAY + 120, ok=False)
    _record(app, created["id"], TODAY - 5 * 86400, ok=True)   # 7 天窗口内、今天之外
    from src.api.deps import ApiKeyPrincipal

    principal = ApiKeyPrincipal(username="root", key_id=created["id"],
                                quota=KeyQuota(daily_requests=10, daily_tokens=100, rpm=5))
    payload = usage_payload(app.state.services, principal, days=1, now=NOON)
    assert payload["object"] == "usage" and payload["window"] == "today"
    assert (payload["requests"], payload["ok"], payload["failed"]) == (2, 1, 1)
    assert (payload["input_tokens"], payload["output_tokens"]) == (20, 10)
    assert payload["cost_usd"] == pytest.approx(0.5)
    assert payload["limits"]["daily_requests"] == 10
    assert payload["remaining"] == {"requests": 8, "tokens": 70, "cost_usd": None}
    assert payload["resets_at"] == TODAY + 86400
    assert payload["rate"] == {"requests_last_minute": 0, "tokens_last_minute": 0,
                              "inflight": 0}


def test_payload_seven_day_window_still_reports_today_remaining(app):
    created = _key(app, quota=json.dumps({"daily_requests": 10}))
    _record(app, created["id"], TODAY + 60)
    _record(app, created["id"], TODAY - 3 * 86400)
    _record(app, created["id"], TODAY - 20 * 86400)     # 只在 30 天窗口内
    from src.api.deps import ApiKeyPrincipal

    principal = ApiKeyPrincipal(username="root", key_id=created["id"],
                                quota=KeyQuota(daily_requests=10))
    week = usage_payload(app.state.services, principal, days=7, now=NOON)
    assert week["window"] == "last_7d" and week["requests"] == 2
    # 剩余量按当日算：只减今天那一条
    assert week["remaining"]["requests"] == 9
    month = usage_payload(app.state.services, principal, days=30, now=NOON)
    assert month["window"] == "last_30d" and month["requests"] == 3


# ------------------------------------------------------------ HTTP 层


def test_usage_requires_api_key(client):
    assert client.get("/v1/usage").status_code == 401


def test_usage_returns_only_own_records(client, app):
    mine = _key(app)
    other = _key(app)
    _record(app, mine["id"], int(__import__("time").time()))
    _record(app, other["id"], int(__import__("time").time()))
    body = _get(client, mine["api_key"]).json()
    assert body["requests"] == 1
    # 不泄漏池级/他人信息
    text = json.dumps(body)
    assert "cred_secret" not in text and other["id"] not in text
    assert "credential" not in text and "provider" not in text


def test_usage_defaults_to_today(client, app):
    created = _key(app)
    body = _get(client, created["api_key"]).json()
    assert body["window"] == "today"
    assert body["requests"] == 0 and body["cost_usd"] == 0.0
    assert body["limits"] == KeyQuota().as_dict()
    assert body["remaining"] == {"requests": None, "tokens": None, "cost_usd": None}
    assert body["resets_at"] > __import__("time").time()


@pytest.mark.parametrize("days", [0, 2, 6, 31, -1, "abc", ""])
def test_usage_rejects_unsupported_windows(client, app, days):
    created = _key(app)
    response = _get(client, created["api_key"], days=days)
    assert response.status_code == 400
    assert "days must be one of" in response.json()["error"]["message"]


@pytest.mark.parametrize("days,label", [(1, "today"), (7, "last_7d"), (30, "last_30d")])
def test_usage_accepts_three_windows(client, app, days, label):
    created = _key(app)
    assert _get(client, created["api_key"], days=days).json()["window"] == label


def test_usage_reports_live_rate_usage(client, app):
    created = _key(app, quota=json.dumps({"rpm": 5}))
    app.state.services.key_quota.admit(created["id"], KeyQuota(rpm=5), tokens=42)
    body = _get(client, created["api_key"]).json()
    assert body["rate"] == {"requests_last_minute": 1, "tokens_last_minute": 42,
                            "inflight": 1}


def test_usage_is_not_recorded_as_a_request(client, app):
    """自助用量端点不触发上游，也不写明细（否则会自我放大计数）。"""
    created = _key(app)
    before = _get(client, created["api_key"]).json()["requests"]
    _get(client, created["api_key"])
    assert _get(client, created["api_key"]).json()["requests"] == before == 0


def test_usage_does_not_trigger_upstream(client, app, provider):
    created = _key(app)
    _get(client, created["api_key"])
    assert provider.calls == 0
