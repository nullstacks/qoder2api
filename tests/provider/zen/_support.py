"""Zen 测试共享装配助手。

`_client` 在 `test_client.py` / `test_provider.py` 各写一份逐字节等价的实现，
`SESSION_RE` 在 `test_client.py` / `test_gate.py` 各写一份，收敛到这里。
"""

from __future__ import annotations

import re

from src.provider.zen.client import ZenClient
from tests.helpers.http import provider_client

__all__ = ["SESSION_RE", "client"]

# 会话 id 的闸门口径：`ses_` + 12 位十六进制 + 14 位 base62。
SESSION_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")


def client(handler, **kw) -> ZenClient:
    """用 `handler` 造 ZenClient。"""
    return provider_client(ZenClient, handler, **kw)
