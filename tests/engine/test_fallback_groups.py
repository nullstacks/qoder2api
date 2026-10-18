"""兼容组解析与 fallback 链顺序（纯函数）。"""

from __future__ import annotations

from src.engine.model_resolver import ordered_fallback_chain, parse_fallback_groups


def test_parse_fallback_groups_basic():
    groups = parse_fallback_groups("fast=glm-4.6,glm-5;strong=kimi-k3")
    assert groups == {"fast": ("glm-4.6", "glm-5"), "strong": ("kimi-k3",)}

def test_parse_fallback_groups_ignores_bad_segments():
    """空段 / 缺 `=` / 空组名 / 空成员一律跳过，不影响合法组。"""
    groups = parse_fallback_groups(";  ;broken;=nope;empty=;ok=a,b")
    assert groups == {"ok": ("a", "b")}

def test_parse_fallback_groups_empty():
    assert parse_fallback_groups("") == {}
    assert parse_fallback_groups("   ") == {}

def test_parse_fallback_groups_duplicate_name_last_wins():
    groups = parse_fallback_groups("g=a;g=b,c")
    assert groups == {"g": ("b", "c")}

def test_ordered_chain_group_name_entry():
    """请求组名：组名不进链，按配置顺序返回成员。"""
    groups = {"fast": ("glm-4.6", "glm-5")}
    assert ordered_fallback_chain("fast", groups) == ("glm-4.6", "glm-5")

def test_ordered_chain_member_entry_puts_request_first():
    """请求成员：该成员置首，组内其余成员保持配置顺序。"""
    groups = {"fast": ("glm-4.6", "glm-5", "glm-4")}
    assert ordered_fallback_chain("glm-5", groups) == ("glm-5", "glm-4.6", "glm-4")

def test_ordered_chain_case_insensitive():
    groups = {"Fast": ("GLM-4.6", "GLM-5")}
    assert ordered_fallback_chain("FAST", groups) == ("GLM-4.6", "GLM-5")
    assert ordered_fallback_chain("glm-5", groups) == ("glm-5", "GLM-4.6")

def test_ordered_chain_miss():
    assert ordered_fallback_chain("other", {"fast": ("a",)}) == ()
