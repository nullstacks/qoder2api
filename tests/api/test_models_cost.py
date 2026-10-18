"""`/v1/models` 与单查透出的 `cost` / `reasoning_efforts`。

守住四条口径：

1. `cost` 是**刊例价**（USD / 百万 token），与成本估算共用 `model_match.lookup`
   ——同一份价表、同一套候选键，故「列表显示的价格」与「统计里估算用的价」
   不会打架；只有 input / output / cache_read 三项，**不编** `cache_write`；
2. 缺价、价表记录形状不对都**不带字段**（不填 0，那会被读成免费）；
3. `reasoning_efforts` 是**生效中**的档位（渠道项优先、`*` 兜底），丢掉
   `none`（那是「删除该键」而非档位），未配置不带字段；
4. 列表与单查同源：同一条目的两个出口给出完全一样的值。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.provider.base import Model
from tests.helpers.constants import SECRET


class _Stub:
    """最小渠道替身：`list_models` 返回固定表。"""

    def __init__(self, pid: str, models: list[tuple[str, str]]) -> None:
        self.id = pid
        self._models = models

    async def list_models(self, _data):
        return [Model(id=model_id, name=name) for model_id, name in self._models]

    def import_credential(self, raw):  # pragma: no cover - 未使用
        return raw


def _app(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={
        "trae": _Stub("trae", [("glm-5.2", "GLM 5.2"), ("shared", "Shared")]),
        "kilo": _Stub("kilo", [("shared", "Shared")]),
    })
    for provider in ("trae", "kilo"):
        app.state.credentials.add(provider=provider,
                                  credential_data={"accessToken": "a", "token": "t"})
    return app


def _key(app) -> dict[str, str]:
    created = app.state.api_keys.create("root")
    return {"Authorization": f"Bearer {created['api_key']}"}


def _ids(client, app) -> dict[str, dict]:
    data = client.get("/v1/models", headers=_key(app)).json()["data"]
    return {item["id"]: item for item in data}


# ------------------------------------------------------------------ cost

def test_cost_listed_and_single_lookup_are_identical(tmp_path):
    app = _app(tmp_path)
    app.state.price_table["glm-5.2"] = (3.0, 15.0, 0.3)
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
        single = client.get("/v1/models/glm-5.2", headers=_key(app)).json()
    assert item["cost"] == {"input": 3.0, "output": 15.0, "cache_read": 0.3}
    assert single["cost"] == item["cost"]
    # 我们只有三项价：上游的 cache_write 本服务不采集，不编
    assert set(item["cost"]) == {"input", "output", "cache_read"}


def test_cost_matches_by_display_name(tmp_path):
    """价表键与对外 id 对不上时按展示名兜底（与能力分同一口径）。"""
    app = _app(tmp_path)
    # 价表键是展示名清洗后的形态，条目对外 id 是另一个写法
    app.state.price_table["glm-5-2"] = (1.0, 2.0, 0.1)
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
    assert item["cost"] == {"input": 1.0, "output": 2.0, "cache_read": 0.1}


def test_no_cost_field_without_price(tmp_path):
    """缺价不带字段：`cost: 0` 会被读成「免费」，比没有字段更误导。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
        single = client.get("/v1/models/glm-5.2", headers=_key(app)).json()
    assert "cost" not in item and "cost" not in single


def test_malformed_price_record_is_ignored(tmp_path):
    """价表记录不是三元组（上游结构变了 / 手改快照）时按缺价处理，不抛。"""
    app = _app(tmp_path)
    app.state.price_table["glm-5.2"] = (1.0, 2.0)  # type: ignore[assignment]
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
    assert "cost" not in item


# ------------------------------------------------------- reasoning_efforts

def test_reasoning_efforts_prefers_provider_entry_and_drops_none(tmp_path):
    app = _app(tmp_path)
    app.state.runtime_settings.set(
        "reasoning_effort_map", "trae=low:low,high:high,none:none;*=medium:medium")
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
        single = client.get("/v1/models/glm-5.2", headers=_key(app)).json()
    # 渠道项优先于 `*`；`none` 是「删除该键」不是档位，不进候选
    assert item["reasoning_efforts"] == ["high", "low"]
    assert single["reasoning_efforts"] == item["reasoning_efforts"]


def test_reasoning_efforts_falls_back_to_wildcard(tmp_path):
    app = _app(tmp_path)
    app.state.runtime_settings.set("reasoning_effort_map", "trae=high:high;*=low:low")
    with TestClient(app) as client:
        item = _ids(client, app)["shared"]
    # shared 同时在 trae / kilo 上：trae 命中自身项、kilo 落 `*`，取并集去重
    assert item["reasoning_efforts"] == ["high", "low"]


def test_reasoning_efforts_absent_when_unconfigured(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
    assert "reasoning_efforts" not in item


def test_reasoning_efforts_absent_when_every_target_is_none(tmp_path):
    """全部映射到 `none`（只做删除）时没有可选档位，不带空数组。"""
    app = _app(tmp_path)
    app.state.runtime_settings.set("reasoning_effort_map", "trae=high:none")
    with TestClient(app) as client:
        item = _ids(client, app)["glm-5.2"]
    assert "reasoning_efforts" not in item


def test_reasoning_efforts_hot_reload(tmp_path):
    """档位映射是热更项：改完下一次请求即生效，不需要重启。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert "reasoning_efforts" not in _ids(client, app)["glm-5.2"]
        app.state.runtime_settings.set("reasoning_effort_map", "trae=high:high")
        assert _ids(client, app)["glm-5.2"]["reasoning_efforts"] == ["high"]
