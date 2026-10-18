"""模型目录出口 /v1/models：排序、按渠道费率与推理元数据透传。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_models_by_provider_rates_when_dual_upstream(tmp_path):
    """双上游同名模型倍率不同时，响应带 by_provider 按渠道给倍率。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, rate: float):
            self.id = pid
            self._rate = rate

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2", credit_rate=self._rate)]

        def import_credential(self, raw):
            return raw

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", 0.29), "trae": Stub("trae", 0.17)})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "a"})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        item = next(m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
            if m["id"] == "glm-5.2")
    assert item["providers"] == ["codebuddy", "trae"]
    assert item["credit_rate"] == 0.29          # 合并值：先到先填
    # by_provider 同时给出各渠道的 raw_id（渠道请求 key）与倍率
    assert item["by_provider"] == {
        "codebuddy": {"raw_id": "glm-5.2", "credit_rate": 0.29},
        "trae": {"raw_id": "glm-5.2", "credit_rate": 0.17}}

def test_models_list_orders_cb_then_trae_first(tmp_path):
    """展示顺序：CB → TR → 其余渠道，组内按模型名字典序。

    多渠道模型按最高优先级渠道归位（含 CB 即进第一段）；这是纯展示排序，
    不影响调度选号（`model_resolver` 的 KNOWN_PROVIDERS 顺序）。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, ids: list[str]):
            self.id = pid
            self._ids = ids

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id=model_id) for model_id in self._ids]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={
        # 故意让 zen 的模型名字典序最小、CB 的最大，证明排序按渠道权重而非名字
        "codebuddy": Stub("codebuddy", ["z-cb", "a-cb"]),
        "trae": Stub("trae", ["m-trae"]),
        "zen": Stub("zen", ["aaa-zen"]),
    })
    for provider_id in ("codebuddy", "trae", "zen"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = [item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]]
    assert ids == ["a-cb", "z-cb", "m-trae", "aaa-zen"]

def test_models_list_orders_dual_provider_by_highest_rank(tmp_path):
    """多渠道模型按最高优先级渠道归位：含 CB 进第一段，仅 TR 的进第二段。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, ids: list[str]):
            self.id = pid
            self._ids = ids

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id=model_id) for model_id in self._ids]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={
        # cb-only 与 trae-only 同名 → 合并为多渠道，含 CB 归第一段
        "codebuddy": Stub("codebuddy", ["shared", "cb-only"]),
        "trae": Stub("trae", ["shared", "trae-only"]),
    })
    app.state.credentials.add(provider="codebuddy", credential_data={"t": "a"})
    app.state.credentials.add(provider="trae", credential_data={"t": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
    # shared（含 CB）与 cb-only 同段、按名排序；trae-only 进第二段
    assert [item["id"] for item in data] == ["cb-only", "shared", "trae-only"]
    assert next(item for item in data if item["id"] == "shared")["providers"] == [
        "codebuddy", "trae"]

def test_models_passthrough_reasoning_metadata(tmp_path):
    """supports_reasoning / default_effort 随条目透传，且逐字段补缺。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return self._models

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # trae 只给能力标志，codebuddy 补上默认档位（先到先填、后到补 None）
        "trae": Stub("trae", [Model(id="glm-5.3", supports_reasoning=True)]),
        "codebuddy": Stub("codebuddy", [Model(id="glm-5.3", supports_reasoning=True,
                                              default_effort="medium")]),
    })
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        item = next(m for m in client.get("/v1/models", headers={
            "Authorization": f"Bearer {key}"}).json()["data"]
            if m["id"] == "glm-5.3")
    assert item["supports_reasoning"] is True
    assert item["default_effort"] == "medium"

def test_models_benefit_flag_passthrough(tmp_path):
    """福利标记（CodeArts 每日池模型）随条目透出：单渠道放顶层、多渠道按渠道。

    `benefit` 是展示层区分「走每日 token 池的福利模型」与「按积分计费的内置
    模型」的唯一信号（福利模型上游不给倍率，没有倍率块可看）。它是默认 False
    的布尔，只在为真时才出现——否则每个模型都会多一个 `benefit: false`。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        # 单渠道福利模型：顶层带 benefit
        "codearts": Stub("codearts", [
            Model(id="benefit-only", name="Benefit Only", benefit=True),
            Model(id="builtin-only", name="Builtin Only", credit_rate=0.7),
            # 与 trae 同名 → 并成多渠道：只有 codearts 那侧是福利
            Model(id="shared-model", name="Shared Model", benefit=True)]),
        "trae": Stub("trae", [Model(id="shared-model", name="Shared Model")]),
    })
    for provider_id in ("codearts", "trae"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = {m["id"]: m for m in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    # 单渠道福利：顶层 benefit=True
    assert data["benefit-only"]["benefit"] is True
    # 单渠道内置：无 benefit 字段（默认 False 不产出）
    assert "benefit" not in data["builtin-only"]
    # 多渠道：按渠道标记，顶层不带 benefit（否则会误标 trae）
    merged = data["shared-model"]
    assert merged["providers"] == ["codearts", "trae"]
    assert "benefit" not in merged
    assert merged["by_provider"]["codearts"]["benefit"] is True
    assert "benefit" not in merged["by_provider"]["trae"]
