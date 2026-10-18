"""上游状态页只读端点：GET /api/upstream-status。

守着四件事：**operator-only（admin / operator）**、**未抓取时给空快照而不是
500**、**周期/开关取当前生效值**（热更后立刻反映，与「任务与配置」页同一口径）、
以及**viewer 一律 403**。
"""

from __future__ import annotations

from src.auth.rbac import ROLE_OPERATOR
from src.auth.session import create_session_token
from src.upstream_status import (
    LEVEL_MAJOR,
    LEVEL_OPERATIONAL,
    LEVEL_UNKNOWN,
    Component,
    Incident,
    SourceStatus,
    UpstreamStatusStore,
    spec_by_key,
)
from tests.helpers.constants import SECRET

PATH = "/api/upstream-status"


def _status(key: str, level: str, *, error: str | None = None) -> SourceStatus:
    spec = spec_by_key(key)
    assert spec is not None
    return SourceStatus(
        spec=spec, level=level, fetched_at=1_700_000_000, error=error,
        components=(Component(name="Gateway", level=level, detail="TTFT 2.3s"),),
        incidents=(Incident(title="Elevated errors", level=level, ongoing=True,
                            updated_at=1_700_000_000, url="https://stspg.io/x"),))


def test_requires_operator_rejects_viewer(admin_client):
    """viewer（非 operator）一律 403：与「模型列表」同一门槛。"""
    _app, client = admin_client
    client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
    assert client.get(PATH).status_code == 403


def test_operator_allowed(admin_client):
    """operator 能读：页面与 require_operator 同门槛。"""
    app, client = admin_client
    app.state.user_repo.update_role("guest", ROLE_OPERATOR)     # epoch 0 → 1
    client.cookies.set("coding2api_session",
                       create_session_token("guest", SECRET, epoch=1))
    assert client.get(PATH).status_code == 200


def test_returns_empty_snapshot_before_first_fetch(admin_client):
    """还没抓过（或压根没装配）时给空快照 + enabled/interval，而不是 500。"""
    app, client = admin_client
    # 启动预热已经跑过一轮（测试里抓取被桩成空），换一个全新快照模拟「尚未抓取」
    app.state.upstream_status = UpstreamStatusStore()

    body = client.get(PATH).json()

    assert body["sources"] == [] and body["fetched_at"] is None
    assert body["summary"] == {"level": LEVEL_UNKNOWN, "operational": 0,
                              "abnormal": 0, "unknown": 0, "total": 0}
    assert body["enabled"] is True
    assert body["interval_seconds"] == 300.0     # 默认 5 分钟
    assert isinstance(body["server_time"], int)


def test_returns_snapshot_with_runner_interval(admin_client):
    _app, client = admin_client
    store = UpstreamStatusStore()
    store.update([_status("kilo", LEVEL_MAJOR),
                  _status("moonshot", LEVEL_OPERATIONAL)],
                 fetched_at=1_700_000_000.0)
    client.app.state.upstream_status = store

    body = client.get(PATH).json()

    assert body["summary"] == {"level": LEVEL_MAJOR, "operational": 1,
                               "abnormal": 1, "unknown": 0, "total": 2}
    assert body["fetched_at"] == 1_700_000_000.0
    first = body["sources"][0]
    assert first["key"] == "kilo" and first["label"] == "Kilo"
    assert first["scope"] == "channel" and first["related_providers"] == ["kilo"]
    assert first["components"][0]["detail"] == "TTFT 2.3s"
    assert first["incidents"][0]["ongoing"] is True
    assert first["error"] is None and first["source_stale"] is False
    # 周期来自 TaskRunner 的当前生效值（与 /api/tasks 同一口径）
    assert body["interval_seconds"] == 300.0


def test_reflects_hot_reloaded_interval_and_switch(admin_client):
    """热更周期 / 开关后端点立刻反映（不是装配时的快照）。"""
    app, client = admin_client
    runtime = app.state.runtime_settings
    runtime.set_many({"upstream_status_minutes": 15,
                      "upstream_status_enabled": False})

    body = client.get(PATH).json()

    assert body["interval_seconds"] == 900.0
    assert body["enabled"] is False


def test_falls_back_without_runner_or_store(admin_client):
    """runner / store 都没挂时也能返回（一次性装配与测试）。"""
    app, client = admin_client
    del app.state.upstream_status
    del app.state.task_runner

    body = client.get(PATH).json()

    assert body["sources"] == []
    assert body["interval_seconds"] == 300.0
    assert body["enabled"] is True
