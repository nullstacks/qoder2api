"""进程内日志缓冲：脱敏白名单、环形裁剪、热更容量、handler 挂载。

**脱敏是硬前提**：这里逐形态钉住「整行丢弃」——宁可漏一条日志，也不能把
凭证推进浏览器。再用正常行证明不过滤过度（否则页面全是空白同样不可用）。
"""

from __future__ import annotations

import logging

import pytest

from src.webapp.log_buffer import (
    MAX_LINE_CHARS,
    LogBuffer,
    LogBufferHandler,
    detach_root_handler,
    redact_or_drop,
    sync_root_handler,
)

# ------------------------------------------------------------------ 脱敏


@pytest.mark.parametrize("line", [
    "POST /v1/chat 200 access_token=abc123",
    "上游返回 Authorization: Bearer eyJhbGciOi.xx.yy",
    "Authorization: Basic dXNlcjpwYXNz",
    "刷新 api_key=sk-live-123",
    "x-api-key: abc",
    "client_secret = zzz",
    "set-cookie: session=abcdef",
    "Cookie: sid=1",
    "password=hunter2",
    "passwd: hunter2",
    "tool called with sk-proj-abcdefghijklmnop",
    "GET /cb?token=abc&x=1",
    "some Bearer eyJhbGciOiJIUzI1NiJ9",
    "download secret file done",
    "token 已刷新",
    "API_KEY 缺失",
    '{"access_token": "abc", "user": "x"}',      # JSON 键名带引号
    '{"client_secret":"zzz"}',
    "token: abc",
    "Cookie: a=b; c=d",
])
def test_redact_drops_credential_shapes(line):
    """各类凭证形态（键值对 / 头部 / `sk-` / 裸 Bearer / 敏感词）整行丢弃。"""
    assert redact_or_drop(line) is None


@pytest.mark.parametrize("line", [
    'event=request request_id=abc method=POST route=/v1/chat/completions status=200 duration_ms=12',
    "启动预热模型列表失败: timeout",
    "task-run 完成，耗时 1200ms",          # `task-run` 不是 sk- 形状
    "模型 glm-5.2 的倍率 1.0",
    "凭证 cred_1 已 pin",
])
def test_redact_passes_normal_lines(line):
    """正常行原样放行（返回同一字符串，不额外改写）。"""
    assert redact_or_drop(line) == line


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_redact_drops_blank(line):
    """空行没有信息量，丢掉（页面不渲染空白行）。"""
    assert redact_or_drop(line) is None


# ------------------------------------------------------------------ 缓冲


def test_buffer_keeps_newest_lines_within_capacity():
    buffer = LogBuffer(capacity=lambda: 3)
    for index in range(5):
        buffer.append(level="INFO", logger_name="src.x", message=f"m{index}", ts=float(index))
    lines = buffer.snapshot()
    assert [line["message"] for line in lines] == ["m2", "m3", "m4"]
    assert [line["seq"] for line in lines] == [3, 4, 5]


def test_buffer_snapshot_limit_takes_tail():
    buffer = LogBuffer(capacity=lambda: 10)
    for index in range(4):
        buffer.append(level="INFO", logger_name="src.x", message=f"m{index}")
    assert [line["message"] for line in buffer.snapshot(limit=2)] == ["m2", "m3"]
    # limit=0 不是「不限」而是「不取」：调用方（端点）已把上限夹到 >=1
    assert buffer.snapshot(limit=0) == []


def test_buffer_capacity_hot_reload_trims_oldest():
    """容量热更调小：下一次写入/读取就丢掉最旧的行，不等到重启。"""
    capacity = {"value": 5}
    buffer = LogBuffer(capacity=lambda: capacity["value"])
    for index in range(5):
        buffer.append(level="INFO", logger_name="src.x", message=f"m{index}")
    assert len(buffer.snapshot()) == 5

    capacity["value"] = 2
    assert [line["message"] for line in buffer.snapshot()] == ["m3", "m4"]

    capacity["value"] = 4
    buffer.append(level="INFO", logger_name="src.x", message="m5")
    assert [line["message"] for line in buffer.snapshot()] == ["m3", "m4", "m5"]


def test_buffer_closed_discards_everything():
    """`log_buffer_lines=0`：不保留任何行（也不给订阅者推送）。"""
    capacity = {"value": 3}
    buffer = LogBuffer(capacity=lambda: capacity["value"])
    buffer.append(level="INFO", logger_name="src.x", message="keep")
    capacity["value"] = 0
    assert not buffer.enabled
    assert buffer.append(level="INFO", logger_name="src.x", message="dropped") is None
    assert buffer.snapshot() == []


def test_buffer_negative_capacity_is_treated_as_zero():
    buffer = LogBuffer(capacity=lambda: -5)
    assert buffer.capacity() == 0
    assert not buffer.enabled


def test_buffer_line_carries_request_id_only_when_present():
    buffer = LogBuffer(capacity=lambda: 5)
    with_id = buffer.append(level="WARNING", logger_name="src.api.chat",
                            message="上游 500", request_id="req-1", ts=1.5)
    without = buffer.append(level="INFO", logger_name="src.tasks.runner",
                            message="定时任务完成", ts=2.5)
    assert with_id == {"seq": 1, "ts": 1.5, "level": "WARNING",
                       "logger": "src.api.chat", "message": "上游 500",
                       "request_id": "req-1"}
    assert "request_id" not in without


def test_buffer_publish_reaches_subscriber_via_loop():
    """有订阅者且绑定事件循环时，新行要真的被推给订阅者（实时流的数据通道）。"""

    class FakeLoop:
        def __init__(self) -> None:
            self.published: list[dict] = []

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, callback, payload) -> None:
            callback(payload)
            self.published.append(payload)

    buffer = LogBuffer(capacity=lambda: 5)
    subscription = buffer.hub.subscribe()
    loop = FakeLoop()
    buffer.bind_loop(loop)                        # type: ignore[arg-type]
    buffer.append(level="INFO", logger_name="src.x", message="live")
    assert [line["message"] for line in subscription.drain()] == ["live"]

    buffer.hub.unsubscribe(subscription)
    buffer.append(level="INFO", logger_name="src.x", message="nobody")
    # 没有订阅者时不排队（首帧快照已包含此刻之前的全部行）
    assert len(loop.published) == 1


def test_buffer_skips_publish_without_loop_or_with_closed_loop():
    buffer = LogBuffer(capacity=lambda: 5)
    subscription = buffer.hub.subscribe()
    buffer.append(level="INFO", logger_name="src.x", message="no-loop")

    class ClosedLoop:
        def is_closed(self) -> bool:
            return True

        def call_soon_threadsafe(self, callback, payload):  # pragma: no cover - 不该被调用
            raise AssertionError("事件循环已关闭时不应推送")

    buffer.bind_loop(ClosedLoop())                # type: ignore[arg-type]
    buffer.append(level="INFO", logger_name="src.x", message="closed-loop")
    assert subscription.drain() == []


# ------------------------------------------------------------------ handler


def _record(message: str, *, name: str = "src.api.chat", level: int = logging.INFO,
            **extra) -> logging.LogRecord:
    record = logging.LogRecord(name, level, __file__, 1, message, None, None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_handler_writes_formatted_message_and_level():
    buffer = LogBuffer(capacity=lambda: 5)
    handler = LogBufferHandler(buffer)
    handler.emit(_record("管理员 %s 删除凭证 %s", name="src.api.admin_credentials",
                         level=logging.WARNING, args=("root", "cred_1")))
    line = buffer.snapshot()[0]
    assert line["message"] == "管理员 root 删除凭证 cred_1"
    assert line["level"] == "WARNING"
    assert line["logger"] == "src.api.admin_credentials"


def test_handler_keeps_message_only_not_timestamp_prefix():
    """缓冲行已带 ts/level/logger 字段：格式化结果里不该再拼一遍（页面会重复）。"""
    buffer = LogBuffer(capacity=lambda: 5)
    LogBufferHandler(buffer).emit(_record("纯正文"))
    assert buffer.snapshot()[0]["message"] == "纯正文"


def test_handler_survives_record_with_exc_info():
    """traceback 可能很长且含敏感内容：格式化照常，脱敏按整行判定。"""
    buffer = LogBuffer(capacity=lambda: 5)
    handler = LogBufferHandler(buffer)
    try:
        raise ValueError("上游 500")
    except ValueError:
        record = logging.LogRecord("src.api.chat", logging.ERROR, __file__, 1,
                                   "请求失败", None, __import__("sys").exc_info())
    handler.emit(record)
    assert buffer.snapshot()[0]["message"].startswith("请求失败")


def test_handler_drops_sensitive_line():
    buffer = LogBuffer(capacity=lambda: 5)
    LogBufferHandler(buffer).emit(_record("Authorization: Bearer abc.def"))
    assert buffer.snapshot() == []


def test_handler_reads_request_id_from_record_then_contextvar():
    from src.request_context import set_request_id

    buffer = LogBuffer(capacity=lambda: 5)
    handler = LogBufferHandler(buffer)
    handler.emit(_record("带记录上的 id", request_id="req-record"))
    set_request_id("req-context")
    try:
        handler.emit(_record("带上下文的 id"))
    finally:
        set_request_id(None)
    handler.emit(_record("后台任务无 id"))
    lines = buffer.snapshot()
    assert [line.get("request_id") for line in lines] == ["req-record", "req-context", None]


def test_handler_truncates_overlong_message():
    buffer = LogBuffer(capacity=lambda: 5)
    LogBufferHandler(buffer).emit(_record("x" * (MAX_LINE_CHARS + 50)))
    message = buffer.snapshot()[0]["message"]
    assert len(message) == MAX_LINE_CHARS + len("…（已截断）")
    assert message.endswith("…（已截断）")


def test_handler_swallows_buffer_errors():
    """缓冲故障不得冒泡进业务代码（一条日志不能把请求打成 500）。"""

    class BrokenBuffer(LogBuffer):
        def append(self, **_kwargs):
            raise RuntimeError("boom")

    handler = LogBufferHandler(BrokenBuffer(capacity=lambda: 5))
    seen: list[logging.LogRecord] = []
    handler.handleError = lambda record: seen.append(record)   # type: ignore[method-assign]
    handler.emit(_record("会炸"))
    assert len(seen) == 1


# ------------------------------------------------------------------ 挂载


@pytest.fixture()
def clean_root():
    """临时 root：用例结束复原，避免影响其他测试的日志断言。"""
    root = logging.getLogger()
    saved = list(root.handlers)
    yield root
    root.handlers = saved


def test_sync_root_handler_attaches_exactly_once(clean_root):
    buffer = LogBuffer(capacity=lambda: 5)
    first = sync_root_handler(buffer, clean_root)
    second = sync_root_handler(buffer, clean_root)
    assert first is second
    assert sum(isinstance(h, LogBufferHandler) for h in clean_root.handlers) == 1


def test_sync_root_handler_replaces_foreign_instance(clean_root):
    """开发 reload / 反复 build_app：旧实例要被换掉，不能叠加两份。"""
    old = sync_root_handler(LogBuffer(capacity=lambda: 5), clean_root)
    buffer = LogBuffer(capacity=lambda: 5)
    new = sync_root_handler(buffer, clean_root)
    assert new is not None and new is not old
    assert old not in clean_root.handlers
    assert sum(isinstance(h, LogBufferHandler) for h in clean_root.handlers) == 1


def test_sync_root_handler_detaches_when_disabled(clean_root):
    buffer = LogBuffer(capacity=lambda: 5)
    sync_root_handler(buffer, clean_root)
    disabled = LogBuffer(capacity=lambda: 0)
    assert sync_root_handler(disabled, clean_root) is None
    assert not any(isinstance(h, LogBufferHandler) for h in clean_root.handlers)


def test_detach_root_handler_is_idempotent(clean_root):
    buffer = LogBuffer(capacity=lambda: 5)
    sync_root_handler(buffer, clean_root)
    assert detach_root_handler(buffer, clean_root) is True
    assert detach_root_handler(buffer, clean_root) is False
