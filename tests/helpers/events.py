"""出口测试通用的事件脚本常量。

`GOOD` 是「一个内容帧 + 一个收尾帧」的最小成功流，6 个文件里各写了一份
逐字节等价的定义（`tests/engine/_fallback_support.py` 亦引用它），收敛到这里。
"""

from __future__ import annotations

from src.provider.base import Event, EventKind, Usage

__all__ = ["GOOD", "GOOD_EVENTS", "TRAE_GOOD"]

GOOD = [Event(kind=EventKind.CONTENT, content="ok"),
        Event(kind=EventKind.FINISH, finish_reason="stop")]

# 带 usage 的成功流（`tests/api/test_playground.py` / `tests/engine/test_executor_stats.py` 各一份）
GOOD_EVENTS = [Event(kind=EventKind.CONTENT, content="hi"),
               Event(kind=EventKind.USAGE, usage=Usage(11, 22, 3)),
               Event(kind=EventKind.FINISH, finish_reason="stop")]

# trae 成功流（`tests/api/test_chat.py` / `tests/engine/test_executor.py` 各一份）
TRAE_GOOD = [Event(kind=EventKind.CONTENT, content="hi"),
             Event(kind=EventKind.USAGE, usage=Usage(1, 2, 0)),
             Event(kind=EventKind.FINISH, finish_reason="stop")]
