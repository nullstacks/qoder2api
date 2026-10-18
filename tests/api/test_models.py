"""模型目录出口 /v1/models：默认注册表、凭证过滤、缓存回退与黑名单过滤。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


def test_default_registry_contains_both_providers(tmp_path):
    """双上游注册后 /v1/models 与 playground 模型一致，动态拉取失败回退静态表。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class FailingCB:
        id = "codebuddy"

        def list_models(self, _data):
            raise RuntimeError("dynamic fetch unavailable")

        def import_credential(self, raw):
            return raw

    class TraeStub:
        id = "trae"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={
        "trae": TraeStub(),
        "codebuddy": FailingCB(),
    })
    # 模型列表按渠道凭证加载：接入凭证后该渠道才参与
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        data = client.get("/v1/models",
                          headers={"Authorization": f"Bearer {key}"}).json()["data"]
    by_id = {item["id"]: item["providers"] for item in data}
    # CB 的 list_models 抛错 → 该上游被跳过（不拖垮整个列表），TRAE 正常返回
    assert by_id["glm-5.2"] == ["trae"]

def test_models_endpoint_skips_providers_without_credentials(tmp_path):
    """模型列表按渠道凭证加载——没凭证的渠道既不拉取也不展示。

    冷启动只有一个渠道接入时，另一个渠道的 list_models 根本不该被调用，
    Playground / `/v1/models` 也不应出现它的模型。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    calls: list[str] = []

    class Stub:
        def __init__(self, pid: str, model: str):
            self.id = pid
            self._model = model

        async def list_models(self, _data):
            from src.provider.base import Model

            calls.append(self.id)
            return [Model(id=self._model)]

        def import_credential(self, raw):  # pragma: no cover - 未使用
            return raw

    app = build_app(settings, providers={
        "trae": Stub("trae", "trae-only"),
        "codebuddy": Stub("codebuddy", "cb-only"),
    })
    # 只接 TRAE，CodeBuddy 无凭证
    trae_id = app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        ids = {m["id"] for m in client.get("/v1/models", headers=auth).json()["data"]}
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        playground = {m["id"] for m in client.get("/api/playground/models").json()["data"]}
        assert ids == {"trae-only"}
        assert playground == {"trae-only"}
        assert "codebuddy" not in calls              # CodeBuddy 未被拉取

        # 暂停唯一凭证 → 该渠道在列表里消失（不展示打不通的渠道）
        app.state.credentials.set_enabled(trae_id, False)
        assert client.get("/v1/models", headers=auth).json()["data"] == []

        # 恢复 → 立刻回到列表
        app.state.credentials.set_enabled(trae_id, True)
        assert {m["id"] for m in client.get(
            "/v1/models", headers=auth).json()["data"]} == {"trae-only"}

        # 硬禁用（会话失效）同样不展示；补一条新凭证立刻恢复
        from src.engine.scheduler import ErrorOutcome

        app.state.credentials.save_error(trae_id, ErrorOutcome(disabled=True))
        assert client.get("/v1/models", headers=auth).json()["data"] == []
        app.state.credentials.add(provider="trae", credential_data={"accessToken": "b"})
        assert {m["id"] for m in client.get(
            "/v1/models", headers=auth).json()["data"]} == {"trae-only"}
    assert "codebuddy" not in calls

def test_models_cache_used_when_fetch_fails(tmp_path):
    """拉取成功后写入缓存；之后失败时用缓存兜底，/v1/models 保持完整。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class FailingCB:
        id = "codebuddy"

        def list_models(self, _data):
            raise RuntimeError("dynamic fetch unavailable")

        def import_credential(self, raw):
            return raw

    class FlakyTrae:
        id = "trae"
        fail = False

        async def list_models(self, _data):
            from src.provider.base import Model

            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2"), Model(id="DeepSeek-V4-Flash")]

        def import_credential(self, raw):
            return raw

    trae = FlakyTrae()
    app = build_app(settings, providers={"trae": trae, "codebuddy": FailingCB()})
    # CB 有凭证才会被尝试拉取（进而触发失败回退）；TRAE 同理
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    app.state.credentials.add(provider="codebuddy", credential_data={"token": "b"})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        # 第一次：拉取成功，缓存写入
        first = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in first} == {"glm-5.2", "deepseek-v4-flash"}
        cached_keys = app.state.services.model_list_cache["trae"].keys()
        assert cached_keys == {"glm-5.2", "deepseek-v4-flash"}

        # 第二次：拉取失败 → 用缓存兜底，列表不缺模型
        trae.fail = True
        second = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in second} == {"glm-5.2", "deepseek-v4-flash"}
        assert {item["id"] for item in second} and all(
            item["providers"] == ["trae"] for item in second)

        # 恢复后重新拉取成功，缓存刷新
        trae.fail = False
        third = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in third} == {"glm-5.2", "deepseek-v4-flash"}

def test_models_blocklist_filters_noise_and_old(tmp_path):
    """默认黑名单滤非用户模型（custom_model_*/subagent/summary/file_search_agent/
    default/hunyuan-image-*），MODEL_BLOCKLIST 覆盖后可再滤老模型；
    直连指定不受列表过滤影响。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class NoisyProvider:
        id = "trae"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="glm-5.2"), Model(id="kimi-k2.6"),
                    Model(id="custom_model_claude"),
                    Model(id="explore_sub_agent_v13"), Model(id="summary"),
                    Model(id="file_search_agent"), Model(id="default"),
                    Model(id="hunyuan-image-alpha")]

        def import_credential(self, raw):
            return raw

    app = build_app(settings, providers={"trae": NoisyProvider()})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 默认黑名单：噪音全滤（实测内部/不可用模型），老模型默认保留（是否滤由配置决定）
    assert "custom_model_claude" not in ids
    assert "explore_sub_agent_v13" not in ids
    assert "summary" not in ids
    assert "file_search_agent" not in ids
    assert "default" not in ids
    assert "hunyuan-image-alpha" not in ids
    assert {"glm-5.2", "kimi-k2.6"} <= ids

    # 覆盖黑名单：额外滤老模型（完全替换语义，需重写噪音规则）
    settings2 = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                         MODEL_BLOCKLIST="custom_model_*,*sub*agent*,summary,kimi-k2.6")
    app2 = build_app(settings2, providers={"trae": NoisyProvider()})
    app2.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key2 = app2.state.api_keys.create("root")["api_key"]
    with TestClient(app2) as client:
        ids2 = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key2}"}).json()["data"]}
    assert "kimi-k2.6" not in ids2 and "glm-5.2" in ids2

def test_models_blocklist_default_covers_retired_upstream_models(tmp_path):
    """默认黑名单含 2026-10-10 复测补入的三条「上游已下架」。

    `hy4-preview-x`（CB 400 11102）与同名的 `hy4-preview` 只差后缀，故必须
    写全名而不是 `hy4-preview*`；`qwen3.8-flash` 只命中 Qoder 的 qfmodel，
    CB/TRAE 的 `qwen3.8-max` 不受影响；`glyph-cluster` 按 kilo 展示名清洗出的
    归一键命中，不误伤同渠道其它模型。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Stub:
        def __init__(self, pid: str, models):
            self.id = pid
            self._models = models

        async def list_models(self, _data):
            return list(self._models)

        def import_credential(self, raw):
            return raw

    from src.provider.base import Model

    app = build_app(settings, providers={
        "codebuddy": Stub("codebuddy", [Model(id="hy4-preview-x", name="Hy4 preview"),
                                        Model(id="hy4-preview", name="Hy4 preview")]),
        "qoder": Stub("qoder", [Model(id="qfmodel", name="Qwen3.8-Flash"),
                                Model(id="qmodel_38max", name="Qwen3.8-Max")]),
        "kilo": Stub("kilo", [Model(id="stealth/glyph-cluster",
                                    name="Stealth: Glyph Cluster (free)"),
                               Model(id="kilo-auto/free", name="Auto Free")]),
    })
    for pid in ("codebuddy", "qoder", "kilo"):
        app.state.credentials.add(provider=pid, credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}

    assert "hy4-preview-x" not in ids
    assert "qwen3.8-flash" not in ids
    assert "glyph-cluster" not in ids
    # 近邻模型不受牵连：同名不同 id 的 hy4-preview、qwen3.8-max、kilo-auto 都在
    assert {"hy4-preview", "qwen3.8-max", "kilo-auto"} <= ids

def test_models_blocklist_matches_normalized_id_and_display_name(tmp_path):
    """黑名单按**归一后的对外写法**匹配，且两边忽略大小写。

    对外 id 改为归一键后，用户照列表里看到的名字写黑名单必须生效——
    按 zen 归一键 `longcat-2.5-preview`、展示名派生的 `KIMI-K3`（**大小写
    与展示名不同**）都能滤掉；Qoder 的原代号 `kmodel_latest` 归一后是
    `kimi-k3`，故 `KIMI-K3`（忽略大小写）命中。同时老口径（原代号
    `custom_model_claude`）仍生效。未被列入的 `MiMo V2.5` 照常保留。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        MODEL_BLOCKLIST="KIMI-K3,longcat-2.5-preview,"
                                        "custom_model_claude")

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
        "zen": Stub("zen", [Model(id="longcat-2.5-preview-free",
                                  name="LongCat 2.5 Preview"),
                            Model(id="mimo-v2.5-free", name="MiMo V2.5")]),
        "qoder": Stub("qoder", [Model(id="kmodel_latest", name="Kimi-K3"),
                                Model(id="custom_model_claude")]),
    })
    for provider_id in ("zen", "qoder"):
        app.state.credentials.add(provider=provider_id, credential_data={"t": "x"})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        ids = {item["id"] for item in client.get(
            "/v1/models", headers={"Authorization": f"Bearer {key}"}).json()["data"]}
    # 归一键 / 展示名（大小写混合）/ 原代号三种写法命中即滤
    assert "longcat-2.5-preview" not in ids
    assert "kimi-k3" not in ids
    assert ids == {"mimo-v2.5"}   # 未列入黑名单的保留

def test_models_blocklist_hot_reload_applies_without_waiting_for_ttl(tmp_path):
    """MODEL_BLOCKLIST 热更后下一次 /v1/models 立即过滤，不受 TTL 缓存影响。

    回归点：缓存若存过滤后的结果，TTL（300s）内改黑名单不会生效，且被滤掉的
    模型会从「失败兜底缓存」里复活。现在缓存存未过滤表，过滤在出口现做。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class StubTrae:
        id = "trae"
        fail = False

        async def list_models(self, _data):
            from src.provider.base import Model

            if self.fail:
                raise RuntimeError("upstream down")
            return [Model(id="glm-5.2"), Model(id="kimi-k2.6")]

        def import_credential(self, raw):
            return raw

    provider = StubTrae()
    app = build_app(settings, providers={"trae": provider})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    key = app.state.api_keys.create("root")["api_key"]
    auth = {"Authorization": f"Bearer {key}"}
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        first = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in first} == {"glm-5.2", "kimi-k2.6"}
        # 缓存存的是**未过滤**表（两条都在），否则热更在 TTL 内无法生效
        assert set(app.state.services.model_list_cache["trae"]) == {"glm-5.2", "kimi-k2.6"}

        # TTL 未过（刚拉过），此时改黑名单
        client.put("/api/settings", json={"values": {"model_blocklist": "kimi-k2.6"}},
                   headers={"Referer": "http://testserver/"})
        second = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in second} == {"glm-5.2"}   # 立即生效

        # 上游随后失败：兜底缓存也不能让被滤模型复活
        provider.fail = True
        third = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in third} == {"glm-5.2"}

        # 清空黑名单（'' 与 null 等价 = 恢复默认）：被滤模型立刻回来
        client.put("/api/settings", json={"values": {"model_blocklist": ""}},
                   headers={"Referer": "http://testserver/"})
        fourth = client.get("/v1/models", headers=auth).json()["data"]
        assert {item["id"] for item in fourth} == {"glm-5.2", "kimi-k2.6"}

def test_codebuddy_import_via_api(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        assert created.status_code == 200
        listed = client.get("/api/credentials").json()["credentials"]
        assert any(row["provider"] == "codebuddy" for row in listed)
        assert any(row["provider"] == "zen" for row in listed)  # 启动即种子
        assert any(row["provider"] == "kilo" for row in listed)  # 启动即种子
        bad = client.post("/api/credentials", json={"provider": "codebuddy",
                                                    "credential": {}})
        assert bad.status_code == 400
