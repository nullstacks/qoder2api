"""Kilo SSE 帧解析、事件聚合与错误分类。"""

from __future__ import annotations

import pytest

from src.engine.sse import SSEFrame
from src.provider.base import ErrKind, EventKind
from src.provider.kilo import events as kilo_events
from tests.helpers.http import chunk_payload as chunk
from tests.helpers.http import sse_frame as frame

pytestmark = pytest.mark.real_upstream("kilo")


def test_parse_frame_returns_none_for_done_and_empty():
    assert kilo_events.parse_frame(SSEFrame(event="", data="")) is None
    assert kilo_events.parse_frame(SSEFrame(event="", data=" [DONE] ")) is None

def test_parse_frame_content_and_reasoning():
    content = kilo_events.parse_frame(frame(chunk({"content": "hi"})))
    assert content is not None and content.kind is EventKind.CONTENT
    # Kilo 用 delta.reasoning（不是 zen 的 reasoning_content）
    reasoning = kilo_events.parse_frame(frame(chunk({"reasoning": "think"})))
    assert reasoning is not None and reasoning.kind is EventKind.REASONING
    assert reasoning.content == "think"

def test_parse_frame_ignores_reasoning_content_field():
    """zen 的 reasoning_content 在 Kilo 不是思考通道：不产出 REASONING。"""
    assert kilo_events.parse_frame(frame(chunk({"reasoning_content": "x"}))) is None

def test_parse_frame_tool_calls_drops_blank_noise():
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "", "arguments": ""}},    # 噪声
        {"index": 1, "function": {"name": "", "arguments": "{}"}},   # "{}" 空壳也算噪声
        {"index": 2, "function": {"arguments": {}}},                 # 空对象也算噪声
        {"index": 3, "function": {"name": "grep", "arguments": ""}},
    ]})
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert [c["index"] for c in event.tool_calls] == [3]

def test_parse_frame_tool_calls_all_blank_falls_through():
    """全部是空名噪声：整批丢弃，不产出 tool_calls 事件。"""
    payload = chunk({"tool_calls": [
        {"index": 0, "function": {"name": "  ", "arguments": ""}},
        {"index": 1, "function": {"name": "", "arguments": "{}"}},
    ], "content": "text"})
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.CONTENT

def test_parse_frame_tool_calls_tolerates_non_dict_entries():
    """非 dict 的 tool_call 条目被跳过，不崩。"""
    payload = chunk({"tool_calls": [
        "junk",
        {"index": 1, "function": {"name": "grep", "arguments": "x"}},
    ]})
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.kind is EventKind.TOOL_CALLS
    assert [c["index"] for c in event.tool_calls] == [1]

def test_parse_frame_usage_and_finish():
    usage_event = kilo_events.parse_frame(frame(chunk(
        {}, choices=[], usage={"prompt_tokens": 3})))
    assert usage_event is not None and usage_event.kind is EventKind.USAGE
    finish = kilo_events.parse_frame(frame(chunk({}, finish="stop")))
    assert finish is not None and finish.kind is EventKind.FINISH

def test_parse_frame_no_choices_returns_none():
    assert kilo_events.parse_frame(frame({"choices": []})) is None
    assert kilo_events.parse_frame(frame({"usage": None})) is None
    # choices 缺失 → None
    assert kilo_events.parse_frame(frame({"usage": None, "id": "x"})) is None

def test_parse_frame_bad_payload_raises():
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(SSEFrame(event="", data="{not json"))
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(SSEFrame(event="", data="[1, 2]"))
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(frame({"choices": "nope"}))
    with pytest.raises(kilo_events.UpstreamProtocolViolation):
        kilo_events.parse_frame(frame({"choices": ["nope"]}))

def test_parse_frame_error_envelope_int_code():
    event = kilo_events.parse_frame(frame({"error": {"code": 429, "message": "slow"}}))
    assert event is not None and event.kind is EventKind.ERROR
    assert event.error_code == 429 and event.error_message == "slow"
    # 429 是模型级限流（OpenRouter 共享池点名模型），只冷却触发的模型
    assert event.error_kind is ErrKind.MODEL

def test_parse_frame_error_envelope_string_code_and_missing_message():
    event = kilo_events.parse_frame(frame({"error": {"code": "bad", "message": None}}))
    assert event is not None and event.error_kind is ErrKind.OTHER
    assert event.error_code is None and event.error_message == ""

def test_parse_frame_error_string_envelope_is_ignored():
    """Kilo 的 HTTP 404 用 `{"error": "..."}`（字符串），流内不产 ERROR 事件。"""
    assert kilo_events.parse_frame(frame({"error": "not found"})) is None

def test_parse_all_events_splits_content_and_finish():
    """最后一段正文与 finish_reason 同帧：两者都要产出。"""
    payload = chunk({"content": "!"}, finish="stop")
    kinds = [e.kind for e in kilo_events.parse_all_events(frame(payload))]
    assert kinds == [EventKind.CONTENT, EventKind.FINISH]

def test_parse_all_events_appends_usage_to_finish_chunk():
    payload = chunk({"content": "!"}, finish="stop",
                    usage={"prompt_tokens": 1, "completion_tokens": 2})
    events = kilo_events.parse_all_events(frame(payload))
    assert [e.kind for e in events] == [
        EventKind.CONTENT, EventKind.USAGE, EventKind.FINISH]

def test_parse_all_events_usage_only_chunk():
    events = kilo_events.parse_all_events(frame(chunk(
        {}, choices=[],
        usage={"prompt_tokens": 10, "completion_tokens": 5,
               "prompt_tokens_details": {"cached_tokens": 2}})))
    assert [e.kind for e in events] == [EventKind.USAGE]
    assert events[0].usage is not None
    assert events[0].usage.input_tokens == 10
    assert events[0].usage.cached_tokens == 2

def test_parse_all_events_empty_and_done():
    assert kilo_events.parse_all_events(SSEFrame(event="", data="")) == []
    assert kilo_events.parse_all_events(SSEFrame(event="", data="[DONE]")) == []

def test_parse_all_events_usage_without_finish():
    """usage 有、finish_reason 缺：只补 usage。"""
    events = kilo_events.parse_all_events(frame(chunk(
        {}, choices=[{"index": 0, "delta": {}, "finish_reason": None}],
        usage={"prompt_tokens": 1})))
    assert [e.kind for e in events] == [EventKind.USAGE]

def test_usage_tolerates_bad_types():
    """usage 字段类型不对（bool / 字符串 / 非 dict details）不能崩。"""
    payload = {"choices": [], "usage": {
        "prompt_tokens": True, "completion_tokens": "x",
        "reasoning_tokens": True, "cached_tokens": True,
        "prompt_tokens_details": "nope"}}
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.input_tokens is None
    assert event.usage.output_tokens is None
    assert event.usage.reasoning_tokens is None
    assert event.usage.cached_tokens is None

def test_usage_top_level_cached_tokens_fallback():
    """details 缺失或非 dict 时回落顶层 cached_tokens。"""
    payload = {"choices": [], "usage": {"prompt_tokens": 4, "cached_tokens": 3}}
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.cached_tokens == 3

def test_usage_details_without_cached_tokens_falls_back():
    """details 是 dict 但无 cached_tokens → 回落顶层。"""
    payload = {"choices": [], "usage": {
        "prompt_tokens_details": {}, "cached_tokens": 7}}
    event = kilo_events.parse_frame(frame(payload))
    assert event is not None and event.usage is not None
    assert event.usage.cached_tokens == 7

def test_classify_status_maps_each_branch():
    # 401 只代表「该模型需要付费 key」，不是虚拟凭证失效；归 DEAD 会因一次
    # 强制 @kilo 的付费模型请求把整条渠道硬禁用。
    assert kilo_events.classify_status(401) is ErrKind.INVALID
    # 429 是模型级限流（OpenRouter 共享池），不是账号级软冷却
    assert kilo_events.classify_status(429) is ErrKind.MODEL
    assert kilo_events.classify_status(400) is ErrKind.INVALID
    assert kilo_events.classify_status(404) is ErrKind.INVALID
    assert kilo_events.classify_status(422) is ErrKind.INVALID
    assert kilo_events.classify_status(403) is ErrKind.REQUEST
    # 上游 provider 瞬时不可用（实测 503 no endpoints available）也是模型级，
    # 不能累计成账号级 OTHER（3 次 → 整条渠道 10m）
    assert kilo_events.classify_status(502) is ErrKind.MODEL
    assert kilo_events.classify_status(503) is ErrKind.MODEL
    assert kilo_events.classify_status(504) is ErrKind.MODEL
    assert kilo_events.classify_status(500) is ErrKind.OTHER

def test_classify_error_code_maps_each_branch():
    assert kilo_events.classify_error_code(401) is ErrKind.INVALID
    assert kilo_events.classify_error_code(429) is ErrKind.MODEL
    assert kilo_events.classify_error_code(502) is ErrKind.MODEL
    assert kilo_events.classify_error_code(503) is ErrKind.MODEL
    assert kilo_events.classify_error_code(504) is ErrKind.MODEL
    assert kilo_events.classify_error_code(400) is ErrKind.INVALID
    assert kilo_events.classify_error_code(404) is ErrKind.INVALID
    assert kilo_events.classify_error_code(422) is ErrKind.INVALID
    assert kilo_events.classify_error_code(403) is ErrKind.REQUEST
    assert kilo_events.classify_error_code(999) is ErrKind.OTHER
    assert kilo_events.classify_error_code(None) is ErrKind.OTHER
