"""CodeBuddy 成长中心 HTTP 客户端：端点读写、池生命周期与失败映射。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.codebuddy.client import CodeBuddyCredential
from src.provider.codebuddy.events import UpstreamProtocolViolation
from src.provider.codebuddy.growth import (
    EP_BUDDY_OPEN,
    EP_BUDDY_QUOTA,
    EP_ENERGY,
    EP_LOTTERY_CHANCES,
    EP_LOTTERY_DRAW,
    EP_MAKEUP_USE,
    EP_REDEEM,
    EP_REDEEM_SUMMARY,
    EP_STREAK,
    EP_TASK_ACCEPT,
    EP_TASKS,
    EP_TRAVEL_CLAIM,
    EP_TRAVEL_CONFIG,
    EP_TRAVEL_DEPART,
    EP_TRAVEL_STATUS,
    CodeBuddyGrowth,
    GrowthRejected,
)
from tests.provider.codebuddy._growth_support import (
    REDEEM_LOCKED,
    STREAK_BODY,
    TASKS_BODY,
    TRAVEL_ARRIVED,
)
from tests.provider.codebuddy._growth_support import client as _client


async def test_growth_client_reads_every_endpoint():
    """七个只读端点的路径与解析（路径写错会在这里红）。"""
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return {
            EP_TRAVEL_STATUS: httpx.Response(200, json=TRAVEL_ARRIVED),
            EP_TRAVEL_CONFIG: httpx.Response(200, json={"code": 0, "data": {"locations": [
                {"id": 1, "name": "咖啡馆"}]}}),
            EP_TASKS: httpx.Response(200, json=TASKS_BODY),
            EP_STREAK: httpx.Response(200, json=STREAK_BODY),
            EP_REDEEM_SUMMARY: httpx.Response(200, json=REDEEM_LOCKED),
            EP_LOTTERY_CHANCES: httpx.Response(200, json={"code": 0, "data": {"balance": 0}}),
            EP_BUDDY_QUOTA: httpx.Response(200, json={"code": 0, "data": {
                "affordable": 1, "balance": 18, "cost_per_open": 10, "max_open_count": 5}}),
            EP_ENERGY: httpx.Response(200, json={"code": 0, "data": {"balance": 18}}),
        }[request.url.path]

    client = CodeBuddyGrowth("https://e", client=_client(handler))
    credential = CodeBuddyCredential(bearer_token="t")
    assert (await client.travel_status(credential)).state == "arrived"
    assert (await client.travel_locations(credential))[0].name == "咖啡馆"
    assert len(await client.tasks(credential)) == 6
    assert (await client.streak(credential)).days == 0
    assert (await client.redeem_summary(credential))["starter"] == "locked"
    assert await client.lottery_chances(credential) == 0
    assert await client.buddy_quota(credential) == (1, 10, 5)
    assert await client.energy(credential) == 18
    assert seen == [EP_TRAVEL_STATUS, EP_TRAVEL_CONFIG, EP_TASKS, EP_STREAK,
                    EP_REDEEM_SUMMARY, EP_LOTTERY_CHANCES, EP_BUDDY_QUOTA, EP_ENERGY]
    await client.aclose()

async def test_growth_client_lazy_pool_and_close():
    client = CodeBuddyGrowth("https://e")
    assert client._http is client._http
    await client.aclose()

async def test_growth_client_writes_send_expected_payloads():
    """写操作的 payload 形状（client_token 缺失上游直接 400）。"""
    payloads: list[tuple[str, dict]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        payloads.append((request.url.path, _json.loads(request.content or b"")))
        return httpx.Response(200, json={"code": 0, "data": {
            "credit": 10, "energy": 5, "makeup_cards": {"balance": 1},
            "prize_name": "冰箱贴", "buddy": "小猫", "location": {"name": "书店"},
            "duration_hours": 2}})

    client = CodeBuddyGrowth("https://e", client=_client(handler))
    credential = CodeBuddyCredential(bearer_token="t")
    assert await client.claim_travel(credential, 277990) == (10.0, 5.0)
    await client.depart(credential, 1)
    # 接单是复数数组（2026-09 契约）：单数形式在新服务端一律 400
    assert await client.accept_tasks(credential, ["t1", "t2"]) == [
        {"task_code": "t1", "status": "ok"}, {"task_code": "t2", "status": "ok"}]
    claimed = await client.claim_task(credential, "t3")
    assert claimed["credit"] == 10 and claimed["prize_name"] == "冰箱贴"
    assert await client.use_makeup_card(credential, "2026-09-01") == 1
    assert await client.redeem(credential, "7d") == (10.0, 5.0)
    await client.draw_lottery(credential)
    await client.open_buddy(credential, 2)
    paths = [path for path, _payload in payloads]
    assert paths == [EP_TRAVEL_CLAIM, EP_TRAVEL_DEPART, EP_TASK_ACCEPT,
                     f"{EP_TASKS}/t3/claim", EP_MAKEUP_USE,
                     EP_REDEEM, EP_LOTTERY_DRAW, EP_BUDDY_OPEN]
    by_path = dict(payloads)
    assert by_path[EP_TRAVEL_CLAIM] == {"record_id": 277990}
    assert by_path[EP_TRAVEL_DEPART] == {"location_id": 1}
    assert by_path[EP_TASK_ACCEPT] == {"task_codes": ["t1", "t2"]}   # 复数数组
    assert by_path[f"{EP_TASKS}/t3/claim"] == {}                      # 领奖 body 空
    assert by_path[EP_MAKEUP_USE]["target_date"] == "2026-09-01"
    assert by_path[EP_MAKEUP_USE]["client_token"].startswith("u-")
    assert by_path[EP_REDEEM]["tier"] == "7d"       # 档位标识，不是天数
    assert by_path[EP_BUDDY_OPEN]["count"] == 2
    await client.aclose()

async def test_growth_client_use_makeup_card_tolerates_shapes():
    """补登回包形状未知（该写路径实测未验证过）：对象/裸值/缺失都不能炸。"""
    # 缺失字段 → None（「上游没说」与「卡用完了」是两回事）
    for body, expected in (({"makeup_cards": {"balance": 3}}, 3),
                           ({"makeup_cards": 2}, 2),
                           ({"makeup_cards": 0}, 0),
                           ({}, None)):
        async def handler(_request: httpx.Request, _body=body) -> httpx.Response:
            return httpx.Response(200, json={"code": 0, "data": _body})

        client = CodeBuddyGrowth("https://e", client=_client(handler))
        got = await client.use_makeup_card(CodeBuddyCredential(bearer_token="t"), "d")
        assert got == expected
        await client.aclose()

async def test_growth_client_maps_failures_to_rejections():
    """401/403 与 5xx 都抛 GrowthRejected 并带上状态码与上游消息。"""
    async def unauthorized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401, "msg": "登录过期"})

    client = CodeBuddyGrowth("https://e", client=_client(unauthorized))
    with pytest.raises(GrowthRejected) as caught:
        await client.travel_status(CodeBuddyCredential(bearer_token="t"))
    assert caught.value.status == 401 and caught.value.message == "登录过期"
    await client.aclose()

    async def server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"boom")

    client2 = CodeBuddyGrowth("https://e", client=_client(server_error))
    with pytest.raises(GrowthRejected) as caught2:
        await client2.tasks(CodeBuddyCredential(bearer_token="t"))
    assert caught2.value.status == 503 and caught2.value.message == ""
    assert caught2.value.needs_attention is True
    await client2.aclose()

async def test_growth_client_rejects_bad_envelopes_and_non_json():
    """HTTP 200 但业务码非 0 / 缺 data / 非 JSON：必须显式报错，不能当空结果。"""
    cases = [
        httpx.Response(200, json={"code": 5, "msg": "boom"}),
        httpx.Response(200, json={"code": 0}),
        httpx.Response(200, json=[1]),
        httpx.Response(200, content=b"<html>"),
    ]
    for response in cases:
        async def handler(_request: httpx.Request, _response=response) -> httpx.Response:
            return _response

        client = CodeBuddyGrowth("https://e", client=_client(handler))
        with pytest.raises(UpstreamProtocolViolation):
            await client.energy(CodeBuddyCredential(bearer_token="t"))
        await client.aclose()

async def test_growth_client_401_without_message_uses_generic_text():
    """401 但响应体没有 msg → 回落通用说明，方便排障一眼看出是登录问题。"""
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"code": 401})

    client = CodeBuddyGrowth("https://e", client=_client(handler))
    with pytest.raises(GrowthRejected) as caught:
        await client.energy(CodeBuddyCredential(bearer_token="t"))
    assert caught.value.message == "credential rejected"
    await client.aclose()

async def test_growth_client_close_is_noop_without_pool():
    """没建立连接池时 aclose 是 no-op（不能因为没事干就报错）。"""
    client = CodeBuddyGrowth("https://e")
    await client.aclose()
    await client.aclose()
