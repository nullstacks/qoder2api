"""侧栏角标聚合端点：GET /api/nav-badges。

守着五件事：**按角色裁剪 key**（viewer 拿不到凭证/告警/任务/用户/上游/目录，
operator 拿不到 admin-only 项）、**未装配 runner / store 时回 0 而非 500**、
**`upstream_level` 随快照走**（前端据此区分维护与真故障）、**模型目录从未拉取
不算陈旧**、以及 **API Key 只数自己的**。
"""

from __future__ import annotations

import time

from src.api.admin_badges import API_KEY_EXPIRY_WINDOW_DAYS
from src.auth.rbac import ROLE_OPERATOR
from src.auth.session import create_session_token
from src.engine.scheduler import ErrorOutcome
from src.upstream_status import (
    LEVEL_MAINTENANCE,
    LEVEL_MAJOR,
    LEVEL_OPERATIONAL,
    SourceStatus,
    UpstreamStatusStore,
    spec_by_key,
)
from tests.helpers.constants import SECRET

PATH = "/api/nav-badges"


def _status(key: str, level: str) -> SourceStatus:
    spec = spec_by_key(key)
    assert spec is not None
    return SourceStatus(spec=spec, level=level, fetched_at=1_700_000_000, error=None,
                        components=(), incidents=())


def _as_operator(app, client, username: str = "guest") -> None:
    app.state.user_repo.update_role(username, ROLE_OPERATOR)     # epoch 0 → 1
    client.cookies.set("coding2api_session",
                       create_session_token(username, SECRET, epoch=1))


def _as_viewer(client, username: str = "guest") -> None:
    client.cookies.set("coding2api_session",
                       create_session_token(username, SECRET))


def test_viewer_gets_only_api_keys(admin_client):
    """viewer：只有 API Key 角标——凭证角标刻意不下发（无写权限，露出会误导）。"""
    _app, client = admin_client
    _as_viewer(client)

    body = client.get(PATH).json()

    assert body == {"api_keys": 0}


def test_operator_gets_credentials_upstream_catalog_not_admin_items(admin_client):
    """operator：有凭证/上游/目录，无告警/任务/用户。"""
    app, client = admin_client
    _as_operator(app, client)

    body = client.get(PATH).json()

    assert set(body) == {"credentials", "api_keys", "upstream", "upstream_level",
                         "model_catalog_stale"}
    assert body["credentials"] == 0
    assert body["upstream"] == 0 and body["upstream_level"] == "unknown"
    assert body["model_catalog_stale"] is False


def test_admin_gets_all_keys(admin_client):
    """admin：全量 key。"""
    _app, client = admin_client

    body = client.get(PATH).json()

    assert set(body) == {"credentials", "api_keys", "alerts", "tasks", "users",
                         "upstream", "upstream_level", "model_catalog_stale"}
    assert body["alerts"] == 0 and body["tasks"] == 0 and body["users"] == 0


def test_credentials_counts_disabled_and_exhausted_only(admin_client):
    """凭证角标 = disabled + exhausted；冷却中与用户暂停的不计。"""
    from src.provider.base import Quota

    app, client = admin_client
    credentials = app.state.credentials
    credentials.add(provider="trae", credential_data={"cookie": "a"})
    dead = credentials.add(provider="trae", credential_data={"cookie": "b"})
    exhausted = credentials.add(provider="trae", credential_data={"cookie": "c"})
    paused = credentials.add(provider="trae", credential_data={"cookie": "d"})
    cooling = credentials.add(provider="trae", credential_data={"cookie": "e"})
    credentials.mark_relogin_required(dead, "session 已失效，请重新登录")
    # total<=0 → health_score 判 EXHAUSTED（-1）
    credentials.save_quota(exhausted, Quota(remaining=0, total=0, probed_at=1))
    credentials.set_enabled(paused, False)
    credentials.save_quota(paused, Quota(remaining=0, total=0, probed_at=1))
    credentials.set_enabled(paused, False)                      # 暂停 + 耗尽：仍是「暂停」
    # 账号级冷却：到点自愈，不该变成常亮角标
    credentials.save_error(cooling, ErrorOutcome(cooling_until=9_999_999_999, err_count=3))

    body = client.get(PATH).json()

    assert body["credentials"] == 2     # dead + exhausted


def test_alerts_counts_only_unresolved(admin_client):
    """告警角标只数未恢复：恢复过的历史行不算（与 count_all 不同）。"""
    app, client = admin_client
    alerts = app.state.services.alerts
    alerts.record(rule="pool_empty", severity="critical", scope="pool", message="m")
    alerts.record(rule="task_failed", severity="warning", scope="alert", message="m")
    alerts.record(rule="error_rate", severity="warning", scope="trae", message="m")
    alerts.resolve("task_failed", "alert", now=1_700_000_000)

    body = client.get(PATH).json()

    assert body["alerts"] == 2


def test_tasks_counts_last_failed_enabled_only(admin_client):
    """任务角标 = 上次真跑失败且启用；从未运行（None）与已关闭的不计。"""
    app, client = admin_client
    runner = app.state.task_runner
    runner.status.record("quota_probe", started_at=1.0, ok=False, report=None,
                         error="boom")
    runner.status.record("refresh", started_at=1.0, ok=True, report=None, error=None)
    # backup 从未运行（不入账）→ last_ok None

    body = client.get(PATH).json()

    assert body["tasks"] == 1


def test_tasks_zero_without_runner(admin_client):
    """未装配 task_runner（一次性构造 / 测试）时回 0 而不是 500。"""
    app, client = admin_client
    del app.state.task_runner

    body = client.get(PATH).json()

    assert body["tasks"] == 0


def test_users_counts_pending_activation(admin_client):
    """用户角标 = 有激活令牌的账号数。"""
    app, client = admin_client
    app.state.user_repo.create("pending", "hash", role="viewer",
                               activation_digest="digest", activation_expires_at=1)
    app.state.user_repo.create("ready", "hash", role="viewer")

    body = client.get(PATH).json()

    assert body["users"] == 1


def test_upstream_zero_without_store(admin_client):
    """未装配 upstream_status（一次性构造 / 测试）时回 0 + unknown，而不是 500。"""
    app, client = admin_client
    del app.state.upstream_status

    body = client.get(PATH).json()

    assert body["upstream"] == 0
    assert body["upstream_level"] == "unknown"


def test_upstream_reports_abnormal_and_worst_level(admin_client):
    """上游角标取 abnormal，档位取最差——维护中与真故障在 level 上可区分。"""
    app, client = admin_client
    store = UpstreamStatusStore()
    store.update([_status("kilo", LEVEL_MAJOR),
                  _status("moonshot", LEVEL_MAINTENANCE),
                  _status("deepseek", LEVEL_OPERATIONAL)],
                 fetched_at=1_700_000_000.0)
    app.state.upstream_status = store

    body = client.get(PATH).json()

    assert body["upstream"] == 2
    assert body["upstream_level"] == LEVEL_MAJOR      # 真故障压过维护


def test_upstream_level_maintenance_when_only_maintenance(admin_client):
    """只有维护中的源时 level 是 maintenance（前端渲染成黄而非红）。"""
    app, client = admin_client
    store = UpstreamStatusStore()
    store.update([_status("kilo", LEVEL_MAINTENANCE),
                  _status("moonshot", LEVEL_OPERATIONAL)],
                 fetched_at=1_700_000_000.0)
    app.state.upstream_status = store

    body = client.get(PATH).json()

    assert body["upstream"] == 1
    assert body["upstream_level"] == LEVEL_MAINTENANCE


def test_catalog_stale_rules(admin_client):
    """从未拉取不算陈旧；拉过但超 2 倍周期才算。"""
    app, client = admin_client
    runtime = app.state.runtime_settings
    runtime.set_many({"openrouter_catalog_minutes": 60})

    # 从未拉取：不算陈旧（新装不该一上来就报警）
    assert client.get(PATH).json()["model_catalog_stale"] is False

    # 刚拉过：不陈旧
    app.state.price_saved_at = time.time()
    assert client.get(PATH).json()["model_catalog_stale"] is False

    # 超过 2 × 60 分钟：陈旧
    app.state.price_saved_at = time.time() - 3 * 3600
    assert client.get(PATH).json()["model_catalog_stale"] is True


def test_api_keys_counts_own_expiring_only(admin_client):
    """API Key 角标只数自己的、且只数已过期或 7 天内到期的。"""
    app, client = admin_client
    keys = app.state.api_keys
    now = int(time.time())
    soon = now + (API_KEY_EXPIRY_WINDOW_DAYS - 1) * 86400
    keys.create("root", "soon", expires_at=soon)               # 计入
    keys.create("root", "expired", expires_at=now - 10)        # 已过期，计入
    keys.create("root", "forever")                             # 永不过期，不计
    keys.create("root", "later", expires_at=now + 30 * 86400)  # 还早，不计
    keys.create("guest", "other", expires_at=soon)             # 别人的，不计

    body = client.get(PATH).json()

    assert body["api_keys"] == 2
