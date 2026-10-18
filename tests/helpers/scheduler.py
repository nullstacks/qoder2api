"""调度器错误结果的构造助手。

`_disabled_outcome`（DEAD 错误结果）在 3 个文件里各写一份逐字节等价的实现，
收敛到这里。
"""

from __future__ import annotations

from src.engine.scheduler import Candidate, Scheduler
from src.provider.base import ErrKind

__all__ = ["disabled_outcome"]


def disabled_outcome():
    """返回一个 DEAD（禁用）错误结果，供去重/状态断言复用。"""
    return Scheduler().note_error(Candidate(credential_id="c", provider="codebuddy"),
                                  ErrKind.DEAD, 0)
