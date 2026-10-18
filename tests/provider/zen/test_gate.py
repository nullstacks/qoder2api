"""Zen 门禁伪装（会话 id / gate 头 / 工具注入与改写）与回包过滤。"""

from __future__ import annotations

import pytest

from src.provider.zen.client import (
    _InjectedToolFilter,
    ensure_gate_tools,
    gate_aliases,
    gate_headers,
    new_session_id,
    prepare_body,
)
from tests.provider.zen._support import SESSION_RE

pytestmark = pytest.mark.real_upstream("zen")

def test_new_session_id_matches_gate_regex():
    session = new_session_id()
    assert SESSION_RE.match(session), session
    assert new_session_id() != session

def test_new_session_id_prefix_encodes_recent_time():
    """前 12 位 hex 必须是近期时间戳的官方编码（随机 hex 会被上游 403）。

    官方 `Identifier.create`：`~((now_ms << 12) + 1)` 的低 48 位。左移会溢出
    48 位故按模比较（与上游一致）；两值之差换算回毫秒须精确等于注入的时间。
    """
    from src.provider.zen.client import _SESSION_TIME_MASK, _SESSION_TIME_SHIFT

    now_ms = 1_700_000_000_123
    session = new_session_id(now_ms=now_ms)
    actual = (~int(session[4:16], 16)) & _SESSION_TIME_MASK
    expected = (now_ms * _SESSION_TIME_SHIFT + 1) & _SESSION_TIME_MASK
    assert actual == expected
    # 后 14 位仍是随机 base62：两次调用前缀相同、整体不同
    again = new_session_id(now_ms=now_ms)
    assert again[4:16] == session[4:16]
    assert again != session

def test_new_session_id_prefix_advances_with_clock():
    """不同时刻生成的会话前缀不同：确保前缀确实取自时钟而非常量。"""
    early = new_session_id(now_ms=1_700_000_000_000)[4:16]
    later = new_session_id(now_ms=1_700_000_001_000)[4:16]
    assert early != later

def test_gate_headers_include_version_and_session():
    headers = gate_headers(version="2.3.4")
    assert headers["User-Agent"] == "opencode/2.3.4"
    assert SESSION_RE.match(headers["x-opencode-session"])
    assert headers["Accept"] == "text/event-stream"
    assert headers["Authorization"] == "Bearer public"

def test_gate_headers_reuse_explicit_session():
    headers = gate_headers(session_id="ses_" + "a" * 12 + "b" * 14)
    assert headers["x-opencode-session"] == "ses_" + "a" * 12 + "b" * 14

def test_ensure_gate_tools_injects_both_when_missing():
    body: dict = {"messages": []}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})
    names = [tool["function"]["name"] for tool in body["tools"]]
    assert names == ["bash", "read"]

def test_ensure_gate_tools_injects_only_missing():
    body = {"tools": [{"type": "function", "function": {"name": "bash"}}]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"read"})
    assert [t["function"]["name"] for t in body["tools"]] == ["bash", "read"]

def test_ensure_gate_tools_keeps_user_tools_verbatim():
    body = {"tools": [
        {"type": "function", "function": {"name": "bash"}},
        {"type": "function", "function": {"name": "read"}},
        {"type": "function", "function": {"name": "grep"}},
    ]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset()
    assert len(body["tools"]) == 3

def test_ensure_gate_tools_tolerates_malformed_entries():
    """非 dict 工具 / function 非 dict：不能崩，按「缺失」补门禁工具。"""
    body = {"tools": [None, "junk", {"type": "function"}, {"function": "x"}]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})

def test_ensure_gate_tools_ignores_non_list_tools():
    body = {"tools": {"not": "a list"}}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})
    assert isinstance(body["tools"], list)

def test_ensure_gate_tools_copies_synonym_schema():
    """注入的 bash 复制同义工具 shell 的描述与参数 schema（供改写透传）。"""
    body = {"tools": [
        {"type": "function", "function": {"name": "shell",
                                          "description": "Run a command",
                                          "parameters": {"type": "object",
                                                         "properties": {
                                                             "command": {"type": "string"}},
                                                         "required": ["command"]}}},
    ]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})
    by_name = {t["function"]["name"]: t["function"] for t in body["tools"]}
    assert by_name["bash"]["description"] == "Run a command"
    assert by_name["bash"]["parameters"]["required"] == ["command"]
    # 无同义的 read 仍是最小空壳
    assert by_name["read"] == {"name": "read", "parameters": {"type": "object"}}

def test_ensure_gate_tools_tolerates_malformed_synonym():
    """同义工具的 description/parameters 非法 → 跳过，不复制坏字段。"""
    body = {"tools": [
        {"type": "function", "function": {"name": "shell",
                                          "description": 123,
                                          "parameters": "junk"}},
    ]}
    ensure_gate_tools(body)
    bash = next(t["function"] for t in body["tools"]
                if t["function"]["name"] == "bash")
    assert bash == {"name": "bash", "parameters": {"type": "object"}}

def test_find_synonym_skips_malformed_tools():
    body = {"tools": [None, {"function": "x"},
                      {"type": "function"},
                      {"function": {"name": "shell"}}]}
    injected = ensure_gate_tools(body)
    assert injected == frozenset({"bash", "read"})

def test_gate_aliases_maps_injected_to_present_synonyms():
    tools = [{"type": "function", "function": {"name": "shell"}},
             {"type": "function", "function": {"name": "read"}}]
    aliases = gate_aliases(tools, frozenset({"bash", "read"}))
    assert aliases == {"bash": "shell"}  # read 的同义 read_file 不在客户端

def test_gate_aliases_prefers_first_synonym():
    tools = [{"function": {"name": "execute"}},
             {"function": {"name": "shell"}}]
    assert gate_aliases(tools, frozenset({"bash"})) == {"bash": "shell"}

def test_gate_aliases_ignores_unmapped_and_malformed_input():
    # 注入名不在同义表 → 不映射（调用按丢弃处理）
    assert gate_aliases([{"function": {"name": "shell"}}],
                        frozenset({"mcp_tool"})) == {}
    # tools 缺失/非 list → 空映射
    assert gate_aliases(None, frozenset({"bash"})) == {}
    assert gate_aliases([{"function": {"name": "shell"}}],
                        frozenset()) == {}

def test_prepare_body_forces_stream_and_deepcopies_messages():
    messages = [{"role": "user", "content": "hi"}]
    body, injected = prepare_body({"messages": messages, "stream": False, "tools": []},
                                  "big-pickle")
    assert body["stream"] is True
    assert body["model"] == "big-pickle"
    assert injected == frozenset({"bash", "read"})
    # 深拷贝：改写上游 body 不影响引擎持有的原始请求体
    body["messages"][0]["content"] = "changed"
    assert messages[0]["content"] == "hi"

def test_tool_filter_drops_injected_by_name():
    flt = _InjectedToolFilter(frozenset({"bash"}))
    kept = flt.keep([{"index": 0, "function": {"name": "bash"}},
                     {"index": 1, "function": {"name": "grep"}}])
    assert [c["index"] for c in kept] == [1]

def test_tool_filter_drops_index_continuations():
    """分片续传：首片带 name 被丢，后续只有 arguments 的同 index 片一并丢。"""
    flt = _InjectedToolFilter(frozenset({"read"}))
    assert flt.keep([{"index": 3, "function": {"name": "read"}}]) == []
    assert flt.keep([{"index": 3, "function": {"arguments": "{\"p\""}}]) == []
    # 其他 index 不受影响
    assert flt.keep([{"index": 4, "function": {"arguments": "x"}}])

def test_tool_filter_drops_injected_without_index():
    flt = _InjectedToolFilter(frozenset({"bash"}))
    assert flt.keep([{"function": {"name": "bash"}}]) == []

def test_tool_filter_keeps_real_tools_and_non_dict_function():
    flt = _InjectedToolFilter(frozenset({"bash"}))
    kept = flt.keep([{"index": 0, "function": "weird"}, {"index": 1}])
    assert len(kept) == 2

def test_tool_filter_rewrites_injected_to_synonym():
    """有同义映射：伪 bash 改写成 shell 透传，原对象不被就地篡改。"""
    original = {"index": 0, "id": "c1", "type": "function",
                "function": {"name": "bash", "arguments": "{\"command\":"}}
    flt = _InjectedToolFilter(frozenset({"bash"}), {"bash": "shell"})
    kept = flt.keep([original])
    assert kept[0]["function"]["name"] == "shell"
    assert kept[0]["id"] == "c1"
    assert kept[0]["function"]["arguments"] == "{\"command\":"
    assert original["function"]["name"] == "bash"
    # 改写的调用不进丢弃索引：后续 arguments 分片自然保留
    assert flt.keep([{"index": 0, "function": {"arguments": "\"pwd\""}}])

def test_tool_filter_rewrite_keeps_mix_and_still_drops_unmapped():
    """混合批次：shell 映射保留，无映射的 read 仍被丢。"""
    flt = _InjectedToolFilter(frozenset({"bash", "read"}), {"bash": "shell"})
    kept = flt.keep([{"index": 0, "function": {"name": "bash"}},
                     {"index": 1, "function": {"name": "read"}},
                     {"index": 2, "function": {"name": "grep"}}])
    assert [c["function"]["name"] for c in kept] == ["shell", "grep"]
