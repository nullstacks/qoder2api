"""执行引擎测试的双上游装配助手。

`DualProvider` + `dual_executor` 在 `tests/engine/test_executor.py` 与
`tests/api/test_chat_stream.py` 各有一份逐字节等价的实现（验证调度层对
trae / codebuddy 两个上游行为一致），收敛到这里。
"""

from __future__ import annotations

from src.engine.executor import Executor, ExecutorDeps
from src.engine.scheduler import Scheduler

__all__ = ["DualProvider", "dual_executor"]


class DualProvider:
    """同一脚本双 provider：验证调度层对两个上游行为一致。"""

    def __init__(self, provider_id: str, script) -> None:
        self.id = provider_id
        self.script = script
        self.calls = 0

    async def stream_chat(self, _credential_data, _payload, _model):
        index = min(self.calls, len(self.script) - 1)
        self.calls += 1
        for item in self.script[index]:
            if isinstance(item, Exception):
                raise item
            yield item

    def list_models(self, _credential_data):
        return []


def dual_executor(credentials, trae_script, cb_script, **kw):
    """装配 trae + codebuddy 双上游执行器，返回 `(executor, trae, codebuddy)`。"""
    trae = DualProvider("trae", trae_script)
    codebuddy = DualProvider("codebuddy", cb_script)
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": codebuddy}, credentials=credentials,
        scheduler=Scheduler(**kw), default_model="glm-5.2"))
    return executor, trae, codebuddy
