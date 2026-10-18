"""按脚本回放事件的 trae 替身 provider。

`FakeProvider` 在 `tests/api/test_chat.py` 与 `tests/engine/test_executor.py` 各写
一份逐字节等价的实现（按调用次数回放脚本、可抛异常、校验 accessToken），收敛
到这里。
"""

from __future__ import annotations

from src.provider.base import ErrKind, Model
from src.provider.trae.events import UpstreamProtocolViolation

__all__ = ["ScriptedProvider"]


class ScriptedProvider:
    """按脚本回放的 trae provider：越界后重复最后一组事件。"""

    id = "trae"

    def __init__(self, script: list) -> None:
        self.script = script
        self.calls = 0

    async def stream_chat(self, credential_data, payload, model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    async def list_models(self, _credential_data):
        return [Model(id="glm-5.2")]

    def import_credential(self, raw):
        if not raw.get("accessToken"):
            raise UpstreamProtocolViolation("credential missing accessToken")
        return dict(raw)

    def classify(self, status, body=b""):
        return ErrKind.OTHER
