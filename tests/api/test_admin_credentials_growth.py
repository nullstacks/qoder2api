"""凭证运维端点：成长中心运行/历史与仓储。"""

from __future__ import annotations

import httpx

from src.config import Settings
from src.db.conn import Database
from src.db.migrate import SCHEMA_VERSION, apply_schema
from src.main import build_app
from src.provider.base import GrowthResult, GrowthStep, StepStatus
from src.provider.codebuddy.client import CodeBuddyProvider
from src.provider.codebuddy.growth import EP_ENERGY, EP_TASKS, EP_TRAVEL_CONFIG, EP_TRAVEL_STATUS
from tests.helpers.constants import SECRET
from tests.provider.codebuddy._growth_support import TRAVEL_IDLE
from tests.provider.codebuddy._growth_support import client as _client


def _admin_client(tmp_path):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    return app, client

def test_growth_endpoints_run_and_history(tmp_path):
    """手动执行 + 历史查询；无该能力的渠道与不存在的凭证都返回 400。"""
    app, client = _admin_client(tmp_path)
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def growth(self, _data, *, allow_irreversible: bool = True):
            self.flags.append(allow_irreversible)
            return GrowthResult(ok=True, credit=42.0, energy=7, streak_days=3,
                                report="领旅行礼物：+42 积分",
                                steps=[GrowthStep("领旅行礼物", StepStatus.DONE, "+42 积分")])

    stub = Stub()
    app.state.executor._deps.providers["codebuddy"] = stub
    with client:
        body = client.post(f"/api/credentials/{credential_id}/growth").json()
        assert body["ok"] is True and body["credit"] == 42.0 and body["streak_days"] == 3
        assert body["steps"] == [{"name": "领旅行礼物", "status": "done",
                                  "detail": "+42 积分", "credit": None}]
        assert stub.flags == [True]                 # 跟随配置开关

        history = client.get(f"/api/credentials/{credential_id}/growth").json()["events"]
        assert len(history) == 1 and history[0]["trigger"] == "manual"
        assert history[0]["report"] == "领旅行礼物：+42 积分"

        # 列表携带最近一次结果，界面不必再查一次 events
        listed = next(row for row in client.get("/api/credentials").json()["credentials"]
                      if row["provider"] == "codebuddy")
        assert listed["growth_last_result"] == "领旅行礼物：+42 积分"

        # TRAE 不支持成长中心 → 稳定可读的错误
        trae_id = app.state.credentials.add(provider="trae",
                                            credential_data={"accessToken": "t"})
        assert client.post(f"/api/credentials/{trae_id}/growth").status_code == 400
        assert client.get("/api/credentials/cred_missing/growth").status_code == 400
        assert client.post("/api/credentials/cred_missing/growth").status_code == 400

def test_growth_endpoint_respects_irreversible_setting(tmp_path):
    """配置关闭不可逆动作时，手动入口也必须跟着关（不能只挡定时任务）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", GROWTH_IRREVERSIBLE_ACTIONS=False)
    app = build_app(settings)
    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})

    class Stub:
        endpoint = "https://e"

        def __init__(self) -> None:
            self.flags: list[bool] = []

        async def growth(self, _data, *, allow_irreversible: bool = True):
            self.flags.append(allow_irreversible)
            return GrowthResult(ok=True, report="无可领取项")

    stub = Stub()
    app.state.executor._deps.providers["codebuddy"] = stub
    with client:
        client.post(f"/api/credentials/{credential_id}/growth")
    assert stub.flags == [False]

def test_growth_history_limit_and_empty(tmp_path):
    """历史查询：limit 生效，空历史返回空数组。"""
    app, client = _admin_client(tmp_path)
    credential_id = app.state.credentials.add(provider="codebuddy",
                                              credential_data={"bearer_token": "t"})
    with client:
        assert client.get(f"/api/credentials/{credential_id}/growth").json() == {"events": []}
    for index in range(3):
        app.state.growth_events.record(
            credential_id=credential_id,
            result=GrowthResult(ok=True, report=f"第 {index} 轮"),
            now=1000 + index)
    with client:
        events = client.get(f"/api/credentials/{credential_id}/growth?limit=2").json()["events"]
    assert [event["report"] for event in events] == ["第 2 轮", "第 1 轮"]

def test_growth_repository_recent_clamps_limit(growth_repo):
    """limit 下限保护：<=0 时仍返回至少一行（否则界面永远空着像坏了）。"""
    credentials, events = growth_repo
    credential_id = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    events.record(credential_id=credential_id, result=GrowthResult(report="x"), now=1)
    assert len(events.recent(credential_id, limit=0)) == 1
    assert events.latest_for("cred_missing") is None

def test_growth_schema_migration_from_legacy_db(tmp_path):
    """老库升级：补 growth 列 + 建 growth_events 表，历史数据不丢。"""
    import sqlite3

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    conn.execute("""
        CREATE TABLE credentials (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, data_enc TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1, disabled INTEGER NOT NULL DEFAULT 0,
            disabled_reason TEXT, pinned INTEGER NOT NULL DEFAULT 0,
            health INTEGER, cooling_until INTEGER, err_count INTEGER NOT NULL DEFAULT 0,
            quota_remaining REAL, quota_total REAL, quota_cycle_end INTEGER,
            quota_probed_at INTEGER, created_at INTEGER NOT NULL, added_by TEXT)
    """)
    conn.execute("INSERT INTO credentials (id, provider, data_enc, created_at) "
                 "VALUES ('cred_old', 'codebuddy', 'x', 1)")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    columns = {row[1] for row in db.connect().execute("PRAGMA table_info(credentials)")}
    assert {"growth_last_run_at", "growth_last_result"} <= columns
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "growth_events" in tables
    # 历史凭证保留，新列为 NULL
    row = db.connect().execute(
        "SELECT growth_last_result, created_at FROM credentials WHERE id = 'cred_old'").fetchone()
    assert row["growth_last_result"] is None and row["created_at"] == 1
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    apply_schema(db.connect())                        # 二次执行不抛错
    db.close()

def test_growth_schema_index_exists(tmp_path):
    """growth_events 的 (credential_id, ts) 索引必须建出来（历史查询走它）。"""
    db = Database(tmp_path / "index.sqlite3")
    apply_schema(db.connect())
    indexes = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert "idx_growth_cred_ts" in indexes
    db.close()

async def test_provider_growth_delegates_to_runner_and_closes_pool():
    """provider.growth 走缓存客户端；aclose 释放 growth 连接池。"""
    from src.provider.codebuddy.client import _GROWTH_CACHE, _cached_growth

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == EP_TRAVEL_STATUS:
            return httpx.Response(200, json=TRAVEL_IDLE)
        if request.url.path == EP_TRAVEL_CONFIG:
            return httpx.Response(200, json={"code": 0, "data": {"locations": []}})
        if request.url.path == EP_TASKS:
            return httpx.Response(200, json={"code": 0, "data": {"tasks": []}})
        if request.url.path == EP_ENERGY:
            return httpx.Response(200, json={"code": 0, "data": {"balance": 3}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    from src.provider.codebuddy.client import CodeBuddyClient

    client = CodeBuddyClient(endpoint="https://e",
                             short_client=_client(handler))
    provider = CodeBuddyProvider(client=client)
    result = await provider.growth({"bearer_token": "t"}, allow_irreversible=False)
    assert result.ok is True and result.energy == 3
    assert _cached_growth(client) is _cached_growth(client)
    assert client in _GROWTH_CACHE
    await provider.aclose()
    assert client not in _GROWTH_CACHE
