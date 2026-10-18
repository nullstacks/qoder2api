"""CodeBuddy 活跃上报：user_id 解析、事件体、HTTP 客户端与 provider 委托。"""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from src.provider.codebuddy.activity import (
    EP_REPORT,
    ActivityRejected,
    CodeBuddyActivity,
    chat_request_event,
    resolve_user_id,
    user_id_from_token,
)
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.events import UpstreamProtocolViolation


def _jwt(claims: dict) -> str:
    """造一个只有 payload 有意义的假 JWT（签名段任意）。"""
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"

def _client(handler) -> CodeBuddyActivity:
    transport = httpx.MockTransport(handler)
    return CodeBuddyActivity(
        "https://copilot.tencent.com",
        client=httpx.AsyncClient(transport=transport, timeout=None))

def test_user_id_from_token_reads_sub():
    assert user_id_from_token(_jwt({"sub": "abc-123"})) == "abc-123"
    # sub 前后空白归一
    assert user_id_from_token(_jwt({"sub": " abc "})) == "abc"

@pytest.mark.parametrize("token", [
    "", "not-a-jwt", "a.b", "a.b.c.d",           # 段数不对
    "header.!!!.sig",                             # payload 非 base64
    _jwt({"sub": ""}), _jwt({"sub": 123}),        # sub 缺失/非字符串
    _jwt({"other": "x"}),                         # 无 sub
    "header." + base64.urlsafe_b64encode(b'"str"').decode().rstrip("=") + ".sig",
])
def test_user_id_from_token_rejects_bad_input(token):
    assert user_id_from_token(token) == ""

def test_resolve_user_id_prefers_credential_then_token():
    # 凭证里有 account_uid 优先
    cred = CodeBuddyCredential(account_uid="uid", bearer_token=_jwt({"sub": "sub"}))
    assert resolve_user_id(cred) == "uid"
    # 回落 user_id
    cred = CodeBuddyCredential(user_id="uid2", bearer_token=_jwt({"sub": "sub"}))
    assert resolve_user_id(cred) == "uid2"
    # 都为空 → 用 token sub
    cred = CodeBuddyCredential(bearer_token=_jwt({"sub": "sub"}))
    assert resolve_user_id(cred) == "sub"
    # 全无 → 空串
    assert resolve_user_id(CodeBuddyCredential()) == ""

def test_chat_request_event_shape():
    event = chat_request_event("u1", now_ms=1700000000000)
    assert event["eventCode"] == "chat_request_send"
    assert event["userId"] == "u1"
    assert event["timestamp"] == event["presentAt"] == 1700000000000
    assert event["conversationId"] == event["requestId"] == event["rootRequestId"]
    assert event["parentConversationId"] == event["conversationId"]
    assert event["agentName"] == "default" and event["agentType"] == "conversation"
    assert event["mentionContexts"] == [] and event["knowledgeId"] == []
    # 全字段形状：键数与参考实现一致（36 键），防上游后续加严
    assert len(event) == 36
    # 显式 conversationId 可用
    fixed = chat_request_event("u1", conversation_id="conv-1")
    assert fixed["conversationId"] == fixed["requestId"] == "conv-1"
    assert fixed["parentConversationId"] == "conv-1"

async def test_report_chat_posts_event_array_with_user_id_header():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "msg": "OK"})

    cred = CodeBuddyCredential(bearer_token=_jwt({"sub": "uid-9"}))
    result = await _client(handler).report_chat(cred)
    assert result.ok is True and result.user_id == "uid-9"
    assert seen["url"] == f"https://copilot.tencent.com{EP_REPORT}"
    assert seen["headers"]["x-user-id"] == "uid-9"
    assert isinstance(seen["body"], list) and len(seen["body"]) == 1
    assert seen["body"][0]["userId"] == "uid-9"
    assert seen["body"][0]["eventCode"] == "chat_request_send"

async def test_report_chat_without_user_id_never_calls_upstream():
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"code": 0})

    result = await _client(handler).report_chat(CodeBuddyCredential(bearer_token="x"))
    assert result.ok is False and "userId" in result.message
    assert called is False

@pytest.mark.parametrize("status,body", [
    (500, {"code": 0, "msg": "boom"}),
    (401, {"code": 11101, "msg": "token expired"}),
    (200, {"code": 1234, "msg": "rejected"}),          # 业务失败也是 200
])
async def test_report_chat_surfaces_failures(status, body):
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    with pytest.raises((ActivityRejected, UpstreamProtocolViolation)):
        await _client(handler).report_chat(
            CodeBuddyCredential(bearer_token=_jwt({"sub": "u1"})))

async def test_report_chat_rejects_non_json_and_non_object():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("nonjson"):
            return httpx.Response(200, content=b"<html>")
        return httpx.Response(200, json=["array"])

    cred = CodeBuddyCredential(bearer_token=_jwt({"sub": "u1"}))
    # 非 JSON → UpstreamProtocolViolation
    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).report_chat(cred)
    # 非对象信封 → UpstreamProtocolViolation（另一个 handler）
    def handler2(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=["array"])

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler2).report_chat(cred)

async def test_activity_client_lazy_http_and_close():
    """未注入 client 时惰性建连；aclose 幂等（未建连也不报错）。"""
    client = CodeBuddyActivity("https://copilot.tencent.com")
    assert client._http is client._http          # noqa: SLF001 - 同一实例复用
    await client.aclose()
    await client.aclose()                        # 再关一次不报错
    empty = CodeBuddyActivity("https://x")
    await empty.aclose()                         # 从未建连

async def test_report_chat_non_json_body():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json")

    with pytest.raises(UpstreamProtocolViolation):
        await _client(handler).report_chat(
            CodeBuddyCredential(bearer_token=_jwt({"sub": "u1"})))

def test_activity_message_of_falls_back_to_empty():
    """失败响应非 JSON / 非对象 / message 非字符串时返回空串。"""
    from src.provider.codebuddy.activity import _message_of

    request = httpx.Request("POST", "https://x/y")
    assert _message_of(httpx.Response(500, text="oops", request=request)) == ""
    assert _message_of(httpx.Response(500, json=["array"], request=request)) == ""
    assert _message_of(httpx.Response(
        500, json={"message": 7}, request=request)) == ""
    assert _message_of(httpx.Response(
        500, json={"message": "fallback"}, request=request)) == "fallback"

async def test_provider_activity_uses_token_sub(monkeypatch):
    """CodeBuddyProvider.activity 走 _cached_activity，userId 取自 JWT sub。"""
    from src.provider.codebuddy.client import CodeBuddyProvider

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"code": 0, "msg": "OK"})

    transport = httpx.MockTransport(handler)
    provider = CodeBuddyProvider()
    provider.client._short_client = httpx.AsyncClient(  # noqa: SLF001
        transport=transport, timeout=None)
    credential = {"bearer_token": _jwt({"sub": "uid-77"})}
    result = await provider.activity(credential)
    assert result.ok is True
    await provider.activity(credential)          # 第二次命中 _ACTIVITY_CACHE
    assert seen["body"][0]["userId"] == "uid-77"
    assert seen["headers"]["x-user-id"] == "uid-77"
    # activity_scope：endpoint + userId
    assert provider.activity_scope(credential) == (
        f"{provider.client.endpoint.rstrip('/')}|uid-77")
    # 身份完全缺失 → 空 scope（调用方回落 credential_id）
    assert provider.activity_scope({"bearer_token": "x"}) == ""
    await provider.aclose()

async def test_provider_activity_rejects_missing_user_id():
    from src.provider.codebuddy.client import CodeBuddyProvider

    provider = CodeBuddyProvider()
    result = await provider.activity({"bearer_token": "not-a-jwt"})
    assert result.ok is False and "userId" in result.message
