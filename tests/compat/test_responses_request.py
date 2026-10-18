"""Responses 出口：入站请求映射（Responses 请求体 → chat 载荷）。"""

from __future__ import annotations

import pytest

from src.compat.openai.request import InvalidRequest
from src.compat.responses.request import parse_responses_request

# ------------------------------------------------------------- 入站映射


def test_maps_instructions_and_string_input():
    request = parse_responses_request({
        "model": "m", "instructions": "be brief", "input": "hi"})
    assert request.raw["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert request.raw["model"] == "m"
    assert request.raw["stream"] is False

def test_maps_message_items_and_developer_role():
    request = parse_responses_request({
        "input": [
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "rules"}]},
            {"role": "user", "content": "plain string content"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "answer"}]},
        ]})
    assert request.raw["messages"] == [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "plain string content"},
        {"role": "assistant", "content": "answer"},
    ]

def test_maps_function_call_and_output_history():
    """Codex 的 FunctionCallOutputPayload 线上是 {content} / {content_items}。"""
    request = parse_responses_request({"input": [
        {"type": "function_call", "name": "shell", "arguments": '{"a":1}',
         "call_id": "call_1"},
        {"type": "function_call_output", "call_id": "call_1",
         "output": {"content": "done"}},
        {"type": "function_call_output", "call_id": "call_2",
         "output": {"content_items": [{"type": "output_text", "text": "part"}]}},
        {"type": "function_call_output", "call_id": "call_3", "output": "raw"},
        {"type": "function_call_output", "call_id": "call_4"},
    ]})
    messages = request.raw["messages"]
    assert messages[0] == {
        "role": "assistant", "content": "",
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "shell", "arguments": '{"a":1}'}}],
    }
    assert messages[1] == {"role": "tool", "tool_call_id": "call_1", "content": "done"}
    assert messages[2] == {"role": "tool", "tool_call_id": "call_2", "content": "part"}
    assert messages[3] == {"role": "tool", "tool_call_id": "call_3", "content": "raw"}
    assert messages[4] == {"role": "tool", "tool_call_id": "call_4", "content": ""}

def test_function_call_falls_back_to_id_and_empty_arguments():
    request = parse_responses_request({"input": [
        {"type": "function_call", "name": "f", "id": "fc_9"}]})
    call = request.raw["messages"][0]["tool_calls"][0]
    assert call["id"] == "fc_9"
    assert call["function"]["arguments"] == ""

def test_function_call_explicit_empty_arguments_accepted():
    """显式 `arguments: ""`（本服务流对无参数调用的回传形状）不得判非法。

    与「省略键」同语义，此前会抛 InvalidRequest，导致带工具调用的对话续轮 400。
    """
    request = parse_responses_request({"input": [
        {"type": "function_call", "name": "f", "call_id": "c1", "arguments": ""}]})
    call = request.raw["messages"][0]["tool_calls"][0]
    assert call["function"]["arguments"] == ""

def test_reasoning_history_becomes_reasoning_content():
    request = parse_responses_request({"input": [
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "a"}],
         "content": [{"type": "reasoning_text", "text": "b"}]},
        {"type": "message", "role": "user", "content": "go"},
    ]})
    assert request.raw["messages"][0] == {
        "role": "assistant", "content": "", "reasoning_content": "ab"}

def test_reasoning_and_compaction_without_text_are_dropped():
    """Codex 每轮回传 encrypted_content 的 reasoning/compaction：无声丢弃。"""
    request = parse_responses_request({"input": [
        {"type": "reasoning", "summary": [], "content": None,
         "encrypted_content": "gAAAA"},
        {"type": "compaction", "encrypted_content": "gAAAA"},
        {"type": "message", "role": "user", "content": "hi"},
    ]})
    assert request.raw["messages"] == [{"role": "user", "content": "hi"}]

def test_maps_tools_tool_choice_and_sampling():
    request = parse_responses_request({
        "input": "hi", "temperature": 0.3, "top_p": 0.9,
        "max_output_tokens": 128, "reasoning": {"effort": "high"},
        "prompt_cache_key": "k1", "text": {"verbosity": "low"},
        "parallel_tool_calls": False,
        "tools": [{"type": "function", "name": "f", "description": "d",
                   "parameters": {"type": "object"}}],
        "tool_choice": {"type": "function", "name": "f"},
    })
    raw = request.raw
    assert raw["temperature"] == 0.3 and raw["top_p"] == 0.9
    assert raw["max_tokens"] == 128
    assert raw["reasoning_effort"] == "high"
    assert raw["prompt_cache_key"] == "k1"
    assert raw["verbosity"] == "low"
    assert raw["parallel_tool_calls"] is False
    assert raw["tools"] == [{"type": "function",
                             "function": {"name": "f", "description": "d",
                                          "parameters": {"type": "object"}}}]
    assert raw["tool_choice"] == {"type": "function", "function": {"name": "f"}}

def test_tool_choice_strings_pass_through():
    for value in ("auto", "none", "required"):
        parsed = parse_responses_request({"input": "hi", "tool_choice": value})
        assert parsed.raw["tool_choice"] == value

def test_rejects_non_finite_sampling_and_bad_max_output_tokens():
    """NaN/Inf 采样参数与越界 max_output_tokens 在入站即 400。"""
    with pytest.raises(InvalidRequest, match="must be a finite number"):
        parse_responses_request({"input": "hi", "temperature": float("inf")})
    with pytest.raises(InvalidRequest, match="must be a number"):
        parse_responses_request({"input": "hi", "top_p": "x"})
    with pytest.raises(InvalidRequest, match="temperature must be <="):
        parse_responses_request({"input": "hi", "temperature": 99999})
    with pytest.raises(InvalidRequest, match="max_output_tokens"):
        parse_responses_request({"input": "hi", "max_output_tokens": 0})
    with pytest.raises(InvalidRequest, match="max_output_tokens"):
        parse_responses_request({"input": "hi", "max_output_tokens": 10**9})
    # bool 是 int 子类，不能当合法上限透传
    with pytest.raises(InvalidRequest, match="max_output_tokens"):
        parse_responses_request({"input": "hi", "max_output_tokens": True})

def test_empty_tools_are_omitted():
    request = parse_responses_request({"input": "hi", "tools": []})
    assert "tools" not in request.raw

def test_text_format_text_and_json_object_pass_through():
    for kind in ("text", "json_object"):
        request = parse_responses_request(
            {"input": "hi", "text": {"format": {"type": kind}}})
        assert request.raw["response_format"] == {"type": kind}

def test_text_format_json_schema_expands_to_chat_shape():
    request = parse_responses_request({"input": "hi", "text": {
        "verbosity": "low",
        "format": {"type": "json_schema", "name": "out",
                   "description": "d", "strict": True,
                   "schema": {"type": "object"}}}})
    assert request.raw["verbosity"] == "low"
    assert request.raw["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "out", "description": "d", "strict": True,
                        "schema": {"type": "object"}}}

def test_text_format_json_schema_omits_optional_fields():
    request = parse_responses_request({"input": "hi", "text": {
        "format": {"type": "json_schema", "name": "out", "schema": {}}}})
    assert request.raw["response_format"] == {
        "type": "json_schema", "json_schema": {"name": "out", "schema": {}}}

def test_text_without_format_yields_no_response_format():
    request = parse_responses_request(
        {"input": "hi", "text": {"verbosity": "high"}})
    assert "response_format" not in request.raw
    assert request.raw["verbosity"] == "high"

def test_include_only_encrypted_reasoning_allowed():
    """Codex CLI 每轮都发 include=["reasoning.encrypted_content"]。"""
    request = parse_responses_request(
        {"input": "hi", "include": ["reasoning.encrypted_content"]})
    assert "include" not in request.raw

def test_stream_flag_flows_through():
    assert parse_responses_request({"input": "hi", "stream": True}).stream is True

@pytest.mark.parametrize(("body", "fragment"), [
    (None, "JSON object"),
    ({}, "input must be a string or an array"),
    ({"input": 5}, "input must be a string or an array"),
    ({"input": []}, "at least one message"),
    ({"input": [{"type": "reasoning"}]}, "at least one message"),
    ({"input": "hi", "store": True}, "store=true"),
    ({"input": "hi", "store": "true"}, "store must be a boolean"),
    ({"input": "hi", "previous_response_id": "resp_1"}, "previous_response_id"),
    ({"input": "hi", "background": True}, "background=true"),
    ({"input": "hi", "include": "x"}, "include must be an array"),
    ({"input": "hi", "include": ["other"]}, "include ['other']"),
    ({"input": "hi", "model": 5}, "model must be a string"),
    ({"input": "hi", "stream": "yes"}, "stream must be a boolean"),
    ({"input": "hi", "messages": "x", "tools": {}}, "tools must be an array"),
    ({"input": "hi", "tools": [{"type": "web_search"}]}, "web_search"),
    ({"input": "hi", "tools": [5]}, "tools[0] must be an object"),
    ({"input": "hi", "tools": [{"type": "function"}]}, "name must be a non-empty"),
    ({"input": "hi", "tool_choice": {"type": "mcp"}}, "tool_choice form"),
    ({"input": "hi", "tool_choice": {"type": "function"}}, "tool_choice.name"),
    ({"input": [5]}, "input items must be objects"),
    ({"input": [{"type": "local_shell_call"}]}, "local_shell_call"),
    ({"input": [{"type": "custom_tool_call"}]}, "custom_tool_call"),
    ({"input": [{"type": "nope"}]}, "unknown input item type"),
    ({"input": [{"type": "message", "role": "tool", "content": "x"}]},
     "role 'tool'"),
    ({"input": [{"type": "message", "role": "user", "content": {"a": 1}}]},
     "content must be a string or an array"),
    ({"input": [{"type": "message", "role": "user", "content": [5]}]},
     "content[0] must be an object"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "input_image"}]}]}, "image_url must be a non-empty"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "input_image", "image_url": ""}]}]},
     "image_url must be a non-empty"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "input_file", "file_id": "f"}]}]},
     "input_file"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "image_url", "image_url": "x"}]}]},
     "image_url"),
    ({"input": [{"type": "message", "role": "user",
                 "content": [{"type": "input_text"}]}]}, "text must be a string"),
    ({"input": [{"type": "message", "role": "user", "content": [{"type": "z"}]}]},
     "unknown content part type"),
    ({"input": [{"type": "function_call", "arguments": "x"}]},
     "name must be a non-empty"),
    ({"input": [{"type": "function_call", "name": "f", "arguments": 5}]},
     "arguments must be a string"),
    ({"input": [{"type": "function_call_output", "output": "x"}]},
     "call_id must be a non-empty"),
    ({"input": [{"type": "function_call_output", "call_id": "c", "output": 5}]},
     "output must be a string, object or array"),
    ({"instructions": 5, "input": "hi"}, "content must be a string or an array"),
    ({"input": "hi", "text": {"format": 5}}, "text.format must be an object"),
    ({"input": "hi", "text": {"format": {"type": "nope"}}}, "text.format.type"),
    ({"input": "hi", "text": {"format": {"type": "json_schema", "schema": {}}}},
     "text.format.name"),
    ({"input": "hi", "text": {"format": {"type": "json_schema", "name": "o"}}},
     "text.format.schema"),
])
def test_rejected_bodies(body, fragment):
    with pytest.raises(InvalidRequest, match=fragment.replace("[", r"\[")):
        parse_responses_request(body)

def test_function_call_output_accepts_array_and_empty_object():
    request = parse_responses_request({"input": [
        {"type": "function_call_output", "call_id": "c1",
         "output": [{"type": "input_text", "text": "arr"}]},
        {"type": "function_call_output", "call_id": "c2", "output": {}},
    ]})
    assert request.raw["messages"][0]["content"] == "arr"
    assert request.raw["messages"][1]["content"] == ""

def test_reasoning_with_malformed_blocks_is_dropped():
    request = parse_responses_request({"input": [
        {"type": "reasoning", "summary": [5], "content": {"nope": 1}},
        {"type": "message", "role": "user", "content": "hi"},
    ]})
    assert request.raw["messages"] == [{"role": "user", "content": "hi"}]

def test_empty_instructions_are_omitted():
    request = parse_responses_request({"instructions": "", "input": "hi"})
    assert request.raw["messages"] == [{"role": "user", "content": "hi"}]

def test_tool_without_description_or_parameters():
    request = parse_responses_request({"input": "hi", "tools": [
        {"type": "function", "name": "bare", "description": 5, "parameters": "x"}]})
    assert request.raw["tools"] == [{"type": "function",
                                     "function": {"name": "bare"}}]

def test_ignore_unknown_extra_fields():
    """Responses 私有键（client_metadata 等）不进上游载荷，也不报错。"""
    request = parse_responses_request({
        "input": "hi", "client_metadata": {"a": "b"}, "service_tier": "auto",
        "stream_options": {"x": 1}})
    for key in ("client_metadata", "service_tier", "stream_options", "store",
                "include", "input", "instructions"):
        assert key not in request.raw


def test_input_image_maps_to_image_url():
    """`input_image` → chat 的 image_url（上游实测接受 data URL 与 http URL）。"""
    request = parse_responses_request({"input": [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "看图"},
            {"type": "input_image", "image_url": "https://x/y.png", "detail": "high"},
        ]}]})
    assert request.raw["messages"] == [{"role": "user", "content": [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]}]


def test_pure_text_content_still_folds_to_string():
    """纯文本 part 仍折成字符串：无图请求的映射结果与改动前逐字节一致。"""
    request = parse_responses_request({"input": [
        {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "a"}, {"type": "output_text", "text": "b"},
        ]}]})
    assert request.raw["messages"] == [{"role": "user", "content": "ab"}]


def test_image_in_instructions_is_rejected():
    """`instructions` 只能是文本：出现图片 part 说明调用方理解有误，直接 400。"""
    with pytest.raises(InvalidRequest, match="image content is not supported"):
        parse_responses_request({"instructions": [
            {"type": "input_image", "image_url": "https://x/y.png"}], "input": "hi"})


def test_function_call_output_with_image_array():
    """`function_call_output.output` 的 part 数组可含图片。"""
    request = parse_responses_request({"input": [
        {"type": "function_call_output", "call_id": "c1", "output": [
            {"type": "input_text", "text": "结果"},
            {"type": "input_image", "image_url": "https://x/y.png"},
        ]}]})
    assert request.raw["messages"] == [{"role": "tool", "tool_call_id": "c1",
                                        "content": [
        {"type": "text", "text": "结果"},
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
    ]}]


def test_function_call_output_content_items_with_image():
    """`{content_items: [...]}` 形态同样支持图片 part。"""
    request = parse_responses_request({"input": [
        {"type": "function_call_output", "call_id": "c1",
         "output": {"content_items": [
             {"type": "input_image", "image_url": "https://x/y.png"}]}}]})
    assert request.raw["messages"][0]["content"] == [
        {"type": "image_url", "image_url": {"url": "https://x/y.png"}}]
