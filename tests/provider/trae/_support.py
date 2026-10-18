"""TRAE 测试共享装配助手。

`fixture` 在 `test_client.py` / `test_events.py` 各写一份逐字节等价的实现，
收敛到这里。
"""

from __future__ import annotations

from src.provider.trae.client import TraeClient
from tests.helpers.http import provider_client
from tests.helpers.paths import provider_fixture

__all__ = ["client", "fixture"]


def fixture(name: str) -> str:
    """读取 TRAE 的真实样本文件内容。"""
    return provider_fixture("trae", name)


def client(handler) -> TraeClient:
    """用 `handler` 造 TraeClient。"""
    return provider_client(TraeClient, handler)
