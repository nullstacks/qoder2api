"""聊天出口端到端：鉴权/管理台冒烟、错误分类与 400 轮换边界。"""

from __future__ import annotations

import httpx
import pytest

from src.config import Settings
from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler
from src.main import build_app
from src.provider.codebuddy.client import CodeBuddyProvider
from tests.api._support import RejectProvider as _RejectProvider
from tests.api._support import http_400 as _http_400
from tests.helpers.chat import chat_request as _request
from tests.helpers.constants import SECRET
from tests.helpers.events import GOOD
from tests.helpers.events import TRAE_GOOD as GOOD_trae
from tests.helpers.providers import ScriptedProvider as FakeProvider
from tests.provider.codebuddy._support import client as _client


@pytest.fixture()
def client(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": FakeProvider([GOOD_trae])})
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client

def _login(client, username="root"):
    from src.auth.session import create_session_token

    token = create_session_token(username, SECRET)
    client.cookies.set("coding2api_session", token)

def test_health_endpoint(client):
    assert client.get("/health").json() == {"status": "ok"}

def test_api_requires_key(client):
    response = client.post("/v1/chat/completions", json={"messages": [{"role": "user"}]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"

def test_api_rejects_bad_key(client):
    response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"},
                           json={"messages": [{"role": "user"}]})
    assert response.status_code == 401

def test_full_flow_apikey_chat_and_models(client, tmp_path):
    app = client.app
    app.state.credentials.add(provider="trae", credential_data={"accessToken": "a"})
    created = app.state.api_keys.create("root", "test")
    headers = {"Authorization": f"Bearer {created['api_key']}"}
    models = client.get("/v1/models", headers=headers)
    assert models.status_code == 200
    assert models.json()["data"][0]["providers"] == ["trae"]
    chat = client.post("/v1/chat/completions", headers=headers,
                       json={"messages": [{"role": "user", "content": "hi"}]})
    assert chat.status_code == 200
    assert chat.json()["choices"][0]["message"]["content"] == "hi"

def test_api_validation_error(client):
    app = client.app
    key = app.state.api_keys.create("root")["api_key"]
    headers = {"Authorization": f"Bearer {key}"}
    response = client.post("/v1/chat/completions", headers=headers, json={"messages": []})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_admin_endpoints_require_session(client):
    assert client.get("/api/credentials").status_code == 401

def test_admin_credential_lifecycle(client):
    _login(client)
    created = client.post("/api/credentials", json={
        "provider": "trae", "credential": {"accessToken": "a", "uid": "u"}, "nickname": "n"})
    assert created.status_code == 200
    credential_id = created.json()["id"]
    assert client.get("/api/credentials").json()["credentials"][0]["id"] == credential_id
    pinned = client.post("/api/credentials/pin", json={"credential_id": credential_id})
    assert pinned.status_code == 200
    assert client.post(f"/api/credentials/{credential_id}/toggle",
                       json={"enabled": False}).status_code == 200
    assert client.delete(f"/api/credentials/{credential_id}").status_code == 200
    assert client.delete(f"/api/credentials/{credential_id}").status_code == 400

def test_admin_import_rejects_unknown_provider(client):
    _login(client)
    response = client.post("/api/credentials", json={"provider": "nope", "credential": {}})
    assert response.status_code == 400

def test_admin_import_rejects_bad_credential(client):
    _login(client)
    response = client.post("/api/credentials", json={"provider": "trae", "credential": {}})
    assert response.status_code == 400

def test_api_key_crud(client):
    _login(client)
    created = client.post("/api/api-keys", json={"name": "k"})
    assert created.status_code == 200 and created.json()["api_key"].startswith("sk-")
    key_id = created.json()["id"]
    assert client.get("/api/api-keys").json()["api_keys"][0]["id"] == key_id
    assert client.delete(f"/api/api-keys/{key_id}").status_code == 200
    assert client.delete(f"/api/api-keys/{key_id}").status_code == 400

def test_non_admin_cannot_write(client):
    _login(client, username="guest")
    assert client.post("/api/credentials", json={"provider": "trae",
                                                 "credential": {}}).status_code == 403

def test_authorize_rejects_callback_without_pending_login(client):
    """没有进行中的登录时，任意回调不得被塞进凭证池。"""
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_authorize_expires_stale_pending_login(client):
    """待完成登录有 TTL：/authorize 无鉴权，长期挂着的 pending 会被同网络
    任何人用自己的 refreshToken 完成兑换（凭证塞进池子）。过期即作废。"""
    import time as _time

    client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
    client.app.state.pending_callbacks["root"] = (_time.monotonic() - 601, "machine:device")
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert "expired" in response.json()["error"]["message"]
    assert "root" not in client.app.state.pending_callbacks

def test_authorize_requires_initiating_session(client):
    """回调必须由发起登录的本人完成：会话缺失 → 拒绝。

    防止同网段的人用自己的 refreshToken 顶掉别人进行中的登录、把凭证塞进池子。
    """
    import time as _time

    client.app.state.pending_callbacks["root"] = (_time.monotonic(), "machine:device")
    response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    # 未被消费：真正的发起人仍可完成
    assert "root" in client.app.state.pending_callbacks

async def test_http_400_does_not_cooldown_or_rotate(dual_repo):
    """上游 400（模型不存在等）→ 抛 InvalidRequest；凭证不冷却、不轮换。"""
    from src.compat.openai.request import InvalidRequest

    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(400, json={"code": 1002, "msg": "model not found"})

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    provider = CodeBuddyProvider(client=_client(handler))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo, scheduler=Scheduler(),
        default_model="qwen3.8"))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8"), username="u")
    assert "qwen3.8" in str(exc_info.value)
    assert calls["n"] == 1                      # 未轮换重试
    # 凭证零错误记录、无冷却、未禁用
    row = db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials").fetchone()
    assert tuple(row) == (0, None, 0)

async def test_http_400_stream_yields_invalid_request_frame(dual_repo):
    """流式路径：400 → invalid_request 错误帧；凭证不冷却、不轮换。"""
    import json as _json

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"code": 1002, "msg": "model not found"})

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "t1"})
    provider = CodeBuddyProvider(client=_client(handler))
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo, scheduler=Scheduler(),
        default_model="qwen3.8"))

    frames = [f async for f in executor.stream(_request("qwen3.8"), username="u")]
    assert len(frames) == 1                     # 错误帧（内含 [DONE]）
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["type"] == "api_error"
    assert payload["error"]["code"] == "invalid_request"
    assert "qwen3.8" in payload["error"]["message"]
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert tuple(row) == (0, None)

async def test_invalid_skips_provider_and_falls_back_to_next(dual_repo):
    """CB 400（不认识模型）→ 跳过 CB，TRAE 成功接住；凭证零冷却。"""
    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    # pin CB 保证它先被选中，从而触发「400 → 跳过 → TRAE 接住」路径
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'codebuddy'")
    cb = _RejectProvider("codebuddy", [_http_400()])
    trae = _RejectProvider("trae", [GOOD])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    result = await executor.complete(_request("qwen3.8-max"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert cb.calls == 1 and trae.calls == 1
    assert tuple(db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials").fetchone()) \
        is not None
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials")]
    assert rows == [(0, None, 0), (0, None, 0)]     # 谁都没被冷却

async def test_all_upstreams_reject_yields_400(dual_repo):
    """两个上游都 400 → InvalidRequest（400 语义），凭证零冷却。"""
    from src.compat.openai.request import InvalidRequest

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "trae"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    trae = _RejectProvider("trae", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb, "trae": trae}, credentials=repo,
        scheduler=Scheduler(), default_model="qwen3.8-max"))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8-max"), username="u")
    assert "qwen3.8-max" in str(exc_info.value)
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until, disabled FROM credentials")]
    assert rows == [(0, None, 0), (0, None, 0)]

async def test_stream_rotate_exhausted_with_invalid_last_error(dual_repo):
    """流式 + 轮换耗尽时最后错误是 INVALID → invalid_request 帧（155-159）。"""
    import json as _json

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen3.8"))

    frames = [f async for f in executor.stream(_request("qwen3.8"), username="u")]
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["code"] == "invalid_request"
    assert "qwen3.8" in payload["error"]["message"]

async def test_complete_rotate_exhausted_with_invalid_last_error(dual_repo):
    """非流式 + 轮换耗尽 + 最后错误 INVALID → 400（executor 238）。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    cb = _RejectProvider("codebuddy", [_http_400()])
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="qwen3.8"))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("qwen3.8"), username="u")

async def test_executor_invalid_request_includes_suggestions(dual_repo):
    """全部上游 400 → 400 文案附相近模型建议。"""
    from src.compat.openai.request import InvalidRequest

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "tr"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": _RejectProvider("codebuddy", [_http_400()]),
                   "trae": _RejectProvider("trae", [_http_400()])},
        credentials=repo, scheduler=Scheduler(), default_model="qwen3.8-max",
        model_suggestions=lambda name: ["qwen-3.7-plus"]))

    with pytest.raises(InvalidRequest) as exc_info:
        await executor.complete(_request("qwen3.8-max"), username="u")
    assert "qwen-3.7-plus" in str(exc_info.value)
