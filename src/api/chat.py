"""OpenAI 兼容出口：POST /v1/chat/completions（流式 + 非流式）。"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Request

from ..compat.openai.request import parse_chat_request
from .deps import ApiKeyPrincipal, Services, api_key_user
from .endpoints import dispatch_compat

# dump 目录最多保留的文件数：诊断开关长期开着时不能无限侵占磁盘
DUMP_KEEP_FILES = 200


def dump_request_body(data_dir: Path, body: object) -> None:
    """把原始请求体写到 data/dumps/（诊断用），并淘汰超量的旧文件。

    请求体包含完整对话内容，因此这个开关只应用于排查客户端差异，
    不能长期开启；这里只做基本的有界保留。
    """
    dump_dir = data_dir / "dumps"
    dump_dir.mkdir(parents=True, exist_ok=True)
    try:
        (dump_dir / f"{int(time.time() * 1000)}.json").write_text(
            json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:  # 磁盘只读/满：诊断失败绝不能影响请求
        return
    stale = sorted(dump_dir.glob("*.json"))[:-DUMP_KEEP_FILES]
    for path in stale:
        with contextlib.suppress(OSError):
            path.unlink()


def create_router(services: Services) -> APIRouter:
    router = APIRouter()
    executor = services.executor
    settings = services.settings

    @router.post("/v1/chat/completions")
    async def chat_completions(request: Request,
                               principal: ApiKeyPrincipal = Depends(api_key_user)):
        return await dispatch_compat(
            executor, settings, request, principal,
            parse=parse_chat_request,
            on_body=(lambda body: dump_request_body(Path(settings.data_dir), body))
            if settings.dump_request_bodies else None)

    return router
