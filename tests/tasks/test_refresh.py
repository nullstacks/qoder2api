"""凭证刷新：token 轮换、账号切换与失败标记。"""

from __future__ import annotations

import time

import httpx
import pytest

from src.provider.base import Quota, UpstreamReloginRequired
from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.refresh import Account, CodeBuddyRefresh
from tests.helpers.constants import SWITCH_PATH
from tests.helpers.http import mock_async_client as _refresh_client


async def test_refresh_updates_token_and_reports_rotation():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": [
                {"accountId": "a2", "nickname": "second", "pluginEnabled": True}]}})
        return httpx.Response(200, json={"code": 0, "data": {
            "accessToken": "NEW", "refreshToken": "RT2", "expiresIn": 60}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT1", auth_source="oauth", user_id="u"))
    assert outcome.credential.bearer_token == "NEW"
    assert outcome.credential.refresh_token == "RT2"
    assert outcome.refresh_token_rotated is True
    assert outcome.accounts is not None and outcome.accounts[0].account_id == "a2"
    assert outcome.credential.auth_source == "oauth"      # 来源必须继承
    await client.aclose()

async def test_refresh_keeps_old_token_when_response_has_none():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": 5}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    original = CodeBuddyCredential(bearer_token="old", refresh_token="RT",
                                   auth_source="oauth")
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(original)
    assert original.bearer_token == "old" and original.refresh_token == "RT"
    await client.aclose()

async def test_refresh_requires_refresh_token():
    client = CodeBuddyRefresh("https://e", client=_refresh_client(
        lambda _r: httpx.Response(200, json={"code": 0})))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="x", auth_source="oauth"))
    await client.aclose()

async def test_refresh_marks_accounts_pending_on_failure():
    """账号同步失败 → accounts_pending（可恢复），但 token 已刷新。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(500, content=b"boom")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.accounts_pending is True and outcome.accounts is None
    await client.aclose()

@pytest.mark.parametrize("account_response", [
    httpx.Response(200, content=b"<html>"),
    httpx.Response(200, json={"code": 1}),
    httpx.Response(200, json={"code": 0, "data": {"accounts": "no"}}),
    httpx.Response(200, json={"code": 0, "data": {}}),
])
async def test_accounts_unavailable_shapes(account_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return account_response
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "N"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.list_accounts(CodeBuddyCredential(bearer_token="t"))
    await client.aclose()

async def test_refresh_survives_accounts_transport_error():
    """账号阶段传输失败 → pending（可恢复），token 刷新本身成功。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.credential.bearer_token == "NEW"
    assert outcome.accounts_pending is True
    await client.aclose()

async def test_refresh_transport_error_on_token_stage_raises():
    """token 阶段传输失败必须显式失败，让调用方重试（不能静默当成功）。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns")

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(
            bearer_token="old", refresh_token="RT", auth_source="oauth"))
    await client.aclose()

async def test_switch_account_clears_enterprise_context_for_personal():
    """切到个人账号必须清空企业上下文（AGENTS.md：禁止回退旧 enterprise_id）。"""
    seen: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            seen.append((request.url.path, dict(request.headers)))
            return httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "SWITCHED", "refreshToken": "RT2"}})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "personal1", "nickname": "me", "type": "personal",
             "pluginEnabled": True},
            {"accountId": "ent1", "nickname": "corp", "type": "enterprise",
             "enterpriseId": "ent-9", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    base = CodeBuddyCredential(bearer_token="t", user_id="u", enterprise_id="old-ent",
                               department_full_name="技术部", auth_source="oauth",
                               refresh_token="RT")
    personal = await client.switch_account(base, "personal1")
    assert personal.enterprise_id == "" and personal.department_full_name == ""
    assert personal.account_uid == "personal1"
    assert personal.bearer_token == "SWITCHED"          # 切换会换 token
    assert seen[-1][0] == SWITCH_PATH                  # 个人账号走无后缀路径

    corporate = await client.switch_account(base, "ent1")
    assert corporate.enterprise_id == "ent-9" and corporate.department_full_name == "技术部"
    assert seen[-1][0] == f"{SWITCH_PATH}/ent-9"       # 企业账号带 enterprise_id
    assert "x-enterprise-id" in {k.lower() for k in seen[-1][1]}
    assert "x-tenant-id" in {k.lower() for k in seen[-1][1]}
    await client.aclose()

async def test_switch_account_rejects_disabled_target_account():
    """切换后目标账号不在启用列表 → account_missing。"""
    calls = {"n": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {"accessToken": "N"}})
        calls["n"] += 1
        accounts = [{"accountId": "a1", "type": "personal", "pluginEnabled": True}]
        if calls["n"] > 1:                              # 切换后的复查：账号被禁用
            accounts[0]["pluginEnabled"] = False
        return httpx.Response(200, json={"code": 0, "data": {"accounts": accounts}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "a1")
    await client.aclose()

async def test_switch_account_rejects_enterprise_without_id():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "e1", "type": "enterprise", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "e1")
    await client.aclose()

@pytest.mark.parametrize("account_id", ["", "ghost"])
async def test_switch_account_rejects_missing(account_id):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/accounts/switch"):
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1"}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(CodeBuddyCredential(bearer_token="t"), account_id)
    await client.aclose()

@pytest.mark.parametrize("switch_response", [
    httpx.Response(409, content=b"conflict"),
    httpx.Response(200, content=b"<html>"),
    httpx.Response(200, json={"code": 7}),
    httpx.Response(200, json={"code": 10081}),
])
async def test_switch_account_rejects_upstream_failure(switch_response):
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            return switch_response
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(CodeBuddyCredential(bearer_token="t",
                                                        refresh_token="RT"), "a1")
    await client.aclose()

async def test_refresh_lazy_client_and_close():
    client = CodeBuddyRefresh("https://e")
    assert client._http is client._http
    await client.aclose()

def test_to_account_skips_objects_without_id():
    from src.provider.codebuddy.refresh import _to_account

    assert _to_account("junk") is None
    assert _to_account({}) is None
    assert _to_account({"uid": "u", "pluginEnabled": True}) == Account(
        account_id="u", enabled=True)
    assert _to_account({"id": "i", "name": "n", "type": "personal"}).nickname == "n"

async def test_refresh_rejects_unauthorized_and_server_error():
    # 401/403 = 续期凭据被拒，终态：程序侧换不回新 token，只能重新登录
    for status in (401, 403):
        async def handler(_request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, content=b"x")

        client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
        with pytest.raises(UpstreamReloginRequired):
            await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                     auth_source="oauth"))
        await client.aclose()
    # 5xx 是可重试的失败：必须留在 UpstreamProtocolViolation，判成终态会让一次
    # 偶发故障永久废掉一条好凭证
    for status in (500, 503):
        async def handler(_request: httpx.Request, status=status) -> httpx.Response:
            return httpx.Response(status, content=b"x")

        client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
        with pytest.raises(UpstreamProtocolViolation):
            await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                     auth_source="oauth"))
        await client.aclose()

async def test_refresh_rejects_non_json_body():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>")

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                auth_source="oauth"))
    await client.aclose()

async def test_refresh_rejects_business_error_code():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 40001, "msg": "nope"})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.refresh(CodeBuddyCredential(bearer_token="t", refresh_token="RT",
                                                auth_source="oauth"))
    await client.aclose()

async def test_refresh_keeps_old_refresh_token_when_not_rotated():
    """上游没轮换 refresh token 时必须沿用旧值，不能清空。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            return httpx.Response(200, json={"code": 0, "data": {"accounts": []}})
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.credential.refresh_token == "RT"
    assert outcome.refresh_token_rotated is False
    await client.aclose()

async def test_refresh_survives_generic_exception_in_accounts_stage():
    """账号阶段抛非 httpx 异常也要记为 pending（refresh 125-130）。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v2/plugin/accounts"):
            raise RuntimeError("weird transport")
        return httpx.Response(200, json={"code": 0, "data": {"accessToken": "NEW"}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    outcome = await client.refresh(CodeBuddyCredential(
        bearer_token="old", refresh_token="RT", auth_source="oauth"))
    assert outcome.accounts_pending is True
    await client.aclose()

async def test_switch_account_transport_error_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith(SWITCH_PATH):
            raise httpx.ConnectError("dns")
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"accountId": "a1", "type": "personal", "pluginEnabled": True}]}})

    client = CodeBuddyRefresh("https://e", client=_refresh_client(handler))
    with pytest.raises(UpstreamProtocolViolation):
        await client.switch_account(
            CodeBuddyCredential(bearer_token="t", refresh_token="RT"), "a1")
    await client.aclose()

def test_switch_account_triggers_immediate_probe(admin_client):
    """账号切换后额度对应新账号，必须重探测而不是沿用旧值。"""
    app, client = admin_client
    credential_id = app.state.credentials.add(
        provider="codebuddy", credential_data={"bearer_token": "t"})
    probed: list[int] = []

    class Stub:
        endpoint = "https://e"

        async def switch_account(self, data, account_id):
            return {**data, "account_uid": account_id}

        async def probe_quota(self, _data):
            probed.append(1)
            return Quota(remaining=1, total=4, probed_at=1)

    app.state.executor._deps.providers["codebuddy"] = Stub()
    client.post(f"/api/credentials/{credential_id}/accounts/select",
                json={"account_id": "a1"})

    for _ in range(50):
        if probed:
            break
        time.sleep(0.02)
    assert probed == [1]
