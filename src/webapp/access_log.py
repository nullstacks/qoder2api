"""请求级结构化日志（logfmt，纯 ASGI 中间件，PROPOSAL §6）。

**为什么还要加这一层**：uvicorn 自带的 access log 只有一行裸文本
`INFO: 127.0.0.1:1234 - "POST /v1/chat/completions?foo=bar HTTP/1.1" 200 OK`——
它带完整 query string（可能含客户端塞进去的 token）、没有请求 ID、没有耗时，
也没法和 `usage_events.request_id` 对上。排障时「这条慢请求是谁、走的哪条
路由、跟哪条统计行对应」只能靠时间戳猜。

这里每完成一个**业务请求**打一行 logfmt：

    event=request request_id=<id> method=POST route=/v1/chat/completions status=200 duration_ms=1234

纪律（与 PROPOSAL §6「日志脱敏」一致）：

- **不写 query string**：只取 ASGI `scope["path"]`，从不读 `query_string`。
- **不写请求体 / 响应体 / 请求头**：本中间件只旁路观察 `http.response.start`
  的状态码，不碰 body。
- **不写用户身份**：用户名不进日志，靠 `request_id` 关联 `usage_events`
  （那里已有 `username` 列）——日志系统通常保留更久、权限更宽，能少放就少放。
- **路由用模板而不是实际路径**：`/api/credentials/{credential_id}` 而不是
  `/api/credentials/cred_abc123`，既避免把资源 ID 写进日志，也避免日志按
  ID 无限分叉。未匹配到路由（404 / 被安全中间件拒掉）时回落为清洗过的
  原始路径：只保留 `[A-Za-z0-9._~/%:@-]`、截断 120 字符，防换行注入。

**只记业务请求**：`/api/*`、`/v1/*`、`/authorize`。静态资源（每次打开管理台
都会拉一堆 `assets/*.js`）与探针（`/health`、`/healthz`、`/metrics`）不记——
它们由 uvicorn access log 覆盖，重复记录只会把业务行挤出视野。

注册顺序：必须在 `RequestIdMiddleware` **内侧**（后者的 ID 要先生成）、在
`BodySizeLimitMiddleware` **外侧**（413 也是请求结果，要能记到）。

关闭方式：热更项 `REQUEST_LOG_ENABLED=false`（见 `runtime_settings`）。
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from typing import Any

from ..request_context import get_request_id

logger = logging.getLogger("src.access")

# 记录的前缀（ASGI path 不带前导斜杠，见 `_should_log`）。
_LOGGED_PREFIXES = ("api/", "v1/")
# 精确匹配的额外路径：`/authorize` 是 TRAE 浏览器回调落点，不在 api/v1 前缀里。
_LOGGED_EXACT = frozenset({"api", "v1", "authorize"})
# SPA catch-all 的路由模板：命中它说明「没有具体路由」，要回落原始路径。
_SPA_TEMPLATE = "/{path:path}"
# 原始路径（未匹配路由时）的清洗白名单与长度上限：防换行/控制字符注入。
_UNSAFE_PATH = re.compile(r"[^A-Za-z0-9._~/%:@\-]")
MAX_PATH_LENGTH = 120


def _should_log(path: str) -> bool:
    """是否记录该请求（只记业务请求，见模块 docstring）。"""
    normalized = path.lstrip("/")
    return normalized in _LOGGED_EXACT or normalized.startswith(_LOGGED_PREFIXES)


def _route_label(scope: dict[str, Any]) -> str:
    """路由模板；未匹配到具体路由时回落为清洗后的原始路径。"""
    route = scope.get("route")
    template = getattr(route, "path", None)
    if isinstance(template, str) and template and template != _SPA_TEMPLATE:
        return template
    raw = str(scope.get("path") or "/")
    cleaned = _UNSAFE_PATH.sub("?", raw)[:MAX_PATH_LENGTH]
    return cleaned or "/"


class RequestLogMiddleware:
    """每个业务请求一行 logfmt：request_id / method / route / status / duration_ms。"""

    def __init__(self, app, *, enabled: Callable[[], bool] | None = None) -> None:
        # `enabled` 是取值器而不是快照：热更开关改完下一请求立即生效。
        self.app = app
        self._enabled = enabled or (lambda: True)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if not _should_log(str(scope.get("path") or "")) or not self._enabled():
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 0

        async def send_with_status(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_status)
        finally:
            # 异常向上冒泡时由服务器回 500：日志照记，否则「挂掉的请求」恰好
            # 是唯一没有日志的请求。
            _emit(scope, status or 500, time.perf_counter() - started)


def _emit(scope: dict[str, Any], status: int, elapsed: float) -> None:
    """写一行结构化日志（logfmt）。"""
    message = ("event=request request_id={} method={} route={} status={} duration_ms={}"
               .format(get_request_id() or "-",
                       str(scope.get("method") or "-"),
                       _route_label(scope),
                       status,
                       int(elapsed * 1000)))
    # 5xx 是服务端自己的问题，值得按 WARNING 抓；4xx 多为客户端用法问题，
    # 降噪留在 INFO（否则 401 探测、404 拼错路径会把告警日志淹没）。
    if status >= 500:
        logger.warning(message)
    else:
        logger.info(message)
