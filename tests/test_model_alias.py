"""自定义模型别名（`MODEL_ALIASES`）的宽容解析。

格式 `对外名=模型@渠道;对外名2=模型`：坏段忽略并记 warning，一条笔误不能
让整个网关起不来，也不能因此丢掉其它合法段。撞车 / 目标不存在属注入期错误
（要等模型目录到齐才判定），见 `tests/api/test_models_alias.py`。
"""

from __future__ import annotations

import logging

from src.model_alias import ModelAlias, parse_model_aliases


def test_empty_config_yields_no_aliases():
    assert parse_model_aliases("") == {}
    assert parse_model_aliases("   ") == {}
    # 只有分隔符的退化输入同样不产生条目
    assert parse_model_aliases("; ;") == {}


def test_parses_alias_without_provider():
    parsed = parse_model_aliases("team-fast=glm-5.2")
    assert parsed == {"team-fast": ModelAlias(name="team-fast", model="glm-5.2")}


def test_parses_alias_with_known_provider_and_trims_whitespace():
    parsed = parse_model_aliases("  fast = kimi-k3 @ TRAE ")
    assert parsed["fast"] == ModelAlias(name="fast", model="kimi-k3", provider="trae")


def test_multiple_segments_keep_configuration_order():
    parsed = parse_model_aliases("a=glm-5.2;b=kimi-k3@qoder;c=longcat-2.5-preview")
    assert list(parsed) == ["a", "b", "c"]
    assert parsed["b"].provider == "qoder"
    assert parsed["c"].provider is None


def test_duplicate_alias_name_last_wins():
    """重复对外名后者覆盖前者（与 `parse_fallback_groups` 的重复组名同口径）。"""
    parsed = parse_model_aliases("fast=glm-5.2;fast=kimi-k3")
    assert parsed == {"fast": ModelAlias(name="fast", model="kimi-k3")}


def test_unknown_provider_segment_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_model_aliases("bad=kimi-k3@nope;good=glm-5.2")
    assert list(parsed) == ["good"]
    assert any("未知" in record.getMessage() for record in caplog.records)


def test_provider_is_case_insensitive():
    assert parse_model_aliases("fast=kimi-k3@CodeBuddy")["fast"].provider == "codebuddy"


def test_empty_provider_after_at_is_skipped(caplog):
    """`model@`（渠道为空）是坏段——不能当成「不钉渠道」静默放行。"""
    with caplog.at_level(logging.WARNING):
        parsed = parse_model_aliases("bad=kimi-k3@;good=glm-5.2")
    assert list(parsed) == ["good"]
    assert any("未知" in record.getMessage() for record in caplog.records)


def test_missing_equals_segment_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_model_aliases("just-a-name;good=glm-5.2")
    assert list(parsed) == ["good"]
    assert any("缺少 `=`" in record.getMessage() for record in caplog.records)


def test_empty_alias_name_is_skipped(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_model_aliases("=glm-5.2;good=glm-5.2")
    assert list(parsed) == ["good"]
    assert any("对外名为空" in record.getMessage() for record in caplog.records)


def test_empty_target_is_skipped_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        parsed = parse_model_aliases("fast=   ;good=glm-5.2")
    assert list(parsed) == ["good"]
    assert any("目标模型为空" in record.getMessage() for record in caplog.records)


def test_empty_model_before_at_is_skipped(caplog):
    """`@trae`（模型为空）同样是坏段：没有可路由的目标。"""
    with caplog.at_level(logging.WARNING):
        parsed = parse_model_aliases("bad=@trae;good=glm-5.2")
    assert list(parsed) == ["good"]
    assert any("未知" in record.getMessage() for record in caplog.records)


def test_model_name_may_contain_at_when_provider_is_known():
    """`rpartition("@")` 语义：最后一个 `@` 才是渠道分隔符。"""
    parsed = parse_model_aliases("odd=weird@name@trae")
    assert parsed["odd"] == ModelAlias(name="odd", model="weird@name", provider="trae")
