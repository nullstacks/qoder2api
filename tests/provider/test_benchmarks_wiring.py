"""OpenRouter 目录：端到端注入与汇率/周期热更。"""

from __future__ import annotations

import pytest

from src import main
from src.benchmarks import build_benchmark_table, save_catalog
from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from tests.helpers.constants import SECRET
from tests.provider._benchmarks_support import OR_PAYLOAD as _OR_PAYLOAD
from tests.provider._benchmarks_support import benchmarks_app as _benchmarks_app
from tests.provider._benchmarks_support import price_app as _price_app

pytestmark = pytest.mark.real_upstream("openrouter")

def _seed_models(services, *, models: dict) -> None:
    """把渠道模型表塞进缓存并 publish 别名表，随后即可打 /v1/models。

    在 lifespan **之后**调用：启动预热会重拉各渠道（`_warm_model_list`），
    早于它填的缓存会被覆盖。trae 是否「已连接」由 `credential_providers`
    按凭证判定，这里顺带给该渠道加一条凭证，与线上「接入渠道」等价。
    """
    from src.api import models as models_api
    from src.provider.base import Model

    services.credentials.add(provider="trae", credential_data={"bearer_token": "t"})
    services.model_list_cache["trae"] = {
        key: (value if isinstance(value, Model) else Model(id=key, name=value))
        for key, value in models.items()}
    models_api.publish_aliases(services, {"trae"})



def _catalog_client(app):
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    client = TestClient(app)
    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    return client

def _create_api_key(client) -> str:
    """用 admin 会话造一把 API Key（/v1/models 走 API Key 鉴权）。"""
    from src.auth.session import create_session_token

    client.cookies.set("coding2api_session", create_session_token("root", SECRET))
    response = client.post("/api/api-keys", json={"name": "bench"}, headers={
        "X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200, response.text
    return response.json()["api_key"]

def test_v1_models_and_playground_carry_benchmarks(tmp_path, monkeypatch):
    """/v1/models 与 /api/playground/models 都随条目透出分数（同一份表）。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={
        "glm-5.3": "GLM 5.3",
        "glm-5.3-flash": "GLM 5.3 Flash",
    })
    app.state.model_benchmarks.update(build_benchmark_table(_OR_PAYLOAD))
    with TestClient(app) as client:
        key = _create_api_key(client)
        data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        rows = {row["id"]: row for row in data["data"]}
        assert rows["glm-5.3"]["benchmarks"]["intelligence_index"] == 44.8
        # 前缀不算匹配：没有分数就不带该字段，而不是带一个错的值
        assert "benchmarks" not in rows["glm-5.3-flash"]

def test_models_endpoint_without_table_has_no_benchmarks(tmp_path, monkeypatch):
    """能力表为空（拉取失败 / 未装配）时，条目与加字段前完全一致。"""
    from fastapi.testclient import TestClient

    # 上游不可用（raw={}）→ 启动预热也拉不到，表保持空
    app = _benchmarks_app(tmp_path, monkeypatch, table={}, raw={})
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={"glm-5.3": "GLM 5.3"})
    with TestClient(app) as client:
        key = _create_api_key(client)
        data = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        assert all("benchmarks" not in row for row in data["data"])

def test_playground_models_endpoint_requires_session(tmp_path, monkeypatch):
    """未登录访问 playground 列表仍 401（不带任何分数）。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app) as client:
        assert client.get("/api/playground/models").status_code == 401

def test_model_catalog_endpoint_merges_benchmarks(tmp_path, monkeypatch):
    """「模型列表」页按同一口径给行补分，无分不带字段。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    catalog = {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3",
                         "provider": "z-ai", "input": 1.0, "output": 2.0,
                         "cache_read": 0.1},
        "other/model": {"id": "other/model", "name": "No Score", "provider": "p",
                        "input": 0.5, "output": 1.0, "cache_read": 0.05},
    }
    save_catalog(str(tmp_path), {}, {}, catalog)
    # raw={} → 启动预热拿不到上游数据、失败退出，落盘明细目录得以保留（否则预热会
    # 用替身数据把它覆盖掉）；能力分表由 snapshot/helper 提供。
    app = _benchmarks_app(tmp_path, monkeypatch,
                          table=build_benchmark_table(_OR_PAYLOAD), raw={})
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        payload = client.get("/api/model-catalog").json()
    rows = {row["id"]: row for row in payload["models"]}
    assert rows["z-ai/glm-5.3"]["benchmarks"]["coding_index"] == 74.8
    assert "benchmarks" not in rows["other/model"]
    assert payload["saved_at"] is not None

def test_model_catalog_sorts_by_intelligence_index(tmp_path, monkeypatch):
    """`sort=intelligence` 按综合智能指数排，无分数的行排最后（不冒充 0 分）。

    目录里有分数的两条（glm-5.3 智 44.8 / kimi-k3 智 43.6）与没分数的两条
    （xai/grok-no-score 三项全 null → 不入表；broken/no-benchmarks 无块）。
    """
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    save_catalog(str(tmp_path), {}, {}, {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3", "provider": "z-ai",
                         "input": 1.0, "output": 2.0, "cache_read": 0.1},
        "moonshotai/kimi-k3": {"id": "moonshotai/kimi-k3", "name": "Kimi K3",
                               "provider": "moonshotai", "input": 0.5, "output": 1.0,
                               "cache_read": 0.05},
        "broken/no-benchmarks": {"id": "broken/no-benchmarks", "name": "Broken",
                                 "provider": "broken", "input": 1.0, "output": 1.0,
                                 "cache_read": 0.1},
    })
    app = _benchmarks_app(tmp_path, monkeypatch,
                          table=build_benchmark_table(_OR_PAYLOAD), raw={})
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        # 默认 id 升序（无 sort 参数）
        default_ids = [row["id"] for row in
                       client.get("/api/model-catalog").json()["models"]]
        # 智降序：44.8 → 43.6 → 无分
        desc_ids = [row["id"] for row in
                    client.get("/api/model-catalog",
                               params={"sort": "intelligence", "order": "desc"}).json()["models"]]
        # 智升序：43.6 → 44.8 → 无分（无分恒在末尾，不因方向翻到最前）
        asc_ids = [row["id"] for row in
                   client.get("/api/model-catalog",
                              params={"sort": "intelligence", "order": "asc"}).json()["models"]]
    assert default_ids == ["broken/no-benchmarks", "moonshotai/kimi-k3", "z-ai/glm-5.3"]
    assert desc_ids == ["z-ai/glm-5.3", "moonshotai/kimi-k3", "broken/no-benchmarks"]
    assert asc_ids == ["moonshotai/kimi-k3", "z-ai/glm-5.3", "broken/no-benchmarks"]

def test_model_catalog_endpoint_lists_sorted_catalog(tmp_path):
    """带登录可读全量模型目录：按模型 id 升序，含汇率与快照时间。"""
    save_catalog(str(tmp_path), {}, {}, {
        "glm-5.2": {"id": "glm-5.2", "name": "GLM 5.2", "provider": "zhipuai",
                    "context": 200000, "input": 1.0, "output": 2.0,
                    "cache_read": 0.1},
        "a-model": {"id": "a-model", "name": None, "provider": "p",
                    "input": 0.5, "output": 1.5, "cache_read": 0.05},
    })
    app = _price_app(tmp_path, USD_CNY_RATE=7)
    with _catalog_client(app) as client:
        payload = client.get("/api/model-catalog").json()
        assert payload["count"] == 2
        assert payload["currency"] == "USD"
        assert payload["usd_cny_rate"] == 7
        assert payload["saved_at"] == app.state.price_saved_at
    assert [row["id"] for row in payload["models"]] == ["a-model", "glm-5.2"]
    assert payload["models"][1] == {
        "id": "glm-5.2", "name": "GLM 5.2", "provider": "zhipuai",
        "context": 200000, "input": 1.0, "output": 2.0, "cache_read": 0.1,
        "available": False}      # 未接入任何渠道：全部标不可用

def test_model_catalog_marks_availability_from_connected_channels(tmp_path, monkeypatch):
    """`available` 命中当前渠道提供的模型才 true（与 /v1/models 同口径）。

    `z-ai/glm-5.3`（OpenRouter id）与渠道侧 `glm-5.3`（归一键）写法不同，
    靠候选键匹配对上；没接入的模型（`other/model`）标 false。
    """
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    catalog = {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3",
                         "provider": "z-ai", "input": 1.0, "output": 2.0,
                         "cache_read": 0.1},
        "other/model": {"id": "other/model", "name": "No Channel", "provider": "p",
                        "input": 0.5, "output": 1.0, "cache_read": 0.05},
    }
    save_catalog(str(tmp_path), {}, {}, catalog)
    app = _benchmarks_app(tmp_path, monkeypatch, table=None, raw={})
    with TestClient(app):
        pass
    _seed_models(app.state.services, models={"glm-5.3": "GLM 5.3"})
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        rows = {row["id"]: row for row in client.get("/api/model-catalog").json()["models"]}
    assert rows["z-ai/glm-5.3"]["available"] is True
    assert rows["other/model"]["available"] is False

def test_model_catalog_availability_ignores_channel_without_credentials(tmp_path,
                                                                        monkeypatch):
    """渠道表在缓存里但**没有可用凭证**（暂停 / 未接入）时不算 available。"""
    from fastapi.testclient import TestClient

    from src.api import models as models_api
    from src.auth.session import create_session_token
    from src.provider.base import Model

    save_catalog(str(tmp_path), {}, {}, {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3", "provider": "z-ai",
                         "input": 1.0, "output": 2.0, "cache_read": 0.1},
    })
    app = _benchmarks_app(tmp_path, monkeypatch, table=None, raw={})
    with TestClient(app):
        pass
    services = app.state.services
    services.model_list_cache["trae"] = {"glm-5.3": Model(id="glm-5.3",
                                                          name="GLM 5.3")}
    models_api.publish_aliases(services, {"trae"})
    # 未加凭证：credential_providers 为空，即便缓存里有表也不标 available
    assert "trae" not in models_api.credential_providers(services)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        rows = {row["id"]: row for row in client.get("/api/model-catalog").json()["models"]}
    assert rows["z-ai/glm-5.3"]["available"] is False

def test_model_catalog_filters_and_paginates_server_side(tmp_path, monkeypatch):
    """`q` / `only_available` / `limit` / `offset` 都在服务端生效，`total` 是筛选后条数。

    `count` 仍是目录全量（页头「命中 N / 共 M」），只有 `total` 随筛选变化。
    """
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    save_catalog(str(tmp_path), {}, {}, {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3", "provider": "z-ai",
                         "input": 1.0, "output": 2.0, "cache_read": 0.1},
        "moonshotai/kimi-k3": {"id": "moonshotai/kimi-k3", "name": "Kimi K3",
                               "provider": "moonshotai", "input": 0.5, "output": 1.0,
                               "cache_read": 0.05},
        "xai/grok": {"id": "xai/grok", "name": "Grok", "provider": "xai",
                     "input": 2.0, "output": 4.0, "cache_read": 0.2},
    })
    app = _benchmarks_app(tmp_path, monkeypatch, table=None, raw={})
    with TestClient(app):
        pass
    # 只有 glm-5.3 接了渠道 → only_available 只剩它
    _seed_models(app.state.services, models={"glm-5.3": "GLM 5.3"})
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))

        # 空 q（None / 纯空白）不筛；count 恒为目录全量
        all_payload = client.get("/api/model-catalog", params={"q": "  "}).json()
        assert all_payload["count"] == 3
        assert all_payload["total"] == 3

        # q 命中 id：大小写不敏感
        by_id = client.get("/api/model-catalog", params={"q": "KIMI"}).json()
        assert [row["id"] for row in by_id["models"]] == ["moonshotai/kimi-k3"]
        assert by_id["total"] == 1

        # q 命中名称（id 里没有 needle）
        by_name = client.get("/api/model-catalog", params={"q": "grok"}).json()
        assert [row["id"] for row in by_name["models"]] == ["xai/grok"]

        # 既不命中 id 也不命中名称 → 空表 + total=0
        none_hit = client.get("/api/model-catalog", params={"q": "zzz"}).json()
        assert none_hit["models"] == [] and none_hit["total"] == 0

        # only_available 只留接入渠道的行
        available = client.get("/api/model-catalog", params={"only_available": True}).json()
        assert [row["id"] for row in available["models"]] == ["z-ai/glm-5.3"]
        assert available["total"] == 1

        # limit / offset 切片：id 升序下取第 2 条（kimi-k3 < xai/grok < z-ai/glm-5.3）
        page = client.get("/api/model-catalog", params={"limit": 1, "offset": 1}).json()
        assert [row["id"] for row in page["models"]] == ["xai/grok"]
        assert page["total"] == 3


def test_model_catalog_marks_suspected_from_recent_failures(tmp_path, monkeypatch):
    """`suspected`：最近窗口内该模型有过模型级失败（口径见可用性视图）。

    目录 id（`z-ai/glm-5.3`）与网关侧对外名（`glm-5.3`）靠候选键对上；没有
    失败记录的行**不带**该字段（「没数据」不等于「确认健康」）。`only_suspected`
    只留带该字段的行，`availability_days` 随响应下发供前端显示「近 N 天」。
    """
    import time

    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    save_catalog(str(tmp_path), {}, {}, {
        "z-ai/glm-5.3": {"id": "z-ai/glm-5.3", "name": "GLM 5.3", "provider": "z-ai",
                         "input": 1.0, "output": 2.0, "cache_read": 0.1},
        "moonshotai/kimi-k3": {"id": "moonshotai/kimi-k3", "name": "Kimi K3",
                               "provider": "moonshotai", "input": 0.5, "output": 1.0,
                               "cache_read": 0.05},
    })
    app = _benchmarks_app(tmp_path, monkeypatch, table=None, raw={})
    with TestClient(app):
        pass
    db = app.state.stats_collector._db  # noqa: SLF001 - 直接造明细（见下）
    now = int(time.time())
    with db.transaction() as conn:
        # `upstream_blocked` 不在受控集合里，必须直接写库（走 record 会被置 None）
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok, error_type)"
            " VALUES (?,?,?,?,?,?,?)",
            ("evt_1", now - 60, "root", "trae", "glm-5.3", 0, "upstream_blocked"))
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok, error_type)"
            " VALUES (?,?,?,?,?,?,?)",
            ("evt_2", now - 90, "root", "trae", "glm-5.3", 0, "invalid_request"))
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, model, ok, error_type)"
            " VALUES (?,?,?,?,?,?,?)",
            ("evt_3", now - 120, "root", "kilo", "glm-5.3", 0, "invalid_request"))
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        payload = client.get("/api/model-catalog").json()
        only = client.get("/api/model-catalog",
                          params={"only_suspected": True}).json()
        clamped = client.get("/api/model-catalog",
                             params={"availability_days": 999}).json()
    rows = {row["id"]: row for row in payload["models"]}
    assert rows["z-ai/glm-5.3"]["suspected"]["failures"] == 2
    # 同一候选键对应两条 (渠道, 模型)：只保留失败最多的那条
    assert rows["z-ai/glm-5.3"]["suspected"]["provider"] == "trae"
    assert "suspected" not in rows["moonshotai/kimi-k3"]
    assert [row["id"] for row in only["models"]] == ["z-ai/glm-5.3"]
    assert only["total"] == 1
    assert payload["availability_days"] == 7
    assert clamped["availability_days"] == 30


def test_model_catalog_endpoint_empty_table(tmp_path, monkeypatch):
    """无快照时回空表 + saved_at=None（页面显示空态，不是错误）。"""
    async def no_models(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", no_models)
    app = _price_app(tmp_path)
    with _catalog_client(app) as client:
        payload = client.get("/api/model-catalog").json()
    assert payload["models"] == [] and payload["count"] == 0
    assert payload["saved_at"] is None

def test_model_catalog_endpoint_requires_session(tmp_path):
    """未登录一律 401：模型列表页也要经过会话鉴权。"""
    from fastapi.testclient import TestClient

    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    app = _price_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/model-catalog").status_code == 401


def test_model_catalog_endpoint_rejects_viewer(tmp_path):
    """viewer（非 operator）403：模型列表已移入管理组，与上游状态同门槛。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token

    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    app = _price_app(tmp_path)
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("guest", SECRET))
        assert client.get("/api/model-catalog").status_code == 403

# --------------------------------------------------------- 汇率热更

def test_usd_cny_rate_is_hot_setting(tmp_path):
    from src.db.repo import RuntimeSettingsRepository
    from src.runtime_settings import load_runtime_settings

    db = Database(tmp_path / "r.sqlite3")
    apply_schema(db.connect())
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      USD_CNY_RATE=7.0)
    runtime = load_runtime_settings(config, RuntimeSettingsRepository(db))
    assert runtime.usd_cny_rate == 7.0
    runtime.set("usd_cny_rate", 6.5)
    assert runtime.usd_cny_rate == 6.5
    runtime.reset("usd_cny_rate")
    assert runtime.usd_cny_rate == 7.0
    db.close()

def test_openrouter_catalog_minutes_has_floor():
    from src.runtime_settings import HOT_BY_KEY

    spec = HOT_BY_KEY["openrouter_catalog_minutes"]
    assert spec.task == "openrouter_catalog"
    assert spec.floor == 60
    assert spec.minimum == 60
