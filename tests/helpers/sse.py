"""SSE 解析测试助手。

`parse_frames` 原在 `src/engine/sse.py`，但生产路径（各 provider client）只走
异步的 `iter_frames`；同步版仅被测试当「参考实现」用来与流式结果逐帧比对。
搬到 tests/ 后，生产模块不再暴露无人调用的同步解析入口。
"""

from __future__ import annotations

from src.engine.sse import FrameAssembler, SSEFrame

__all__ = ["parse_frames"]


def parse_frames(text: str) -> list[SSEFrame]:
    """把一段 SSE 文本解析为帧列表（与 `iter_frames` 同一状态机）。"""
    assembler = FrameAssembler()
    frames: list[SSEFrame] = []
    for line in text.split("\n"):
        frame = assembler.feed_line(line)
        if frame is not None:
            frames.append(frame)
    tail = assembler.flush()
    if tail is not None:
        frames.append(tail)
    return frames
