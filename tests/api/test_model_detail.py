"""`GET /v1/models/{id}` 单模型查询：三种写法、白名单 404、能力分透出。

对外 id / 展示名 / 任一渠道原 id 都要认（与执行侧别名表同口径）；白名单外与
不存在统一 404，不能让客户端用单查探测「池子里还有哪些模型」。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET


class _Stub:
    """最小渠道替身：`list_models` 返回固定表。"""

    def __init__(self, pid: str, models: list[tuple[str, str]]) -> None:
        self.id = pid
        self._models = models

    async def list_models(self, _data):
        from src.provider.base import Model

        return [Model(id=model_id, name=name) for model_id, name in self._models]

    def import_credential(self, raw):  # pragma: no cover - 未使用
        return raw


def _app(tmp_path, providers: dict | None = None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers=providers or {
        "trae": _Stub("trae", [("glm-5.2", "GLM 5.2"), ("kimi-k3-1", "Kimi-K3")]),
        "kilo": _Stub("kilo", [("dots-studio/dots-3-note-preview:free",
                                "Dots 3 Note Preview")]),
    })
    for provider in ("trae", "kilo"):
        app.state.credentials.add(provider=provider,
                                  credential_data={"accessToken": "a", "token": "t"})
    return app


def _key(app, allowed_models: str = "") -> dict[str, str]:
    created = app.state.api_keys.create("root", allowed_models=allowed_models)
    return {"Authorization": f"Bearer {created['api_key']}"}


def test_single_model_by_public_id(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/models/glm-5.2", headers=_key(app))
    assert response.status_code == 200
    item = response.json()
    assert item["id"] == "glm-5.2" and item["object"] == "model"
    assert item["providers"] == ["trae"]

def test_single_model_accepts_display_name_and_case_insensitive(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _key(app)
        assert client.get("/v1/models/GLM%205.2", headers=headers).json()["id"] == "glm-5.2"
        assert client.get("/v1/models/Glm-5.2", headers=headers).json()["id"] == "glm-5.2"

def test_single_model_accepts_raw_upstream_id_with_namespace(tmp_path):
    """`:path` 路由：带斜杠的原代号（kilo）也要能单查，否则列表有、单查 404。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/models/dots-studio/dots-3-note-preview:free",
                              headers=_key(app))
    assert response.status_code == 200
    assert response.json()["id"] == "dots-3-note-preview"

def test_single_model_404_for_unknown_id(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/models/no-such-model", headers=_key(app))
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "model_not_found" and error["status"] == 404
    assert "no-such-model" in error["message"]

def test_single_model_outside_key_allowlist_is_404(tmp_path):
    """白名单外与不存在同形 404：不能让单查成为「池里有什么」的探测口。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        headers = _key(app, allowed_models="glm-5.2")
        assert client.get("/v1/models/glm-5.2", headers=headers).status_code == 200
        hidden = client.get("/v1/models/kimi-k3", headers=headers)
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "model_not_found"

def test_single_model_requires_api_key(tmp_path):
    app = _app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/v1/models/glm-5.2").status_code == 401

def test_single_model_carries_benchmarks_when_available(tmp_path):
    app = _app(tmp_path)
    app.state.model_benchmarks.update({"glm-5.2": {"intelligence": 42}})
    with TestClient(app) as client:
        item = client.get("/v1/models/glm-5.2", headers=_key(app)).json()
    assert item["benchmarks"]["intelligence"] == 42

def test_single_model_blank_id_is_404(tmp_path):
    """空路径段（`/v1/models/`）落到列表端点；纯空白 id 也按未命中处理。"""
    app = _app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/v1/models/%20", headers=_key(app))
    assert response.status_code == 404
