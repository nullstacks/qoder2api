"""自助用量端点：`GET /v1/usage`（Key 归属人查自己的用量与配额余量）。

只读**本地明细**（`usage_events` 按 `api_key_id` 聚合），不触发任何上游调用，
也不返回池级信息——凭证 id / 昵称 / 其他用户的数据一律不出现，避免把共享
凭证池的内部结构经一把 Key 泄露给终端用户。

窗口只有 1 / 7 / 30 天三档（`?days=`）：档位固定而非任意值，是因为响应里的
`window` 字段要能被客户端稳定解析，且「任意天数」的语义（自然日还是 24h×N）
每次都要重新解释。`days=1` = 当日（北京时间自然日，与配额/限额同一口径），
7 / 30 = 含今天在内向前 7 / 30 个自然日。

`remaining` 只对**当日类**配额给出（速率类没有「剩余」的稳定含义）；值为
`None` 表示该项未配置，`0` 表示已用满。`resets_at` 是下一个北京时间 0 点。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Query

from ..compat.openai.request import InvalidRequest
from ..engine.limits import day_start
from ..key_quota import KeyQuota, KeyQuotaGuard, KeyUsage
from .deps import ApiKeyPrincipal, Services, api_key_user

# 允许的窗口档位：值同时是「天数」与响应里的 window 标签来源。
ALLOWED_DAYS = (1, 7, 30)
WINDOW_LABELS = {1: "today", 7: "last_7d", 30: "last_30d"}


def _since(days: int, now: int) -> int:
    """窗口起点：含今天在内向前 `days` 个北京时间自然日的 0 点。"""
    return day_start(now) - (days - 1) * 86400


def _remaining(quota: KeyQuota, usage: KeyUsage) -> dict[str, float | None]:
    """当日类配额的剩余量；未配置的项为 None（前端显示「不限制」）。"""
    return {
        "requests": None if not quota.daily_requests
        else max(0, quota.daily_requests - usage.requests),
        "tokens": None if not quota.daily_tokens
        else max(0, quota.daily_tokens - usage.tokens),
        "cost_usd": None if not quota.daily_cost_usd
        else max(0.0, quota.daily_cost_usd - usage.cost_usd),
    }


def usage_payload(services: Services, principal: ApiKeyPrincipal, *,
                  days: int, now: int | None = None) -> dict:
    """组装响应体（纯函数：`now` 可注入，测试不依赖真实时钟）。"""
    moment = int(now if now is not None else time.time())
    today = day_start(moment)
    usage = services.key_usage.for_key(principal.key_id, since=_since(days, moment))
    today_usage = (usage if days == 1
                   else services.key_usage.for_key(principal.key_id, since=today))
    quota = principal.quota
    guard: KeyQuotaGuard = services.key_quota
    return {
        "object": "usage",
        "window": WINDOW_LABELS[days],
        "requests": usage.requests,
        "ok": usage.ok,
        "failed": usage.failed,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": round(usage.cost_usd, 6),
        "limits": quota.as_dict(),
        # 剩余量按「当日」算（即使窗口是 7/30 天）：配额本身就是当日口径，
        # 拿 7 天用量去减当日上限会得出永远为 0 的假余量。
        "remaining": _remaining(quota, today_usage),
        # 速率类配额的当前占用（进程内计数）：让用户能区分「等窗口」与「等配额重置」
        "rate": {
            "requests_last_minute": guard.window_usage(principal.key_id)[0],
            "tokens_last_minute": guard.window_usage(principal.key_id)[1],
            "inflight": guard.inflight(principal.key_id),
        },
        "resets_at": today + 86400,
    }


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/usage")
    async def usage(days: str = Query("1"),
                    principal: ApiKeyPrincipal = Depends(api_key_user)):
        # 越界/非数字一律 400（与本服务其它入站校验同一口径），不静默夹取——
        # 静默改窗口会让客户端以为自己在看 30 天数据，实际只有 1 天。
        # 参数按字符串收：`?days=abc` 交给这里回 400，而不是框架的 422。
        try:
            window = int(days)
        except ValueError as error:
            raise InvalidRequest(
                f"days must be one of {', '.join(str(d) for d in ALLOWED_DAYS)}") from error
        if window not in ALLOWED_DAYS:
            raise InvalidRequest(
                f"days must be one of {', '.join(str(d) for d in ALLOWED_DAYS)}")
        return usage_payload(services, principal, days=window)

    return router
