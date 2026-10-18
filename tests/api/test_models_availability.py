"""模型可用性只读视图 `GET /api/models/availability`。

口径（详见 `src/api/admin_model_availability.py`）：

* operator 门槛（admin / operator 可读，viewer 403，未登录 401）；
* 窗口默认 7 天、钳到 `[1, 30]`，`limit` 钳到 `[1, 200]`——看板端点不该被
  前端缓存里的越界参数打成 400；
* 只认 `upstream_blocked` / `invalid_request` 两类 `error_type`（前者是协议
  预留值，后者是当前 executor 对「上游不认这个模型」的归类）；
* `blocked` 现算：拿当前 `MODEL_BLOCKLIST` 做 glob 匹配，热更后立即反映；
* 聚合走 `idx_usage_ts`（窗口是绝对时刻 `since`）并限行数，超出截断。
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.main import build_app
from tests.helpers.constants import SECRET

PATH = "/api/models/availability"


def _insert(app, *, provider: str, model: str, error_type: str | None,
            ts: int | None = None, event_id: str | None = None) -> None:
    """直接写一条明细。

    不走 `StatsCollector.record`：`upstream_blocked` 不在
    `CONTROLLED_ERROR_TYPES` 里，会被 `_normalize_error_type` 置 None，而那
    正是本端点要收的协议预留值。
    """
    stamp = int(time.time()) if ts is None else ts
    with app.state.stats_collector._db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok, error_type)"
            " VALUES (?,?,?,?,?,?,?)",
            (event_id or f"evt_{provider}_{model}_{stamp}_{error_type}",
             stamp, "root", provider, model, 0, error_type))


def _rows(client, **params) -> dict:
    response = client.get(PATH, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ------------------------------------------------------------------ 鉴权

def test_requires_session(tmp_path):
    app = build_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get(PATH).status_code == 401


def test_viewer_is_forbidden(tmp_path):
    """viewer 能看自己的用量但看不了运维看板（与凭证页同一门槛）。"""
    app = build_app(_settings(tmp_path))
    app.state.user_repo.create("watcher", "hash", role="viewer")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session",
                           create_session_token("watcher", SECRET))
        assert client.get(PATH).status_code == 403


def test_operator_can_read(tmp_path):
    app = build_app(_settings(tmp_path))
    app.state.user_repo.create("ops", "hash", role="operator")
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("ops", SECRET))
        body = _rows(client)
    assert body["rows"] == []


def _settings(tmp_path):
    from src.config import Settings

    return Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                    ADMIN_USERNAMES="root")


# ------------------------------------------------------------ 聚合与过滤

def test_aggregates_by_provider_and_model(admin_client):
    app, client = admin_client
    now = int(time.time())
    for offset in (10, 20, 30):
        _insert(app, provider="trae", model="glm-5.2", error_type="invalid_request",
                ts=now - offset)
    _insert(app, provider="kilo", model="space-bunny", error_type="upstream_blocked",
            ts=now - 5)
    rows = _rows(client)["rows"]
    assert [row["provider"] for row in rows] == ["trae", "kilo"]  # 失败次数降序
    first = rows[0]
    assert first["model"] == "glm-5.2"
    assert first["failures"] == 3
    assert first["last_failed_at"] == now - 10
    assert rows[1]["failures"] == 1


def test_ignores_other_error_types_and_successes(admin_client):
    """其它失败原因（限流 / 凭证失效 / 上游 5xx）与成功请求都不算「模型下线」。"""
    app, client = admin_client
    for error_type in ("rate_limit", "credential_unavailable",
                       "upstream_error", "model_unavailable", None):
        _insert(app, provider="trae", model="glm-5.2", error_type=error_type)
    with app.state.stats_collector._db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok)"
            " VALUES (?,?,?,?,?,?)",
            ("evt_ok", int(time.time()), "root", "trae", "glm-5.2", 1))
    assert _rows(client)["rows"] == []


def test_window_excludes_old_failures(admin_client):
    app, client = admin_client
    now = int(time.time())
    _insert(app, provider="trae", model="recent", error_type="invalid_request",
            ts=now - 3600)
    _insert(app, provider="trae", model="ancient", error_type="invalid_request",
            ts=now - 40 * 86400)
    assert [row["model"] for row in _rows(client)["rows"]] == ["recent"]
    # 放宽到 30 天也看不到 40 天前的那条（上限就是 30 天）
    assert [row["model"] for row in _rows(client, days=30)["rows"]] == ["recent"]


def test_days_is_clamped(admin_client):
    _app, client = admin_client
    assert _rows(client, days=0)["window_days"] == 1
    assert _rows(client, days=-5)["window_days"] == 1
    assert _rows(client, days=7)["window_days"] == 7
    assert _rows(client, days=999)["window_days"] == 30


def test_since_is_absolute_and_matches_window(admin_client):
    _app, client = admin_client
    before = int(time.time())
    body = _rows(client, days=3)
    after = int(time.time())
    assert before - 3 * 86400 <= body["since"] <= after - 3 * 86400


def test_limit_truncates_and_flags(admin_client):
    app, client = admin_client
    now = int(time.time())
    for index in range(5):
        _insert(app, provider="trae", model=f"m{index}", error_type="invalid_request",
                ts=now - index, event_id=f"evt_{index}")
    body = _rows(client, limit=3)
    assert len(body["rows"]) == 3
    assert body["truncated"] is True
    # 失败次数相同 → 按 (provider, model) 升序，截断结果稳定
    assert [row["model"] for row in body["rows"]] == ["m0", "m1", "m2"]
    full = _rows(client, limit=5)
    assert full["truncated"] is False
    assert len(full["rows"]) == 5


def test_limit_is_clamped(admin_client):
    _app, client = admin_client
    assert _rows(client, limit=0)["rows"] == []
    assert _rows(client, limit=-1)["rows"] == []
    # 上限 200：多写一条即触发截断（证明 limit 被钳到 200 而不是 9999）
    app, client = admin_client
    now = int(time.time())
    for index in range(201):
        _insert(app, provider="trae", model=f"m{index:03d}",
                error_type="invalid_request", ts=now, event_id=f"evt_{index}")
    body = _rows(client, limit=9999)
    assert len(body["rows"]) == 200 and body["truncated"] is True


# ------------------------------------------------------------- blocked 现算

def test_blocked_reflects_current_blocklist(admin_client):
    app, client = admin_client
    _insert(app, provider="trae", model="glm-5.2", error_type="invalid_request")
    _insert(app, provider="kilo", model="space-bunny", error_type="invalid_request")
    rows = {row["model"]: row for row in _rows(client)["rows"]}
    assert rows["glm-5.2"]["blocked"] is False

    app.state.runtime_settings.set("model_blocklist", "glm-*")
    rows = {row["model"]: row for row in _rows(client)["rows"]}
    assert rows["glm-5.2"]["blocked"] is True
    assert rows["space-bunny"]["blocked"] is False


def test_blocked_matches_normalized_key_and_display_name(admin_client):
    """记录里的模型名可能是展示名（`Kimi K3`）：与黑名单同口径归一再比。"""
    app, client = admin_client
    _insert(app, provider="trae", model="Kimi K3", error_type="invalid_request")
    app.state.runtime_settings.set("model_blocklist", "kimi-k3")
    row = _rows(client)["rows"][0]
    assert row["blocked"] is True


def test_error_types_reported(admin_client):
    """响应里带口径说明，前端不必硬编码「哪几类算下线」。"""
    _app, client = admin_client
    assert _rows(client)["error_types"] == ["upstream_blocked", "invalid_request"]
