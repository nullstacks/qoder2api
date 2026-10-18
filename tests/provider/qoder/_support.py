"""Qoder 测试共享替身与装配助手。

`cred`(5) / `GATEWAY`(4) / `OPENAPI`(3) / `handler_for`(3) / `_campaign`(2) /
`_campaigns`(2) / `envelope`(2) / `inner`(2) / `sse`(2) 曾在该目录的多个
测试文件里各写一份逐字节等价的实现，收敛到这里。
"""

from __future__ import annotations

import json
import time

import httpx

from src.provider.qoder.client import QoderClient
from src.provider.qoder.credential import QoderCredential

__all__ = [
    "GATEWAY",
    "OPENAPI",
    "campaign",
    "campaigns",
    "cred",
    "envelope",
    "handler_for",
    "inner",
    "sse",
]

GATEWAY = "https://gateway.test"
OPENAPI = "https://openapi.test"


def cred(**overrides) -> QoderCredential:
    """构造 Qoder OAuth 凭证（默认 24h 后到期）。"""
    base = {"access_token": "dt-token-1", "uid": "u-1", "realm": "cn",
            "refresh_token": "drt-1", "auth_source": "oauth",
            "nickname": "Tester", "expires_at": int(time.time()) + 86400}
    base.update(overrides)
    return QoderCredential(**base)


def handler_for(handler, **kwargs) -> QoderClient:
    """用 `handler` 造 QoderClient（默认挂上 gateway 回退地址）。"""
    transport = httpx.MockTransport(handler)
    kwargs.setdefault("gateway_fallbacks", (GATEWAY,))
    return QoderClient(
        host=OPENAPI, gateway=GATEWAY,
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None),
        **kwargs)


def campaign(campaign_id: str = "c-1", *, action: str = "CLAIM_BENEFIT",
             status: str = "CLAIMABLE", amount: object = 100,
             kind: object = "CREDITS", end_at: object = 1_795_111_140) -> dict:
    """构造一条签到活动（campaign）条目。"""
    benefit: dict = {}
    if kind is not None:
        benefit["kind"] = kind
    if amount is not None:
        benefit["amount"] = amount
    item: dict = {"campaignId": campaign_id, "campaignKey": "act-1",
                  "actionType": action, "claimStatus": status, "benefit": benefit}
    if end_at is not None:
        item["endAt"] = end_at
    return item


def campaigns(*items: dict) -> dict:
    """把若干 campaign 条目包成上游响应体。"""
    return {"showCampaign": bool(items), "campaigns": list(items)}


def envelope(body: object, status: int = 200) -> str:
    """Qoder SSE 的单帧：`data:` 里再套一层 `{headers, body, statusCodeValue}`。"""
    payload = {"headers": {}, "body": body if isinstance(body, str) else json.dumps(body),
               "statusCodeValue": status}
    return f"data: {json.dumps(payload)}\n\n"


def inner(delta: dict, *, finish: str | None = None, usage: dict | None = None,
          choices: list | None = None) -> dict:
    """构造 OpenAI 风格 `chat.completion.chunk`（Qoder 内层 body）。"""
    payload: dict = {"id": "c1", "object": "chat.completion.chunk"}
    if choices is None:
        payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    else:
        payload["choices"] = choices
    if usage is not None:
        payload["usage"] = usage
    return payload


def sse(*payloads: object) -> str:
    """把若干内层 payload 拼成 Qoder SSE 文本并以 `[DONE]` 收尾。"""
    return "".join(envelope(p) for p in payloads) + envelope("[DONE]")
