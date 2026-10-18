"""OpenAI Responses 兼容出口：POST /v1/responses（Codex CLI）。

与 `/v1/chat/completions` 共用同一 `executor`（选号 / 冷却 / 轮换 / 统计 /
会话粘性全部不动），只换入站映射与出口翻译：

- 入站：`compat/responses/request.py` 把 Responses 请求体映射成内部 chat
  载荷（`ChatRequest.raw`），本服务只实现 chat 子集，不支持项显式 400。
- 出口：流式注入 `ResponsesStreamTranslator`（把中立 Event 直接翻成
  Responses SSE 帧）；非流式复用 `executor.complete` 后转换形状。

Responses 协议没有 `[DONE]` 哨兵，终止事件（`response.completed` /
`response.incomplete` / `response.failed`）本身就是流结束。
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request

from ..compat.responses.request import parse_responses_request
from ..compat.responses.response import (
    ResponsesStreamTranslator,
    completion_to_response,
)
from .deps import ApiKeyPrincipal, Services, api_key_user
from .endpoints import dispatch_compat


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor
    settings = services.settings

    @router.post("/v1/responses")
    async def responses(request: Request,
                        principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await dispatch_compat(
            executor, settings, request, principal,
            parse=parse_responses_request,
            translator_for=ResponsesStreamTranslator,
            convert=lambda completion: completion_to_response(
                completion, created_at=int(time.time())))

    return router
