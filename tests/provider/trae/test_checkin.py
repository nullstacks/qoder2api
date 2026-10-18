"""TRAE 签到（软失败、设备 id 轮换、未启用短路）。"""

from __future__ import annotations

import httpx


async def test_trae_checkin_claim_soft_failure_and_success_paths():
    """TRAE claim HTTP 200 + 业务码非 0 是软失败（9074），不得误报成功。"""
    from src.provider.trae.client import TraeClient, TraeProvider

    seen: list[str] = []
    device_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        device_ids.append(request.headers.get("x-device-id", ""))
        if request.url.path.endswith("status"):
            # 只有第二次（成功的）claim 之后才算已签
            checked = len([p for p in seen if p.endswith("claim")]) >= 2
            credits = 200 if checked else 150
            return httpx.Response(200, json={"checked_in": checked, "credits": credits,
                                             "enable": True, "code": 0})
        # 第一次 claim 软失败，之后成功
        failures = len([p for p in seen if p.endswith("claim")])
        if failures == 1:
            return httpx.Response(200, json={"code": 9074,
                                             "message": "当前参与用户太多，请稍后再试"})
        return httpx.Response(200, json={"code": 0})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    provider = TraeProvider(client=client)
    data = {"bearer_token": "t", "device_id": "d", "uid": "u"}

    # 单轮内 9074 会轮换代次设备号重试，所以一轮就应成功；期间用的都是 16 位数字
    first = await provider.checkin(data)
    assert first.ok and first.code == 0 and first.credit == 200
    # gen0: status + claim(9074)；gen1: status + claim(成功) + 回查 status
    assert len(device_ids) == 5
    assert len(set(device_ids)) == 2     # 轮换过一次设备号
    assert all(d.isdigit() and len(d) == 16 for d in device_ids)

async def test_trae_checkin_retries_9074_with_fresh_device_ids():
    """9074 按限流处理：一轮内换新设备号重试；全失败才返回软失败。

    回归背景：9074 曾被误判为「设备号格式不符」，改成确定性派生值——结果对新
    账号连续失败（同一设备号复用）。现在每次尝试都用全新设备号。
    """
    from src.provider.trae.client import TraeClient, TraeProvider

    device_ids: list[str] = []
    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        device_ids.append(request.headers.get("x-device-id", ""))
        if request.url.path.endswith("status"):
            # 只有第二次 claim 成功后才算已签、余额才涨（回查确认靠这个）
            done = len(claims) >= 2
            return httpx.Response(200, json={
                "checked_in": done, "credits": 350 if done else 150,
                "enable": True, "code": 0})
        claims.append(request.headers.get("x-device-id", ""))
        if len(claims) < 2:
            return httpx.Response(200, json={"code": 9074, "message": "当前参与用户太多"})
        return httpx.Response(200, json={"code": 0})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    result = await TraeProvider(client=client).checkin({"bearer_token": "t", "uid": "u"})
    assert result.ok is True and result.code == 0
    assert result.credit == 350
    assert len(claims) == 2
    assert claims[0] != claims[1], "重试必须换新设备号"
    assert all(d.isdigit() and len(d) == 16 for d in device_ids)
    # 每次尝试（status + claim [+ 成功后的回查]）内部必须用同一个设备号：
    # 轮次1 = status + claim（用号 A）；轮次2 = status + claim + 回查（用号 B）
    assert set(device_ids[:2]) == {claims[0]}
    assert set(device_ids[2:]) == {claims[1]}

async def test_trae_checkin_gives_up_after_all_attempts():
    """所有尝试都 9074 → 如实返回最后一次的软失败，不伪装成功、不无限重试。"""
    from src.provider.trae.client import CHECKIN_ATTEMPTS, TraeClient, TraeProvider

    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 150,
                                             "enable": True, "code": 0})
        claims.append(request.headers.get("x-device-id", ""))
        return httpx.Response(200, json={"code": 9074, "message": "当前参与用户太多"})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    result = await TraeProvider(client=client).checkin({"bearer_token": "t", "uid": "u"})
    assert result.ok is False and result.code == 9074
    assert "当前参与用户太多" in result.message
    assert len(claims) == CHECKIN_ATTEMPTS
    assert len(set(claims)) == CHECKIN_ATTEMPTS       # 每次都是新设备号

async def test_trae_checkin_not_enabled_short_circuits():
    """enable=False → 直接返回不可签到，不发 claim。"""
    from src.provider.trae.client import TraeClient

    claims: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0,
                                             "enable": False, "code": 0})
        claims.append(request.url.path)
        return httpx.Response(200, json={"code": 0})

    import httpx as _httpx
    transport = _httpx.MockTransport(handler)
    client = TraeClient(stream_client=_httpx.AsyncClient(transport=transport, timeout=None),
                        short_client=_httpx.AsyncClient(transport=transport, timeout=None))
    from src.provider.trae.client import TraeProvider

    result = await TraeProvider(client=client).checkin({"bearer_token": "t", "uid": "u"})
    assert result.ok is False and result.message == "当前账号不可签到"
    assert claims == []

def test_new_checkin_device_id_is_fresh_numeric_string():
    """签到设备号：16 位数字串，且每次都是新的（复用是 9074 的可疑诱因）。

    历史教训：此处曾断言「由 uid 确定性派生」——那是单次对照得出的错误结论
    （派生值对第二个账号连续 4 次 9074，而随机新值当次成功）。不要改回确定性派生。
    """
    from src.provider.trae.credential import new_checkin_device_id

    ids = {new_checkin_device_id() for _ in range(50)}
    assert len(ids) == 50, "设备号必须每次不同"
    assert all(i.isdigit() and len(i) == 16 for i in ids)
