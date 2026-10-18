"""`GET /api/stats/events/stream`（SSE 实时明细）：鉴权、作用域、订阅上限与响应头。

生成器本身（帧格式 / 心跳 / 注销）在 `tests/stats/test_live.py` 里用替身逐帧断言；
这里只验端点这一层：谁能订阅、订阅失败怎么回、昵称解析是否按角色开闸、
以及 SSE 必需的响应头。故用替身生成器替换 `_stream_events`（真实实现另测）。
"""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from src.auth.rbac import ROLE_OPERATOR
from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from src.stats.live import LiveEventHub
from tests.helpers.constants import SECRET


def _app(tmp_path, users: list[tuple[str, str, bool]] | None = None):
    """隔离 app；`users` = [(用户名, 角色, 是否已改密)]，按需写进库。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    if users:
        from src.auth.users import create_password_hash
        for username, role, active in users:
            app.state.user_repo.create(
                username=username, password_hash=create_password_hash("pw"),
                role=role, must_change_password=False, enabled=active)
    return app


def _fake_stream(captured: dict):
    """替身生成器：记下调用参数，吐两帧就结束（端点逻辑不依赖真实推送）。"""
    async def stream(request, hub, subscription, *, username, provider, model,
                     credential_id, ok, resolve_name):
        captured.update({"username": username, "provider": provider, "model": model,
                         "credential_id": credential_id, "ok": ok,
                         "resolve_name": resolve_name,
                         "subscribers": hub.subscriber_count})
        yield ": connected\n\n"
        yield 'data: {"id":"e1"}\n\n'
    return stream


def test_stream_returns_sse_frames_and_headers(tmp_path, monkeypatch):
    app = _app(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("src.api.admin_stats._stream_events", _fake_stream(captured))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.get("/api/stats/events/stream",
                              params={"provider": "trae", "model": "m", "ok": 0})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # 中间层缓存/缓冲会把「实时」变成批量，这两个头是硬要求
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"
    frames = [line for line in response.text.splitlines() if line]
    assert frames[0] == ": connected"
    assert json.loads(frames[1].removeprefix("data: ")) == {"id": "e1"}
    assert captured["provider"] == "trae" and captured["model"] == "m"
    assert captured["ok"] == 0        # 失败过滤透传到生成器
    # admin 未指定 username → 全量（scope 为 None）
    assert captured["username"] is None
    assert captured["subscribers"] == 1
    # 注销在生成器的 finally 里（真实实现的逐帧断言见 tests/stats/test_live.py），
    # 这里的替身不承担该职责，故不在此断言槽位归零。

def test_stream_resolves_credential_names_for_operator(tmp_path, monkeypatch):
    app = _app(tmp_path, users=[("op", ROLE_OPERATOR, True)])
    captured: dict = {}
    monkeypatch.setattr("src.api.admin_stats._stream_events", _fake_stream(captured))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("op", SECRET))
        client.get("/api/stats/events/stream", params={"username": "alice"})
    # operator 可解析昵称（凭证池常含邮箱/手机），也可看指定人
    assert captured["resolve_name"] is not None
    assert captured["username"] == "alice"

def test_stream_viewer_scoped_to_self_and_never_sees_names(tmp_path, monkeypatch):
    """viewer 的 username 恒回落到本人，且不解析凭证昵称（与明细端点同口径）。

    `guest` 由 conftest 的 users.txt 导入（默认 viewer 角色）。
    """
    app = _app(tmp_path)
    captured: dict = {}
    monkeypatch.setattr("src.api.admin_stats._stream_events", _fake_stream(captured))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        client.get("/api/stats/events/stream", params={"username": "alice"})
    assert captured["username"] == "guest"
    assert captured["resolve_name"] is None

def test_stream_rejects_when_subscriber_slots_full(tmp_path, monkeypatch):
    app = _app(tmp_path)
    monkeypatch.setattr("src.api.admin_stats._stream_events", _fake_stream({}))
    hub = app.state.live_events
    monkeypatch.setattr(hub, "_max_subscribers", 0)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.get("/api/stats/events/stream")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "10"
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert hub.subscriber_count == 0           # 拒绝时不留半截订阅

def test_stream_requires_session(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/stats/events/stream").status_code == 401

def test_hub_is_shared_between_collector_and_services(tmp_path):
    """装配正确性：`StatsCollector` 的 on_record 与 Services 指向同一个 hub。

    两者接错（各建一个实例）时 SSE 永远收不到事件，且不会有任何报错。
    """
    app = _app(tmp_path)
    assert isinstance(app.state.live_events, LiveEventHub)
    assert app.state.services.live_events is app.state.live_events
    subscription = app.state.live_events.subscribe()
    app.state.stats_collector.record(username="root", provider="trae", model="m", ok=True)
    batch = subscription.drain()
    assert len(batch) == 1
    assert batch[0]["provider"] == "trae" and batch[0]["rowid"] > 0
    assert "credential_name" not in batch[0]   # 昵称由端点按角色补
