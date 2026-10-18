"""Host 白名单与安全响应头（PROPOSAL §6）。

拆出来是为了让 build_app 只负责装配；这两个策略本身与业务无关，
改动理由也不同（安全策略调整 vs 服务组装）。
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..compat.openai.errors import error_payload
from ..config import Settings


def _hostname(host_header: str) -> str:
    """从 Host/Origin 头取出主机名（小写，剥端口与 IPv6 方括号）。

    IPv6 字面量形如 `[::1]:8000`：先按 `]` 切开取括号内，否则 `split(":")`
    会在第一个冒号处截断成空串。`csrf._same_host` 用同一套规则（见
    `test_security.py` 的 IPv6 用例）。
    """
    text = host_header.strip()
    if text.startswith("["):
        name, _, _ = text.partition("]")
        return name.lstrip("[").strip().lower()
    return text.split(":", 1)[0].strip().lower()


def _public_base_host(settings: Settings) -> str:
    """PUBLIC_BASE_URL 的主机名（剥 scheme / path / 端口），解析不出返回空串。"""
    base = settings.public_base_url
    if base.startswith(("http://", "https://")):
        base = base.split("://", 1)[1]
    return _hostname(base.split("/", 1)[0])


def host_allowed(host_header: str, settings: Settings) -> bool:
    """Host 白名单（防 DNS rebinding）。

    ALLOWED_HOSTS 配置优先（逗号分隔）；未配置时放行本地回环与 PUBLIC_BASE_URL
    的主机。`testserver`（FastAPI TestClient 默认 Host）只在显式开启
    `ALLOW_TEST_HOST` 时放行——单标签主机名可能被本地 DNS 搜索域解析到本服务，
    生产默认放行等于给 DNS rebinding 留免校验入口。
    只比较主机名，忽略端口。
    """
    hostname = _hostname(host_header)
    if settings.allowed_hosts:
        allowed = {_hostname(h) for h in settings.allowed_hosts.split(",") if h.strip()}
        return hostname in allowed
    allowed = {"localhost", "127.0.0.1", "::1"}
    if settings.allow_test_host:
        allowed.add("testserver")
    path_host = _public_base_host(settings)
    if path_host:
        allowed.add(path_host)
    return hostname in allowed


def session_cookie_secure(request: Request, settings: Settings) -> bool:
    """会话 Cookie 是否加 `Secure`：按**请求的实际协议**判定。

    不能只看 PUBLIC_BASE_URL：同一实例可能同时有两条入口——LAN 直连
    （http://192.168.124.4:8000）与 Cloudflare Tunnel
    （https://ai-api.robbs.work）。`Secure` Cookie 会被浏览器在 http 上丢弃，
    所以「一律 Secure」会让 LAN 登录失败，「一律不 Secure」又让公网 https
    会话失去保护。

    应用自身不做 TLS 终止（uvicorn 跑明文 http），socket 层拿不到真实协议，
    只能靠 `X-Forwarded-Proto`：

    - 有该头 → 采信它的第一个条目（反代 / 隧道终止 TLS 后唯一的协议信号）。
      **故意不依赖 TRUST_PROXY**：伪造它只会让伪造者自己的浏览器丢掉 Cookie
      （判定 https 只会让 Cookie 更严），不构成对他人的降级；而耦合
      TRUST_PROXY 会与本机「LAN 直连必须关 TRUST_PROXY（否则 XFF 可伪造
      绕过 allowed_ips）」冲突，导致隧道请求永远拿不到 Secure。
    - 无该头 → 仅当请求 Host 正是 PUBLIC_BASE_URL 的主机、且后者是 https 时
      按 https 处理（覆盖「https 反代未转发 XFP」的部署）；直连其它地址
      （如 LAN IP）一律视为明文。
    """
    proto = request.headers.get("x-forwarded-proto", "")
    first = proto.split(",", 1)[0].strip().lower()
    if first in ("http", "https"):
        return first == "https"
    if not settings.public_base_url.startswith("https://"):
        return False
    base_host = _public_base_host(settings)
    return bool(base_host) and _hostname(request.headers.get("host", "")) == base_host


def content_security_policy(settings: Settings) -> str:
    """CSP：默认只允许同源资源，顺带堵住 object/base-uri/form-action（L8）。

    `script-src 'self'` 之所以能收这么紧：主题初始化已从内联脚本挪到
    `web/public/theme-init.js`（同步脚本，渲染阻塞、不会闪烁），页面里再无内联
    JS。`style-src` 保留 `'unsafe-inline'`——React 的 `style={}` 会写行内 style
    属性，去掉会直接打断渲染。

    `enable_docs` 打开时 Swagger UI 从 jsdelivr 拉资源，必须放行该 CDN，
    否则 /docs 白屏（默认关闭，不影响生产策略）。
    """
    script_src = "'self'"
    style_src = "'self' 'unsafe-inline'"
    if settings.enable_docs:
        script_src += " https://cdn.jsdelivr.net"
        style_src += " https://cdn.jsdelivr.net"
    return (
        "default-src 'self'; "
        f"script-src {script_src}; "
        f"style-src {style_src}; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; object-src 'none'; base-uri 'self'; "
        "form-action 'self'")


async def security_middleware(request: Request, call_next):
    """Host 校验 + 安全响应头。请求体上限由 BodySizeLimitMiddleware 处理。

    注册方式（app.middleware("http") 装饰器）留在 build_app，因为那是
    FastAPI 的接线细节；返回什么由这里决定。
    """
    host_header = request.headers.get("host", "")
    if not host_allowed(host_header, request.app.state.settings):
        return JSONResponse(status_code=400,
                            content=error_payload("invalid host header",
                                                  "invalid_request", 400))
    # 纯读 content-length 会被 chunked 请求绕过，因此上限在 ASGI 层做
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault(
        "Content-Security-Policy",
        content_security_policy(request.app.state.settings))
    # 只跨源剥离 Referer，同源保留：CSRF 校验依赖同源 Referer（`same-origin`
    # 正好两者兼顾——`no-referrer` 会让同源写请求丢掉 Referer 而被误判跨源）。
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.app.state.settings.public_base_url.startswith("https://"):
        # 仅 https 部署下发 HSTS：明文部署发了无意义，还会预锁本地 http 访问
        response.headers.setdefault("Strict-Transport-Security",
                                    "max-age=31536000; includeSubDomains")
    return response
