"""`httpx.MockTransport` 客户端工厂与 SSE 帧构造。

上游渠道测试大量重复「用 handler 造 AsyncClient 再注入 provider 客户端」的
样板；这里收成工厂函数。各渠道 client 的构造参数差异用 `**kw` 透传。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from src.engine.sse import SSEFrame

__all__ = [
    "chunk_payload",
    "client_with_handler",
    "mock_async_client",
    "sse_frame",
    "sse_text",
    "sse_text_done",
]


def mock_async_client(handler) -> httpx.AsyncClient:
    """把 handler 包成无超时的 `AsyncClient`（流/短请求共用同一个 transport）。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=None)


def client_with_handler(factory, handler, **kw):
    """构造 `factory(stream_client=..., short_client=..., **kw)` 形式的客户端。

    `factory` 是 provider 客户端类（如 `ZenClient`）；`kw` 用于覆盖端点等参数。
    """
    client = mock_async_client(handler)
    return factory(stream_client=client, short_client=client, **kw)


def provider_client(factory, handler, **kw):
    """构造共享同一 `MockTransport` 的流/短双客户端（provider 测试通用样板）。

    与 `client_with_handler` 的差别：这里保留「两个 `AsyncClient` 共用一个
    transport」的既有形状，行为与各 provider 测试里手写的 `_client` 逐字节等价。
    """
    transport = httpx.MockTransport(handler)
    return factory(
        stream_client=httpx.AsyncClient(transport=transport, timeout=None),
        short_client=httpx.AsyncClient(transport=transport, timeout=None),
        **kw,
    )


def sse_frame(data: object, *, event: str = "") -> SSEFrame:
    """构造一个 SSE 帧；非字符串 data 自动 JSON 序列化。"""
    return SSEFrame(event=event, data=data if isinstance(data, str) else json.dumps(data))


def sse_text(*payloads: object) -> str:
    """把若干 payload 拼成 SSE 文本（每个 `data:` 一行）。"""
    return "".join(f"data: {json.dumps(p)}\n" for p in payloads)


def sse_text_done(*payloads: object) -> str:
    """同上，但每个 payload 后跟空行并以 `data: [DONE]` 收尾（OpenAI 风格）。"""
    body = "".join(f"data: {json.dumps(p)}\n\n" for p in payloads)
    return body + "data: [DONE]\n\n"


def chunk_payload(
    delta: dict[str, Any],
    *,
    finish: str | None = None,
    usage: dict[str, Any] | None = None,
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """构造 OpenAI 风格 `chat.completion.chunk`（默认单 choice、index=0）。"""
    payload: dict[str, Any] = {"id": "x", "object": "chat.completion.chunk"}
    if choices is None:
        payload["choices"] = [{"index": 0, "delta": delta, "finish_reason": finish}]
    else:
        payload["choices"] = choices
    if usage is not None:
        payload["usage"] = usage
    return payload
