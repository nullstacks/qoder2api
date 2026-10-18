"""聊天出口测试共享替身与装配助手。

`_RejectProvider`(2) 与 `_http_400`(2) 曾在 `test_chat.py` /
`test_chat_narrowing.py` 各写一份逐字节等价的实现，收敛到这里；
`tests/regression/test_coverage_edge_executor.py` 亦引用 `RejectProvider`。
"""

from __future__ import annotations

__all__ = ["RejectProvider", "http_400"]


class RejectProvider:
    """可脚本化的 provider：按调用次序抛指定异常或返回事件。"""

    def __init__(self, provider_id: str, outcomes: list) -> None:
        self.id = provider_id
        self.outcomes = outcomes
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.outcomes) - 1)
        self.calls += 1
        item = self.outcomes[index]
        if isinstance(item, Exception):
            raise item
        for event in item:
            yield event

    def list_models(self, _credential_data):
        return []


def http_400() -> Exception:
    """CodeBuddy 400「模型不存在」的上游 HTTP 错误。"""
    from src.provider.codebuddy.client import UpstreamHTTPError

    return UpstreamHTTPError(400, b'{"code":1002,"msg":"model not found"}')
