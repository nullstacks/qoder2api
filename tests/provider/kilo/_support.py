"""Kilo 测试共享装配助手。

`_client` 在 `test_client.py` / `test_provider.py` 各写一份逐字节等价的实现，
收敛到这里。
"""

from __future__ import annotations

from src.provider.kilo.client import KiloClient
from tests.helpers.http import provider_client

__all__ = ["client"]


def client(handler, **kw) -> KiloClient:
    """用 `handler` 造 KiloClient（默认无路径测试主机，便于按端点常量断言）。"""
    kw.setdefault("host", "https://kilo.test")
    return provider_client(KiloClient, handler, **kw)
