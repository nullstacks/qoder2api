"""TRAE 客户端：请求体 prepare_body 改写（工具/角色/占位助理）。"""

from __future__ import annotations

import json

from src.provider.trae.client import prepare_body


def test_prepare_body_rewrites_openai_shape():
    body = prepare_body({"model": "glm-5.2",
                         "messages": [{"role": "user", "content": "hi"}]}, "glm-5.2")
    assert body["stream"] is True and body["function"] == "solo_work_lite"
    assert body["config_name"] == body["model"] == "glm-5.2"
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hi"}]

def test_prepare_body_mutations_do_not_leak_into_payload():
    """出站改写绝不穿透回 payload：会话粘性用请求原文算指纹，穿透即粘性失效。"""
    import copy

    payload = {
        "messages": [
            {"role": "developer", "content": "plan"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function", "function": {}}]},
            {"role": "user", "content": "hi"},
        ],
        "tools": [{"type": "function",
                   "function": {"name": "bash", "parameters": {"type": "object"}}}],
    }
    snapshot = copy.deepcopy(payload)
    body = prepare_body(payload, "m")

    assert payload == snapshot                       # 原文未被改写
    assert body["messages"][0]["role"] == "system"   # 出站确实发生改写
    assert body["messages"][1]["content"] == [{"type": "text", "text": "hi"}]
    assert body["tools"][0]["function"]["parameters"] == '{"type": "object"}'

def test_prepare_body_keeps_array_content_and_skips_non_dict():
    body = prepare_body({"messages": [
        {"role": "user", "content": [{"type": "text", "text": "x"}]},
        "raw",
    ]}, "m")
    assert body["messages"][0]["content"] == [{"type": "text", "text": "x"}]
    assert body["messages"][1] == "raw"

def test_prepare_body_normalizes_tool_choice_none():
    body = prepare_body({"messages": [], "tool_choice": "none", "tools": [{}],
                         "functions": [{}]}, "m")
    assert "tool_choice" not in body and "tools" not in body and "functions" not in body

def test_prepare_body_derives_function_name():
    body = prepare_body({"messages": [], "tool_choice": {"type": "function",
                                                         "function": {"name": "get_w"}}}, "m")
    assert body["tool_choice"] == "get_w"

def test_prepare_body_drops_unsupported_tool_choice_shape():
    body = prepare_body({"messages": [], "tool_choice": {"type": "auto"}}, "m")
    assert "tool_choice" not in body

def test_prepare_body_drops_empty_tool_calls_and_content():
    body = prepare_body({"messages": [{"role": "assistant", "content": None,
                                       "tool_calls": []}]}, "m")
    assert body["messages"] == []   # 唯一 assistant 被丢弃

def test_prepare_body_skips_non_dict_tool_call_entries():
    """非对象 tool_call 条目跳过（102-103 分支）。"""
    body = prepare_body({"messages": [{"role": "assistant", "content": None,
        "tool_calls": ["junk", {"id": "c", "type": "function",
                                "function": {"name": "bash", "arguments": "{}"}}]}]}, "m")
    tcs = body["messages"][0]["tool_calls"]
    assert len(tcs) == 1 and tcs[0]["function_call"]["name"] == "bash"
    assert body["messages"][0]["content"] is None   # 原实现保留 nil content

def test_prepare_body_keeps_valid_tool_calls():
    """有 name 的 function → function_call；无 function/name 的剔除。"""
    body = prepare_body({"messages": [{"role": "assistant", "content": "x",
        "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "bash", "arguments": "{\"a\":1}"}},
            {"id": "c2", "type": "function", "function": {"arguments": "{}"}},
            {"id": "c3"},
        ]}]}, "m")
    tcs = body["messages"][0]["tool_calls"]
    assert len(tcs) == 1 and tcs[0]["id"] == "c1"
    assert tcs[0]["function_call"] == {"name": "bash", "arguments": "{\"a\":1}"}
    assert "function" not in tcs[0]

def test_prepare_body_stringifies_tool_parameters():
    """OpenAI tools.parameters(object) → TRAE 要求 JSON 字符串（code=4001 回归）。"""
    from src.provider.trae.client import prepare_body

    payload = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"type": "function", "function": {
                "name": "read", "description": "d",
                "parameters": {"type": "object", "properties": {"p": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "no_params"}},   # 缺 parameters
            "junk",                                                     # 非对象跳过
        ],
        "tool_choice": "auto",
    }
    body = prepare_body(payload, "m")
    tools = body["tools"]
    assert isinstance(tools, list) and len(tools) == 2
    first = tools[0]["function"]["parameters"]
    assert isinstance(first, str) and json.loads(first)["type"] == "object"
    assert tools[1]["function"]["parameters"] == "{}"
    # tool_choice 归一化不受影响
    assert body["tool_choice"] == "auto"

def test_prepare_body_tools_edge_cases():
    """tools 非 list / parameters 为 list / 带 parameters 的字符串透传。"""
    from src.provider.trae.client import prepare_body

    # tools 不是 list → 原样保留（走 _stringify 的早退分支）
    body = prepare_body({"messages": [], "tools": "nope"}, "m")
    assert body["tools"] == "nope"

    # parameters 为 list → 同样字符串化
    body = prepare_body({"messages": [], "tools": [
        {"type": "function", "function": {"name": "f", "parameters": ["a"]}},
    ]}, "m")
    assert body["tools"][0]["function"]["parameters"] == '["a"]'

    # parameters 已是字符串 → 原样保留
    body = prepare_body({"messages": [], "tools": [
        {"type": "function", "function": {"name": "f", "parameters": "{}"}},
    ]}, "m")
    assert body["tools"][0]["function"]["parameters"] == "{}"

def test_prepare_body_drops_unnamed_function_call():
    """function_call 无 name 剔除后若全空 → 整个 tool_calls 删除（102-103）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c", "type": "function",
                         "function": {"arguments": "{}"}}]},
    ]}, "m")
    assert body["messages"] == []   # 唯一 assistant 被丢弃

def test_prepare_body_normalizes_developer_role():
    """TRAE 不认 developer 角色（静默空流 3003）→ 归一 system。"""
    body = prepare_body({"messages": [
        {"role": "developer", "content": "You are PI."},
        {"role": "user", "content": "hi"},
    ]}, "m")
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "user"]

def test_prepare_body_drops_empty_assistant_with_kept_tool_calls():
    """tool_call 有 name 保留 + assistant 占位删除后悬空 tool 也清（127-129）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "keep", "type": "function",
                         "function": {"name": "bash", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "keep", "content": "out"},
    ]}, "m")
    assert len(body["messages"]) == 2
    assert body["messages"][0]["tool_calls"][0]["id"] == "keep"
    assert body["messages"][1]["role"] == "tool"

def test_prepare_body_drops_placeholder_assistant_when_all_calls_dropped():
    """全部 tool_call 被剔 + content=None → 占位 assistant 整条丢弃（127-129）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "x", "type": "function",
                         "function": {"arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "out"},
    ]}, "m")
    assert [m["role"] for m in body["messages"]] == []

def test_prepare_body_keeps_assistant_placeholder_when_content_present():
    """占位 assistant 带 content → 保留（127-129 不走 continue 分支）。"""
    body = prepare_body({"messages": [
        {"role": "assistant", "content": "文本",
         "tool_calls": [{"id": "x", "type": "function",
                         "function": {"arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "x", "content": "out"},
    ]}, "m")
    assert body["messages"][0]["content"] == [{"type": "text", "text": "文本"}]
    assert "tool_calls" not in body["messages"][0]
    # 悬空 tool 消息被成对清理
    assert [m["role"] for m in body["messages"][1:]] == []
