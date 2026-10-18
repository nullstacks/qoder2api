"""Playground 会话端点（无需 API Key）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET
from tests.helpers.events import GOOD_EVENTS


class _PlaygroundProvider:
    id = "trae"

    def __init__(self, script=None) -> None:
        # script 是「按尝试次数」分组的段：每段是一批 Event（或一个异常）
        self.script = script or []
        self.calls = 0

    async def stream_chat(self, _data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        segment = self.script[index]
        if isinstance(segment, Exception):
            raise segment
        for item in segment:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _data):
        from src.provider.base import Model

        return [Model(id="glm-5.2"), Model(id="trae-only")]

    def import_credential(self, raw):  # pragma: no cover - 非本测试路径
        return raw

def _playground_app(tmp_path, script=None):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": _PlaygroundProvider(script)})
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    return app

def test_playground_models_uses_session(tmp_path):
    app = _playground_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/playground/models").status_code == 401
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        body = client.get("/api/playground/models").json()
    assert [m["id"] for m in body["data"]] == ["glm-5.2", "trae-only"]
    assert body["data"][0]["providers"] == ["trae"]

def test_playground_chat_requires_session(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 401

def test_playground_chat_works_without_api_key(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "hi"

def test_playground_chat_stream(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions",
                               json={"messages": [{"role": "user", "content": "hi"}],
                                     "stream": True})
    assert "text/event-stream" in response.headers["content-type"]
    assert "data: [DONE]" in response.text

def test_playground_attributes_usage_to_session_user(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        client.post("/api/playground/chat/completions",
                    json={"messages": [{"role": "user", "content": "hi"}]})
    overview = app.state.stats_query.overview(username="alice")
    assert overview["requests"] == 1 and overview["ok_count"] == 1

def test_playground_invalid_request_returns_400(tmp_path):
    app = _playground_app(tmp_path, [GOOD_EVENTS])
    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        response = client.post("/api/playground/chat/completions", json={"messages": []})
    assert response.status_code == 400

def test_v1_models_and_playground_models_are_consistent(tmp_path):
    app = _playground_app(tmp_path)
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        v1 = client.get("/v1/models", headers={"Authorization": f"Bearer {key}"}).json()
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        playground = client.get("/api/playground/models").json()
    assert v1 == playground
