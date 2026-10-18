"""CodeBuddyProvider 的签到 / 刷新委托。"""

from __future__ import annotations

import httpx

from src.provider.codebuddy.client import CodeBuddyProvider
from tests.helpers.constants import SWITCH_PATH


def _refresh_client_provider(handler) -> object:
    from src.provider.codebuddy.client import CodeBuddyClient

    return CodeBuddyClient(
        stream_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None),
        short_client=httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None))

async def test_provider_checkin_scope_and_refresh_delegation():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/daily-checkin"):
            return httpx.Response(200, json={"code": 0, "data": {"credit": 7}})
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {}})
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": [
                {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    data = {"bearer_token": "t", "account_uid": "acct"}
    assert provider.checkin_scope(data).endswith("|acct")
    result = await provider.checkin(data)
    assert result.ok and result.credit == 7
    assert [a.account_id for a in await provider.list_accounts(data)] == ["a1"]
    switched = await provider.switch_account(data, "a1")
    assert switched["account_uid"] == "a1"

async def test_provider_refresh_skips_bearer_only_manual_credential():
    """bearer-only 手动凭证不进入刷新流程，原样返回（AGENTS.md 约束）。"""
    provider = CodeBuddyProvider(client=_refresh_client_provider(
        lambda _r: httpx.Response(500, content=b"should not be called")))
    data = {"bearer_token": "t", "auth_source": "manual"}
    assert await provider.refresh(data) == data

async def test_provider_refresh_updates_oauth_credential():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": []}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW",
                                                             "expiresIn": 100}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    refreshed = await provider.refresh({"bearer_token": "old", "refresh_token": "RT",
                                        "auth_source": "oauth"})
    assert refreshed["bearer_token"] == "NEW" and refreshed["auth_source"] == "oauth"

def test_provider_cached_clients_are_reused():
    """_cached_checkin/_cached_refresh 复用同一实例（client 200→203）。"""
    from src.provider.codebuddy.client import (
        CodeBuddyClient,
        _cached_checkin,
        _cached_refresh,
    )

    client = CodeBuddyClient(endpoint="https://e")
    assert _cached_checkin(client) is _cached_checkin(client)
    assert _cached_refresh(client) is _cached_refresh(client)

async def test_provider_checkin_and_scope_roundtrip():
    """provider.checkin 走缓存客户端（client 259）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"credit": 3}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    result = await provider.checkin({"bearer_token": "t", "account_uid": "a"})
    assert result.ok and result.credit == 3.0
    assert provider.checkin_scope({"bearer_token": "t", "user_id": "u"}).endswith("|u")

async def test_provider_checkin_status_and_anonymous_scope():
    """checkin_status 只读状态；身份未知时 scope 返回空串（交由任务回落凭证 ID）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/checkin-activity-status")
        return httpx.Response(200, json={"code": 0, "data": {
            "active": True, "today_checked_in": True, "streak_days": 7,
            "today_credit": 100, "total_credits": 700,
            "activity_name": "高校新生攻略", "is_streak_day": True}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    status = await provider.checkin_status({"bearer_token": "t"})
    assert status["streak_days"] == 7 and status["today_checked_in"] is True
    assert status["activity_name"] == "高校新生攻略"
    # 空身份 → 空串（不是 "https://e|"，否则所有 CB 凭证会共用同一 scope）
    assert provider.checkin_scope({"bearer_token": "t"}) == ""

async def test_provider_checkin_without_status_keeps_none():
    """领取失败（无状态回填）时 provider 不构造 status（中立层保持 None）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 5, "msg": "boom"})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    result = await provider.checkin({"bearer_token": "t"})
    assert result.ok is False and result.status is None

async def test_provider_checkin_returns_status_dict():
    """provider.checkin 把私有 CheckinStatus 降级成 dict 透传（中立层不认具体类型）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("checkin-activity-status"):
            return httpx.Response(200, json={"code": 0, "data": {
                "active": True, "today_checked_in": True, "streak_days": 3}})
        return httpx.Response(200, json={"code": 0, "data": {"credit": 100}})

    provider = CodeBuddyProvider(client=_refresh_client_provider(handler))
    result = await provider.checkin({"bearer_token": "t"})
    assert result.ok and isinstance(result.status, dict)
    assert result.status["streak_days"] == 3

async def test_credential_from_provider_helper():
    from src.provider.codebuddy.client import CodeBuddyProvider

    provider = CodeBuddyProvider()
    assert provider.credential_from({"bearer_token": "t", "nickname": "n"}).nickname == "n"
