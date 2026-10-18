"""CodeBuddy 测试共享装配助手。

`fixture`(3) 与 `_client`(3) 曾在该目录的多个测试文件里各写一份逐字节等价
的实现，收敛到这里。
"""

from __future__ import annotations

from src.provider.codebuddy.client import CodeBuddyClient
from tests.helpers.http import provider_client
from tests.helpers.paths import provider_fixture

__all__ = ["client", "fixture"]


def fixture(name: str) -> str:
    """读取 CodeBuddy 的真实样本文件内容。"""
    return provider_fixture("codebuddy", name)


def client(handler) -> CodeBuddyClient:
    """用 `handler` 造 CodeBuddyClient。"""
    return provider_client(CodeBuddyClient, handler)
