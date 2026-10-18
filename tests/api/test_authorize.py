"""TRAE 回调登录闭环与 /authorize。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.provider.trae.callback import login_trace_id
from src.provider.trae.client import TraeProvider
from tests.helpers.constants import SECRET


async def test_poll_pending_returns_status_pending(admin_client):
    """轮询 pending → {"status": "pending"}（main 237-238）。"""
    app, client = admin_client
    oauth = app.state.upstream_auth["codebuddy"]

    async def fake_poll(_state, _username):
        return None

    oauth.poll = fake_poll
    body = client.post("/api/auth/upstream/poll",
                       json={"provider": "codebuddy", "state": "any"}).json()
    assert body == {"status": "pending"}

def test_trae_start_auth_builds_login_url_with_public_callback():
    from src.api.admin_auth import resolve_public_callback_url

    settings = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="https://gw.example")
    provider = TraeProvider()
    session = provider.start_auth(resolve_public_callback_url(settings))

    assert session.flow == "callback"
    assert session.callback_url == "https://gw.example/authorize"
    assert "auth_callback_url=https%3A%2F%2Fgw.example%2Fauthorize" in session.auth_url
    machine_id, _, device_id = session.state.partition(":")
    assert len(machine_id) == 32 and len(device_id) == 32

async def test_trae_complete_callback_exchanges_token():
    """回调链接必须真的换 token，而不是只存 refreshToken。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {
                "Token": "ACCESS", "RefreshToken": "RT2", "TokenExpireAt": 1_800_000_000_000}})
        return _httpx.Response(200, json={"Result": {"UserID": "uid-9", "ScreenName": "昵称"}})

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    session = provider.start_auth("https://gw.example/authorize")
    url = ("https://gw.example/authorize?refreshToken=RT1&userInfo="
           "%7B%22uid%22%3A%22%22%7D")
    data = await provider.complete_callback(url, session.state)

    assert data["accessToken"] == "ACCESS"
    assert data["refreshToken"] == "RT2"
    assert data["expiresAt"] == 1_800_000_000
    assert data["uid"] == "uid-9"          # 回调没给 uid 时回退 GetUserInfo
    assert data["nickname"] == "昵称"
    assert data["machineId"] == session.state.partition(":")[0]

async def test_trae_complete_callback_rejects_bad_state():
    from src.provider.trae.events import UpstreamProtocolViolation

    provider = TraeProvider()
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback("https://x/authorize?refreshToken=RT", "no-colon")

def test_authorize_completes_trae_login_end_to_end(tmp_path):
    """完整闭环：start → 浏览器回调 → 凭证入库 → 立即探测。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        if request.url.endswith("ide_user_ent_usage"):
            return _httpx.Response(200, json={"user_entitlement_pack_list": [
                {"entitlement_base_info": {"quota": {"credits_limit": 100}},
                 "usage": {"credits_amount": 25}}]})
        return _httpx.Response(200, json={"Result": {"UserID": "u1", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    trae = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": trae})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        callback = ("/authorize?refreshToken=RT1&userInfo=%7B%22uid%22%3A%22u1%22%7D"
                    f"&state={started['state']}"
                    f"&loginTraceID={login_trace_id(*started['state'].split(':'))}")
        response = client.get(callback)
        assert response.status_code == 200 and response.json()["captured"] is True

        listed = client.get("/api/credentials").json()["credentials"]
        assert len(listed) == 1 and listed[0]["provider"] == "trae"
        # 响应与列表都不得泄漏 token
        assert "ACCESS" not in response.text
        assert "data_enc" not in listed[0]

    # state 已消费，同一回调不可重放
    with TestClient(app) as replay:
        assert replay.get(callback).status_code == 400

def test_authorize_rejects_callback_without_token(tmp_path):
    """带 state 与正确 loginTraceID 但没有 refreshToken/userJwt 的回调必须拒绝。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()
        trace = login_trace_id(*started["state"].split(":"))
        response = client.get(f"/authorize?state={started['state']}&loginTraceID={trace}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_authorize_rejects_callback_with_wrong_login_trace_id(tmp_path):
    """绑定校验：缺失 / 不匹配的 loginTraceID 一律拒绝，且不消费 pending。

    这是 `/authorize` 的跨站防护——攻击者能带上受害者会话 Cookie（顶层导航
    会带 SameSite=Lax），但拿不到发起那次登录的 trace id，因此无法用自己的
    refreshToken 顶掉别人进行中的登录。
    """
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        # 缺失
        missing = client.get(f"/authorize?refreshToken=RT&state={started['state']}")
        assert missing.status_code == 400
        assert missing.json()["error"]["message"] == "callback does not match the pending login"
        # 不匹配（攻击者自造的 trace）
        wrong = client.get(f"/authorize?refreshToken=RT&state={started['state']}"
                           "&loginTraceID=0000000000000000")
        assert wrong.status_code == 400
        assert wrong.json()["error"]["message"] == "callback does not match the pending login"
        # 被拒的回调不消费 pending：发起人仍能用正确 trace 走完流程
        assert "root" in app.state.pending_callbacks

def test_upstream_start_uses_poll_track_for_codebuddy(admin_client):
    """provider 在 upstream_auth 里时走 poll 轨道（main 275-276）。"""
    app, client = admin_client

    class FakeOAuth:
        def __init__(self) -> None:
            self.store = type("S", (), {"cancel": staticmethod(lambda *_: True)})()

        async def start(self, username):
            from src.provider.base import AuthSession

            return AuthSession(flow="poll", state="local-reservation",
                               auth_url="https://auth.example/x", interval=5)

    app.state.upstream_auth["codebuddy"] = FakeOAuth()
    body = client.post("/api/auth/upstream/start", json={"provider": "codebuddy"}).json()
    assert body["flow"] == "poll"
    assert body["state"] == "local-reservation"
    assert body["callback_url"] is None

async def test_complete_callback_does_not_use_refresh_token_as_access_token():
    """ExchangeToken 失败时不得把 refreshToken 当 accessToken 塞进池子。"""
    import httpx as _httpx

    from src.provider.trae.client import TraeClient
    from src.provider.trae.events import UpstreamProtocolViolation

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(400, content=b"bad refresh token")
        return _httpx.Response(200, json={"Result": {"UserID": "u", "ScreenName": "n"}})

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    session = provider.start_auth("https://gw.example/authorize")
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback(
            "https://gw.example/authorize?refreshToken=RT", session.state)

async def test_complete_callback_raises_when_no_token_available():
    import httpx as _httpx

    from src.provider.trae.client import TraeClient
    from src.provider.trae.events import UpstreamProtocolViolation

    def handler(_request: _httpx.Request) -> _httpx.Response:
        return _httpx.Response(400, content=b"rejected")

    transport = _httpx.MockTransport(handler)
    provider = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))
    session = provider.start_auth("https://gw.example/authorize")
    with pytest.raises(UpstreamProtocolViolation):
        await provider.complete_callback(
            "https://gw.example/authorize?refreshToken=RT", session.state)

def test_authorize_reports_invalid_credential(tmp_path):
    """回调结构损坏 → 400 invalid_credential（main 446-448）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        client.post("/api/auth/upstream/start", json={"provider": "trae"})
        # 有 refreshToken 但状态串损坏 → complete_callback 抛协议违规
        app.state.pending_callbacks["root"] = (time.monotonic(), "broken")
        response = client.get("/authorize?refreshToken=RT&state=broken"
                              "&loginTraceID=broken")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_credential"

def test_authorize_accepts_real_trae_callback_without_state(tmp_path):
    """TRAE 回跳只带 refreshToken/userInfo，不会回传我们的 state。

    之前实现要求 state 匹配，导致真实登录必然 400。
    """
    import httpx as _httpx

    from src.provider.trae.client import TraeClient

    def handler(request: _httpx.Request) -> _httpx.Response:
        if request.url.path.endswith("ExchangeToken"):
            return _httpx.Response(200, json={"Result": {"Token": "ACCESS",
                                                         "RefreshToken": "RT2"}})
        return _httpx.Response(200, json={"Result": {"UserID": "u9",
                                                     "ScreenName": "真名"}})

    transport = _httpx.MockTransport(handler)
    trae = TraeProvider(client=TraeClient(
        stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
        short_client=_httpx.AsyncClient(transport=transport, timeout=None)))

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings, providers={"trae": trae})

    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start", json={"provider": "trae"}).json()

        # 与真实回调一致：不带 state 参数，但带 TRAE 回显的 loginTraceID
        trace = login_trace_id(*started["state"].split(":"))
        callback = ("/authorize?refreshToken=RT1&userInfo="
                    "%7B%22uid%22%3A%22uid-9%22%2C%22nickname%22%3A%22%E7%9C%9F%E5%90%8D%22%7D"
                    f"&loginTraceID={trace}")
        response = client.get(callback)
        assert response.status_code == 200, response.text

        listed = client.get("/api/credentials").json()["credentials"]
        assert len(listed) == 1 and listed[0]["provider"] == "trae"
        assert listed[0]["nickname"] == "真名"
        # machine/device id 来自 pending state，与登录 URL 一致
        assert started["state"].partition(":")[0] in started["auth_url"]

    # 回调成功后 pending 已清空：同一链接重放必须拒绝
    with TestClient(app) as replay:
        assert replay.get(callback).status_code == 400

def test_authorize_without_pending_login_still_rejected(tmp_path):
    """没有进行中的登录时，带 refreshToken 的回调依然拒绝（防乱塞池子）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"

def test_authorize_rejects_revoked_session(tmp_path, monkeypatch):
    """签名有效但发起人已禁用：不得完成兑换（与 principal_from_request 同口径）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"username": "root", "password": "rootpw"})
        started = client.post("/api/auth/upstream/start",
                              json={"provider": "trae"}).json()
        # Cookie 仍有效（签名/过期都 OK），但发起人已被禁用
        monkeypatch.setattr(app.state.services.users, "is_active", lambda _u: False)
        response = client.get(f"/authorize?refreshToken=RT&state={started['state']}")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


def test_dump_request_bodies_writes_file(tmp_path):
    """诊断开关：dump_request_bodies=True 时 /v1 请求体落盘（main 291-293）。"""
    import json as _json

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        dump_request_bodies=True)

    class P:
        id = "p1"

        async def list_models(self, _data):
            from src.provider.base import Model

            return [Model(id="m")]

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={"p1": P()})
    key = app.state.api_keys.create("root")["api_key"]
    with TestClient(app) as client:
        r = client.post("/v1/chat/completions",
                        headers={"Authorization": f"Bearer {key}"},
                        json={"model": "m", "stream": False,
                              "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code in (200, 400, 503)  # body 是否合法不影响 dump
        dumps = list((tmp_path / "dumps").glob("*.json"))
        assert dumps, "应产生 dump 文件"
        body = _json.loads(dumps[0].read_text(encoding="utf-8"))
        assert body["model"] == "m"

def test_authorize_without_trae_provider_is_rejected(tmp_path):
    """registry 里没有 trae 渠道时，/authorize 直接 400（不崩）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))
    app = build_app(settings, providers={})
    with TestClient(app) as client:
        response = client.get("/authorize?refreshToken=RT")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
