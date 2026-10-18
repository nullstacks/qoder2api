"""Anthropic 出口：`cache_control` 原样透传（默认开启，无开关）。

实测七渠道中 6 个接受（codebuddy / codebuddy_intl / trae / qoder / codearts /
kilo；zen 因上游 FreeTierError 403 未验证），见 `scripts/probe_cache_control.py`。
本文件固定每个落点的形状：**没有标记的请求必须逐字节不变**（会话粘性指纹与
上游前缀缓存都依赖这个形状），有标记时标记跟着块走、不丢不改。
"""

from __future__ import annotations

import json

import pytest

from src.compat.anthropic.request import parse_messages_request
from src.compat.openai.request import InvalidRequest

MARK = {"type": "ephemeral"}


def _body(**overrides) -> dict:
    body = {"model": "glm-5.2", "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


# ------------------------------------------------------------ 无标记：形状不变


def test_without_marker_shapes_are_byte_identical():
    """回归基线：不带 `cache_control` 时仍折字符串（折成数组会改变指纹）。"""
    parsed = parse_messages_request(_body(
        system=[{"type": "text", "text": "a"}, {"type": "text", "text": "b"}],
        messages=[
            {"role": "user", "content": [{"type": "text", "text": "u1"},
                                         {"type": "text", "text": "u2"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
        ]))
    assert parsed.messages[0] == {"role": "system", "content": "ab"}
    assert parsed.messages[1] == {"role": "user", "content": "u1u2"}
    assert parsed.messages[2] == {"role": "assistant", "content": "a1"}


# ------------------------------------------------------------ 各落点


def test_system_blocks_keep_marker_and_array_shape():
    parsed = parse_messages_request(_body(system=[
        {"type": "text", "text": "big prompt", "cache_control": MARK},
        {"type": "text", "text": "tail"},
    ]))
    assert parsed.messages[0] == {"role": "system", "content": [
        {"type": "text", "text": "big prompt", "cache_control": MARK},
        {"type": "text", "text": "tail"},
    ]}


def test_user_text_block_marker_keeps_content_array():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "text", "text": "u1", "cache_control": {"type": "ephemeral",
                                                         "ttl": "1h"}},
        {"type": "text", "text": "u2"},
    ]}]))
    assert parsed.messages[0]["content"] == [
        {"type": "text", "text": "u1",
         "cache_control": {"type": "ephemeral", "ttl": "1h"}},
        {"type": "text", "text": "u2"},
    ]


def test_user_image_block_marker():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "image", "cache_control": MARK,
         "source": {"type": "url", "url": "https://example.com/a.png"}},
    ]}]))
    assert parsed.messages[0]["content"][0]["cache_control"] == MARK
    assert parsed.messages[0]["content"][0]["type"] == "image_url"


def test_assistant_text_block_marker():
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "text", "text": "a", "cache_control": MARK}]},
    ]))
    assert parsed.messages[1] == {"role": "assistant", "content": [
        {"type": "text", "text": "a", "cache_control": MARK}]}


def test_assistant_tool_use_marker_sits_on_tool_call_top_level():
    """`function` 只认 name/arguments：标记挂 `tool_calls` 元素顶层。"""
    parsed = parse_messages_request(_body(messages=[
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {},
             "cache_control": MARK}]},
    ]))
    tool_call = parsed.messages[1]["tool_calls"][0]
    assert tool_call["function"]["name"] == "f"
    assert tool_call["cache_control"] == MARK
    # `input` 是对象 → JSON 序列化（空对象序列化成空对象）
    assert json.loads(tool_call["function"]["arguments"]) == {}


def test_tool_result_marker_sits_on_message_top_level():
    """tool 消息的 content 常是字符串，标记挂消息顶层（不造空 text 块）。"""
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "res",
         "cache_control": MARK}]}]))
    assert parsed.messages[0] == {"role": "tool", "tool_call_id": "t1",
                                  "content": "res", "cache_control": MARK}


def test_tool_definition_marker():
    parsed = parse_messages_request(_body(tools=[
        {"name": "f", "input_schema": {"type": "object"}, "cache_control": MARK}]))
    tool = parsed.raw["tools"][0]
    assert tool["cache_control"] == MARK
    assert "cache_control" not in tool["function"]


def test_tool_result_content_block_marker_keeps_array():
    parsed = parse_messages_request(_body(messages=[{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "res", "cache_control": MARK}]}]}]))
    assert parsed.messages[0]["content"] == [
        {"type": "text", "text": "res", "cache_control": MARK}]


# ------------------------------------------------------------ 校验


@pytest.mark.parametrize("marker, message", [
    ("ephemeral", r"system\[0\]\.cache_control must be an object"),
    ({}, r"system\[0\]\.cache_control\.type must be a non-empty string"),
    ({"type": ""}, r"system\[0\]\.cache_control\.type must be a non-empty string"),
    ({"type": "ephemeral", "ttl": 5},
     r"system\[0\]\.cache_control\.ttl must be a string"),
])
def test_marker_structure_is_validated(marker, message):
    """结构不合法显式 400（静默丢弃会悄悄吃掉客户端意图）。"""
    with pytest.raises(InvalidRequest, match=message):
        parse_messages_request(_body(system=[
            {"type": "text", "text": "x", "cache_control": marker}]))


def test_unknown_marker_type_is_passed_through():
    """取值原样透传：本网关是管道，上游将来新增的缓存类型不该被挡在门外。"""
    marker = {"type": "persistent"}
    parsed = parse_messages_request(_body(system=[
        {"type": "text", "text": "x", "cache_control": marker}]))
    assert parsed.messages[0]["content"][0]["cache_control"] == marker
