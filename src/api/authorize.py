"""TRAE 回调落点（无鉴权，浏览器 302 不带 key）。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..auth.session import SESSION_COOKIE, verify_session_token
from ..compat.openai.errors import error_payload
from ..provider.trae.callback import login_trace_id
from ..provider.trae.events import UpstreamProtocolViolation
from .deps import Services

# 待完成回调的有效期：/authorize 无鉴权（浏览器 302 不带 key），长期挂着
# 的 pending 会被同网络的任何人都可用自己的 refreshToken 完成兑换——
# 换句话说，谁都能把自己的凭证塞进池子（落库归属还是发起登录的管理员）。
# 10 分钟足够走完一次浏览器登录。
PENDING_CALLBACK_TTL_SECONDS = 600


def create_router(services: Services) -> APIRouter:
    router = APIRouter()

    @router.get("/authorize")
    async def authorize(request: Request):
        """TRAE 浏览器 302 落点。

        回调不需要 API Key（浏览器不会带），因此这里不做鉴权，但：
        - 只接受**发起人本人**的回调
        - 待完成登录有 TTL，过期即作废并清槽
        - 只接受带 refreshToken 的链接，其他一律拒绝
        - 换到的凭证直接落库，响应里绝不回传 token

        注意：TRAE 回跳时只带 refreshToken/userInfo/userJwt，**不会回传
        登录 URL 里的 state**——machine/device id 从待完成登录的 state 里取，
        这正是 start_auth 把它们编码进 state 的原因（保证登录与落盘凭证一致）。

        因此不能用 state 匹配回调。绑定方式两条并用：
        - **loginTraceID**（主）：TRAE 原样回显 `login_trace_id`，值是
          `(machine_id + device_id)[-16:]`，只有发起登录那次拿得到；
        - **发起人会话**（辅）：回调是顶层浏览器导航，SameSite=Lax 会带上
          管理台的会话 Cookie，据会话用户名找到其待完成登录。

        这样：
        - 两个管理员并发发起登录不会互相覆盖（各按用户名分键）；
        - 同网段的其他人无法用自己的 refreshToken 顶掉别人进行中的登录
          （缺 loginTraceID 或不符即拒）；
        - 凭证记在发起人名下，不会被记错。
        """
        # 唯一无鉴权的写端点。**不用 Origin/Referer 启发式**：合法回调本就
        # 由上游门户（www.trae.cn）302 跳来，是跨源的；https 部署下浏览器
        # 还会因协议降级/Referrer-Policy 丢掉 Referer——按 Referer 判同源
        # 既拦不住真攻击（攻击者加 no-referrer 即绕过），又可能误杀真回调。
        # 改用回调自带的 `loginTraceID` 绑定：它是 start_auth 生成的
        # `(machine_id + device_id)[-16:]`，只出现在发起登录那次的 auth_url 里，
        # 攻击者拿不到，故能钉死到**那一次** pending 登录。
        raw = str(request.url)
        provider = services.registry.get("trae")
        pending: dict[str, tuple[float, str]] = request.app.state.pending_callbacks
        if provider is None:
            return JSONResponse(status_code=400, content=error_payload(
                "no pending TRAE login in progress", "invalid_request", 400))
        # 会话 Cookie → 发起人（回调为顶层导航，Lax Cookie 会带上）。
        # 与 deps.principal_from_request 同一套活跃性校验：签名有效还不够，
        # 发起人必须仍存在且未被禁用、会话 epoch 未被改密/禁用作废，
        # 否则被撤销权限的管理员仍能完成登录把凭证塞进池子。
        verified = verify_session_token(
            request.cookies.get(SESSION_COOKIE, ""), services.settings.app_secret)
        username = ""
        if verified is not None:
            candidate, epoch = verified
            if (services.users.is_active(candidate)
                    and services.users.session_epoch(candidate) == epoch):
                username = candidate
        entry = pending.get(username) if username else None
        if entry is None:
            return JSONResponse(status_code=400, content=error_payload(
                "no pending TRAE login in progress", "invalid_request", 400))
        started_at, state = entry
        if time.monotonic() - started_at > PENDING_CALLBACK_TTL_SECONDS:
            pending.pop(username, None)
            return JSONResponse(status_code=400, content=error_payload(
                "pending TRAE login expired, start a new one",
                "invalid_request", 400))
        # 回调绑定：loginTraceID 必须等于本次 pending 登录的 trace id
        # （machine/device id 在 state 里）。缺失/不符一律拒绝——这才是
        # 「只接受发起人本人那次登录的回调」，与 Referer 无关。
        machine_id, _, device_id = state.partition(":")
        if request.query_params.get("loginTraceID") != login_trace_id(machine_id, device_id):
            return JSONResponse(status_code=400, content=error_payload(
                "callback does not match the pending login", "invalid_request", 400))
        if not request.query_params.get("refreshToken") and not request.query_params.get("userJwt"):
            return JSONResponse(status_code=400, content=error_payload(
                "callback missing refreshToken", "invalid_request", 400))
        # 先消费 pending，防止并发回调重复兑换同一登录
        pending.pop(username, None)
        try:
            credential_data = await provider.complete_callback(raw, state)
        except UpstreamProtocolViolation as error:
            return JSONResponse(status_code=400, content=error_payload(
                error.public_message, "invalid_credential", 400))
        credential_id = services.credentials.add(
            provider="trae", credential_data=credential_data,
            nickname=str(credential_data.get("nickname") or ""),
            added_by=username)
        services.schedule_probe(credential_id)
        return {"ok": True, "captured": True, "at": int(time.time())}

    return router
