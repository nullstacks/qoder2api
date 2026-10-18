"""聊天请求与 JWT 构造助手。

各出口测试普遍需要「造一个最小 chat 请求」，此前在 6 个文件里各写一份
`_request`（分两组逐字节等价）。JWT 造签同样在 3 个文件里各写一份
`_jwt`+`_b64`。这里都收敛成一份。
"""

from __future__ import annotations

import base64
import json

from src.compat.openai.request import parse_chat_request

__all__ = ["b64url", "chat_request", "jwt_token"]


def chat_request(model: str = "glm-5.2"):
    """最小单轮 user 消息的 chat 请求（默认模型 glm-5.2）。"""
    return parse_chat_request({"messages": [{"role": "user", "content": "hi"}],
                               "model": model})


def b64url(obj: object) -> str:
    """base64url 编码任意可 JSON 序列化对象（含数组，用于负例）。"""
    return base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")


def jwt_token(exp: int, *, iat: int | None = None, header: dict | None = None,
              payload_extra: dict | None = None) -> str:
    """构造测试用 JWT（不签名；本地只解码不验签）。"""
    body: dict = {"exp": exp}
    if iat is not None:
        body["iat"] = iat
    body.update(payload_extra or {})
    return f"{b64url(header or {'alg': 'RS256', 'typ': 'JWT'})}.{b64url(body)}.sig"
