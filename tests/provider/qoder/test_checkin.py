"""Qoder 签到（活动领取与旧接口回退）。"""

from __future__ import annotations

import time

import httpx
import pytest

from src.provider.qoder import events as qoder_events
from src.provider.qoder import status_to_dict
from src.provider.qoder.client import CHECKIN_UNAVAILABLE_STATUS, UpstreamHTTPError
from tests.provider.qoder._support import campaign as _campaign
from tests.provider.qoder._support import campaigns as _campaigns
from tests.provider.qoder._support import cred, handler_for


def _status_payload(status: str, *, last: int | None = None) -> dict:
    payload = {"status": status, "currentStreakDays": 3, "rewardCredits": 100,
               "totalRewardCredits": 400}
    if last is not None:
        payload["lastClaimedAt"] = last
    return payload



def _assert_campaign_headers(request: httpx.Request) -> None:
    assert request.headers["Cosy-ClientType"] == qoder_events.COSY_CLIENT_TYPE

async def test_checkin_campaign_claims_and_reports_credit():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        _assert_campaign_headers(request)
        paths.append(request.url.path)
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign("abc-123")))
        assert request.url.path == "/sash/api/v1/me/campaigns/abc-123/claim"
        return httpx.Response(200, json={
            "status": "CLAIMED", "replayed": False, "benefit": {"amount": 100},
            "expiresAt": "2026-10-30T00:00:00Z"})

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.already_checked_in is False
    assert result.credit == 100.0 and result.code == 0
    assert result.message == "签到成功"
    # 封账到当前签到窗口结束（= 下一轮 10:00 开放时刻），否则任务按自然日封账
    # 会把凌晨那轮「已领」当成整天完成，新窗口出现后不再自动签
    assert result.seal_until == 1_795_111_140
    # 新协议不提供连续天数：不编造 0，置 None 交前端隐藏
    assert result.status is not None and result.status.streak_days is None
    assert result.status.today_credit == 100
    assert paths == [qoder_events.EP_CAMPAIGNS,
                     "/sash/api/v1/me/campaigns/abc-123/claim"]

async def test_checkin_campaign_already_claimed_needs_no_claim():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=_campaigns(
            _campaign(status="CLAIMED")))

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.already_checked_in is True
    assert result.message == "今日已签到"
    assert result.seal_until == 1_795_111_140
    assert paths == [qoder_events.EP_CAMPAIGNS]

async def test_checkin_without_window_end_leaves_seal_to_task_default():
    """上游没给 endAt（活动改版）→ 不猜窗口，seal_until 留 None 由任务封到
    当日结束，与其它渠道同口径。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign(end_at=None)))
        return httpx.Response(200, json={"status": "CLAIMED",
                                         "benefit": {"amount": 100}})

    assert (await handler_for(handler).checkin(cred())).seal_until is None

async def test_checkin_claim_already_done_still_seals_to_window_end():
    """重放/同自然人已领：算「今日已签」并封到窗口结束，不每 10 分钟重试。"""
    def replayed(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "CLAIMED", "replayed": True,
                                         "benefit": {"amount": 100}})

    result = await handler_for(replayed).checkin(cred())
    assert result.ok is True and result.already_checked_in is True
    assert result.seal_until == 1_795_111_140

async def test_checkin_blocked_is_failure_and_not_sealed():
    """真被拒（BLOCKED 非同一自然人）→ 不封账，下轮重试。"""
    def blocked(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "BLOCKED",
                                         "failureCode": "RATE_LIMITED"})

    assert (await handler_for(blocked).checkin(cred())).seal_until is None

async def test_checkin_campaign_skips_view_details_and_picks_claimable():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(
                _campaign("detail-1", action="VIEW_DETAILS"),
                _campaign("reward-9"),
            ))
        # 必须领到 CLAIM_BENEFIT 那条，而不是展示位
        assert request.url.path == "/sash/api/v1/me/campaigns/reward-9/claim"
        return httpx.Response(200, json={
            "status": "CLAIMED", "replayed": False, "benefit": {"amount": 100}})

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.credit == 100.0

async def test_checkin_campaign_replayed_and_same_person_are_already():
    def replayed(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "CLAIMED", "replayed": True,
                                         "benefit": {"amount": 100}})

    result = await handler_for(replayed).checkin(cred())
    assert result.ok is True and result.already_checked_in is True

    def same_person(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={
            "status": "BLOCKED", "failureCode": "SAME_PERSON_ALREADY_CLAIMED"})

    blocked = await handler_for(same_person).checkin(cred())
    assert blocked.ok is True and blocked.already_checked_in is True

    def blocked_other(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "BLOCKED",
                                         "failureCode": "RATE_LIMITED"})

    rejected = await handler_for(blocked_other).checkin(cred())
    assert rejected.ok is False and rejected.already_checked_in is False
    assert "RATE_LIMITED" in rejected.message

async def test_checkin_campaign_no_claimable_and_abnormal_benefit():
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_campaigns())

    inactive = await handler_for(empty).checkin(cred())
    assert inactive.ok is True and inactive.already_checked_in is False
    assert inactive.message == "官方签到活动未开放"
    assert inactive.status is not None and inactive.status.active is False

    def claimable_but_disabled(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_campaigns(
            _campaign(status="DISABLED")))

    nothing = await handler_for(claimable_but_disabled).checkin(cred())
    assert nothing.ok is True
    assert nothing.message == "今日暂无可领取的签到奖励"

    def abnormal(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(200, json=_campaigns(_campaign()))
        return httpx.Response(200, json={"status": "CLAIMED"})   # 无 benefit

    weird = await handler_for(abnormal).checkin(cred())
    assert weird.ok is True and weird.credit is None
    assert weird.message == "签到成功（活动响应异常）"

async def test_checkin_legacy_fallback_when_campaigns_unavailable():
    """活动制接口 404 → 整体回退旧 daily-check-in 流程。"""
    now = int(time.time())
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404, content=b"not found")
        assert request.url.path == qoder_events.EP_CHECKIN_STATUS
        return httpx.Response(200, json=_status_payload("CLAIMED", last=now))

    result = await handler_for(handler).checkin(cred())
    assert result.ok is True and result.already_checked_in is True
    assert result.status is not None and result.status.streak_days == 3
    assert paths == [qoder_events.EP_CAMPAIGNS, qoder_events.EP_CHECKIN_STATUS]

async def test_checkin_legacy_fallback_claim_paths():
    def claimable(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(410)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        assert request.url.path == qoder_events.EP_CHECKIN_CLAIM
        return httpx.Response(200, json={"success": True, "rewardCredits": 120})

    result = await handler_for(claimable).checkin(cred())
    assert result.ok is True and result.credit == 120.0
    assert result.status is not None and result.status.streak_days == 3

    def conflict(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(405)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(409, content=b'{"result":"ALREADY_CLAIMED"}')

    conflicted = await handler_for(conflict).checkin(cred())
    assert conflicted.ok is True and conflicted.already_checked_in is True
    assert conflicted.code == 409

    def marker(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(200, json={"result": "ALREADY_CLAIMED"})

    assert (await handler_for(marker).checkin(cred())).already_checked_in is True

async def test_checkin_unavailable_region_is_not_an_error():
    def handler(request: httpx.Request) -> httpx.Response:
        # 活动制与旧接口都不存在：才算本区域无签到
        assert request.url.path in (qoder_events.EP_CAMPAIGNS,
                                    qoder_events.EP_CHECKIN_STATUS)
        return httpx.Response(404, content=b"not found")

    result = await handler_for(handler).checkin(cred(realm="intl"))
    assert result.ok is False and result.already_checked_in is False
    assert "本区域未开放签到接口" in result.message
    assert 404 in CHECKIN_UNAVAILABLE_STATUS

async def test_checkin_legacy_failure_paths():
    def inactive(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        return httpx.Response(200, json=_status_payload("DISABLED"))

    inactive_result = await handler_for(inactive).checkin(cred())
    assert inactive_result.ok is True
    assert inactive_result.message == "官方签到活动未开放"

    def server_error(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(server_error).checkin(cred())

    def rejected(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        if request.url.path == qoder_events.EP_CHECKIN_STATUS:
            return httpx.Response(200, json=_status_payload("CLAIMABLE"))
        return httpx.Response(200, json={"success": False, "error": "denied",
                                         "code": 7})

    failed = await handler_for(rejected).checkin(cred())
    assert failed.ok is False and failed.code == 7 and failed.message == "denied"

async def test_checkin_campaign_http_server_error_propagates():
    """活动制接口非 404 类错误不吞（不误判成「本区域无签到」）。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(handler).checkin(cred())

async def test_checkin_status_query_and_provider_wrapper():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_campaigns(_campaign(status="CLAIMED")))

    status, unavailable = await handler_for(handler).fetch_checkin_status(cred())
    assert unavailable == "" and status is not None
    assert status.today_checked_in is True and status.streak_days is None
    payload = status_to_dict(status)
    assert payload["today_checked_in"] is True
    assert payload["streak_days"] is None
    assert payload["activity_name"] == "Qoder 每日签到"

    from src.provider.qoder import QoderProvider

    provider = QoderProvider(client=handler_for(handler))
    assert (await provider.checkin_status({}))["streak_days"] is None
    await provider.aclose()

async def test_checkin_status_unavailable_returns_inactive_status():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    from src.provider.qoder import QoderProvider

    provider = QoderProvider(client=handler_for(handler))
    status = await provider.checkin_status({})
    assert status["active"] is False and status["today_checked_in"] is False
    await provider.aclose()

async def test_checkin_legacy_status_server_error_propagates():
    """活动制 404 回退后，旧状态接口的非 404 错误不吞。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == qoder_events.EP_CAMPAIGNS:
            return httpx.Response(404)
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(handler).fetch_checkin_status(cred())

def test_campaign_helpers_edges():
    # campaigns 缺失 / 非列表 → 无可领项
    assert qoder_events.checkin_campaign({}) is None
    assert qoder_events.checkin_campaign({"campaigns": "x"}) is None
    # 非 dict 项 + 无可领状态 / 缺 campaignId 的项都被跳过
    assert qoder_events.checkin_campaign({"campaigns": [
        1,
        {"actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMED"},
        {"actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
         "campaignId": ""},
    ]}) is None
    picked = qoder_events.checkin_campaign({"campaigns": [
        3,
        {"actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
         "campaignId": "x"},
    ]})
    assert picked is not None and picked["campaignId"] == "x"

    # benefit 解析：非 CREDITS 的 kind 不当积分；kind 缺失按 CREDITS
    assert qoder_events.campaign_claim_credit(
        {"benefit": {"kind": "CREDITS", "amount": 5}}) == 5.0
    assert qoder_events.campaign_claim_credit(
        {"benefit": {"kind": "RATE", "amount": 5}}) is None
    assert qoder_events.campaign_claim_credit({"benefit": {}}) is None
    assert qoder_events._campaign_amount(
        {"benefit": {"kind": "RATE", "amount": 5}}) is None

    # 状态解析：campaigns 非列表 → 未开放；CLAIMABLE 存在 → 未签
    inactive = qoder_events.checkin_status_from_campaigns({"campaigns": 1})
    assert inactive.active is False and inactive.today_checked_in is False
    mixed = qoder_events.checkin_status_from_campaigns(
        _campaigns(_campaign(status="CLAIMED"), _campaign("c-2")))
    assert mixed.today_checked_in is False and mixed.active is True

    # claim_already_done：replayed / BLOCKED 同自然人 / 其它
    assert qoder_events.claim_already_done({"replayed": True}) is True
    assert qoder_events.claim_already_done(
        {"status": "BLOCKED",
         "failureCode": "SAME_PERSON_ALREADY_CLAIMED"}) is True
    assert qoder_events.claim_already_done(
        {"status": "BLOCKED", "failureCode": "RATE_LIMITED"}) is False
    assert qoder_events.claim_already_done({"status": "CLAIMED"}) is False

def test_checkin_window_end_picks_latest_checkin_campaign():
    """窗口结束 = 下一轮开放时刻：只认签到活动，取最晚 endAt；展示位
    （VIEW_DETAILS）的 endAt 更晚也不能被采信。缺失/非数值 → None。"""
    assert qoder_events.checkin_window_end(
        _campaigns(_campaign("c-1", end_at=100), _campaign("c-2", end_at=200))) == 200
    assert qoder_events.checkin_window_end(_campaigns(
        _campaign(end_at=100),
        _campaign("detail", action="VIEW_DETAILS", end_at=999))) == 100
    assert qoder_events.checkin_window_end({"campaigns": "x"}) is None
    assert qoder_events.checkin_window_end(_campaigns()) is None
    # 非数值 / 0 / 负数的 endAt 一律当缺失（不猜窗口）
    assert qoder_events.checkin_window_end(
        _campaigns(_campaign(end_at="soon"))) is None
    assert qoder_events.checkin_window_end(_campaigns(_campaign(end_at=0))) is None
    assert qoder_events.checkin_window_end(_campaigns(1, "x")) is None

def test_claimed_today_edges():
    now = int(time.time())
    assert qoder_events._claimed_today("x", now) is False
    assert qoder_events._claimed_today(True, now) is False
    assert qoder_events._claimed_today(0, now) is False
    assert qoder_events._claimed_today(now, now) is True

async def test_fetch_checkin_status_reraises_non_unavailable_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    with pytest.raises(UpstreamHTTPError):
        await handler_for(handler).fetch_checkin_status(cred())
