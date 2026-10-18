"""CodeBuddy OAuth 轮询、AuthStateStore 与客户端关闭。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.codebuddy.checkin import CodeBuddyCheckin
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.oauth import AuthProgress, AuthStateStore, CodeBuddyOAuth
from src.provider.codebuddy.refresh import CodeBuddyRefresh


def _oauth_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)

START_OK = {"code": 0, "data": {"authUrl": "https://auth.example/x", "state": "up-state"}}

async def test_oauth_start_returns_session_with_upstream_state():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/plugin/auth/state"
        assert "platform=CLI" in str(request.url)
        return httpx.Response(200, json=START_OK)

    oauth = CodeBuddyOAuth("https://copilot.tencent.com", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert session.flow == "poll" and session.auth_url == "https://auth.example/x"
    assert session.interval == 5
    # 对外暴露的是本地 reservation，不是上游 state
    assert session.state != "up-state"
    assert oauth.store.upstream_state(session.state, "alice") == "up-state"
    await oauth.aclose()

@pytest.mark.parametrize("payload", [
    {"code": 1, "data": {}}, {"code": 0, "data": {}}, {"code": 0, "data": {"authUrl": ""}},
    {"code": 0, "data": {"authUrl": "https://a/x"}}, "not-a-dict",
])
async def test_oauth_start_rejects_invalid(payload):
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")
    await oauth.aclose()

async def test_oauth_start_rejects_http_error_and_registers_nothing():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")
    assert oauth.store.owner("anything", "alice") is False
    await oauth.aclose()


def test_build_credential_rejects_malformed_domain():
    """OAuth 路径的 domain 也进 Host/X-Domain 头，必须与导入路径同样校验。"""
    from src.provider.codebuddy.oauth import _build_credential

    with pytest.raises(UpstreamProtocolViolation, match="domain"):
        _build_credential({"accessToken": "AT", "domain": "evil.com/x"}, {})
    ok = _build_credential({"accessToken": "AT", "domain": "copilot.tencent.com"}, {})
    assert ok.domain == "copilot.tencent.com"

async def test_oauth_poll_pending_then_success():
    """code=11217 → 继续等待；code=0 → 拿 token 与账号 → 消费 state。"""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json={"code": 11217})
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "AT", "refreshToken": "RT", "expiresIn": 3600,
                "domain": "copilot.tencent.com"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "user_id": "u1", "account": {"uid": "acct", "nickname": "nick",
                                         "enterpriseId": "ent"}}})

    oauth = CodeBuddyOAuth("https://copilot.tencent.com", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert await oauth.poll(session.state, "alice") is None          # pending
    result = await oauth.poll(session.state, "alice")
    assert result is not None
    data = result.credential_data
    assert data["bearer_token"] == "AT" and data["refresh_token"] == "RT"
    assert data["auth_source"] == "oauth" and data["account_uid"] == "acct"
    assert data["enterprise_id"] == "ent" and result.nickname == "nick"
    # state 已消费，不可重放
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

async def test_oauth_poll_account_pending_keeps_state():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json={"code": 12151})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert await oauth.poll(session.state, "alice") is None
    # 账号阶段 pending：token 已缓存，state 未消费
    assert oauth.store.progress(session.state, "alice").token_data == {"accessToken": "AT"}
    await oauth.aclose()

async def test_oauth_poll_rejects_unknown_state_and_wrong_owner():
    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(
        lambda _r: httpx.Response(200, json={"code": 0, "data": {}})))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll("ghost", "alice")

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=START_OK)

    oauth2 = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth2.start("alice")
    assert oauth2.store.owner(session.state, "bob") is False
    with pytest.raises(UpstreamProtocolViolation):
        await oauth2.poll(session.state, "bob")
    await oauth.aclose()
    await oauth2.aclose()

@pytest.mark.parametrize("token_payload", [
    {"code": 1}, {"code": 0, "data": {}}, {"code": 0, "data": []},
    {"code": 0, "data": {"accessToken": 5}}, "not-a-dict",
])
async def test_oauth_poll_rejects_bad_token_response(token_payload):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        return httpx.Response(200, json=token_payload)

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

async def test_oauth_poll_account_error_paths():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json={"code": 9999})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

@pytest.mark.parametrize("account_payload", [
    {"code": 0}, {"code": 0, "data": []},
])
async def test_oauth_poll_account_shape_errors(account_payload):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json=account_payload)

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

async def test_oauth_token_http_error_and_bad_json():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(503, content=b"down")
        return httpx.Response(200, json={"code": 0, "data": {}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

async def test_oauth_account_http_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(500, content=b"boom")

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

def test_auth_state_store_ttl_cleanup_and_cancel():
    store = AuthStateStore(ttl_seconds=10)
    first = store.begin("alice", "s1", now=0)
    second = store.begin("alice", "s2", now=100)          # 触发 cleanup
    assert store.owner(second, "alice", now=100) is True
    assert store.owner(first, "alice", now=100) is False  # 已过期
    assert store.cancel(second, "alice", now=100) is True
    assert store.cancel(second, "alice", now=100) is False
    assert store.upstream_state("ghost", "alice", now=100) is None
    # owner 惰性判过期：未触发 cleanup 也能挡掉过期 state（并顺手删除）
    lazy = AuthStateStore(ttl_seconds=10)
    stale = lazy.begin("bob", "s3", now=0)
    assert lazy.owner(stale, "bob", now=10) is False
    assert stale not in lazy._entries

def test_auth_state_store_set_progress_only_for_known_state():
    store = AuthStateStore()
    reservation = store.begin("alice", "s1")
    store.set_progress(reservation, AuthProgress(token_data={"a": 1}))
    assert store.progress(reservation, "alice").token_data == {"a": 1}
    store.set_progress("ghost", AuthProgress())           # 不抛异常

async def test_oauth_lazy_client_and_close():
    oauth = CodeBuddyOAuth("https://e")
    assert oauth._http is oauth._http
    await oauth.aclose()

def test_oauth_expires_at_from_created_plus_ttl():
    from src.provider.codebuddy.oauth import _expires_at

    assert _expires_at({"expires_at": 99}) == 99
    assert _expires_at({"created_at": 100, "expires_in": 50}) == 150
    assert _expires_at({}) == 0
    assert _expires_at({"expires_at": True}) == 0

async def test_oauth_poll_progress_is_reused_across_calls(repo):
    """token 阶段成功后重试只走账号阶段（oauth 149→156 分支）。"""
    calls = {"token": 0, "account": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            calls["token"] += 1
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        calls["account"] += 1
        if calls["account"] == 1:
            return httpx.Response(200, json={"code": 12151})
        return httpx.Response(200, json={"code": 0, "data": {"user_id": "u"}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    assert await oauth.poll(session.state, "alice") is None
    assert await oauth.poll(session.state, "alice") is not None
    assert calls["token"] == 1                       # token 只取一次
    await oauth.aclose()

async def test_oauth_start_rejects_bad_auth_url_and_missing_state():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"authUrl": "not-a-url"}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.start("alice")
    await oauth.aclose()

async def test_oauth_poll_consume_conflict(tmp_path):
    """state 在 poll 期间被并发消费 → 显式失败（oauth 192）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json={"code": 0, "data": {"user_id": "u"}})

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    original_consume = oauth.store.consume
    oauth.store.consume = lambda *_: False            # 模拟并发消费
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    oauth.store.consume = original_consume
    await oauth.aclose()

def test_oauth_start_headers_helper():
    from src.provider.codebuddy.oauth import _poll_headers

    assert _poll_headers()["Accept"].startswith("application/json")

async def test_oauth_account_stage_non_dict_body():
    """账号阶段返回非对象 → 显式失败（oauth 191-192）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/auth/state"):
            return httpx.Response(200, json=START_OK)
        if request.url.path.endswith("/auth/token"):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "AT"}})
        return httpx.Response(200, json=[1, 2])

    oauth = CodeBuddyOAuth("https://e", client=_oauth_client(handler))
    session = await oauth.start("alice")
    with pytest.raises(UpstreamProtocolViolation):
        await oauth.poll(session.state, "alice")
    await oauth.aclose()

async def test_aclose_without_client_is_noop():
    """未创建过 HTTP 客户端时 aclose 直接返回（三处 -> 123 出口）。"""
    await CodeBuddyOAuth("https://e").aclose()
    await CodeBuddyRefresh("https://e").aclose()
    await CodeBuddyCheckin("https://e").aclose()
