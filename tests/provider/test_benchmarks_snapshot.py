"""OpenRouter 目录：三表快照落盘/读回与成本估算接价表。"""

from __future__ import annotations

import json
import time

import pytest

from src.benchmarks import (
    CATALOG_FILENAME,
    CATALOG_VERSION,
    MAX_AGE_SECONDS,
    build_benchmark_table,
    build_model_catalog,
    build_price_table,
    load_catalog_snapshot,
    save_catalog,
)
from src.pricing import estimate_cost_usd
from tests.provider._benchmarks_support import OR_PAYLOAD as _OR_PAYLOAD

pytestmark = pytest.mark.real_upstream("openrouter")

# --------------------------------------------------------- 落盘快照（三表一体）

def test_save_and_load_catalog_roundtrip(tmp_path):
    """落盘 → 读回：三张表内容一致，且共用同一个保存时刻。"""
    prices = {"glm-5.3": (1.0, 2.0, 0.5)}
    benchmarks = build_benchmark_table(_OR_PAYLOAD)
    catalog = build_model_catalog({"data": [
        {"id": "z-ai/glm-5.3", "name": "GLM", "pricing": {"prompt": 1}}]})
    before = time.time()
    save_catalog(str(tmp_path), prices, benchmarks, catalog)
    loaded_prices, loaded_bench, loaded_cat, saved_at = load_catalog_snapshot(
        str(tmp_path))
    assert loaded_prices == prices
    assert loaded_bench == benchmarks
    assert loaded_cat == catalog
    assert saved_at is not None and before <= saved_at <= time.time()
    assert load_catalog_snapshot(str(tmp_path))[1] == benchmarks

def test_catalog_path_filename(tmp_path):
    from src.benchmarks import catalog_path

    assert catalog_path(str(tmp_path)).endswith(CATALOG_FILENAME)

def test_load_catalog_missing_file_is_empty(tmp_path):
    assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert load_catalog_snapshot(str(tmp_path))[1] == {}

def test_load_catalog_version_mismatch_is_empty(tmp_path, caplog):
    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    payload = json.loads((tmp_path / CATALOG_FILENAME).read_text(encoding="utf-8"))
    payload["version"] = CATALOG_VERSION + 1
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert any("模型目录版本不匹配" in r.getMessage() for r in caplog.records)

def test_load_catalog_expired_is_empty(tmp_path):
    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {})
    assert load_catalog_snapshot(str(tmp_path),
                                 now=time.time() + MAX_AGE_SECONDS + 10) == ({}, {}, {}, None)

def test_load_catalog_broken_json_is_empty(tmp_path, caplog):
    (tmp_path / CATALOG_FILENAME).write_text("{not json", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)

def test_load_catalog_saved_at_missing_is_error(tmp_path, caplog):
    payload = {"version": CATALOG_VERSION, "prices": {}, "benchmarks": {},
               "models": {}}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, None)
    assert any("模型目录读取失败" in r.getMessage() for r in caplog.records)

def test_load_catalog_skips_bad_prices(tmp_path):
    """坏价格行跳过而不拖垮整表：非 list / 长度不对 / 含非数值 / 负值 / 布尔。"""
    payload = {
        "version": CATALOG_VERSION, "saved_at": time.time(),
        "prices": {
            "good": [1, 2, 3],
            "notlist": "x",
            "wronglen": [1, 2],
            "hasnull": [1, None, 3],
            "neg": [1, -2, 3],
            "notanumber": [1, "x", 3],
            "boolval": [1, True, 3],
            "": [1, 2, 3],
        },
        "benchmarks": {}, "models": {},
    }
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path))[0] == {"good": (1.0, 2.0, 3.0)}

def test_load_catalog_skips_bad_benchmarks(tmp_path):
    """缺指数字段的记录 / 非 dict 值 / 空键一律不收。"""
    payload = {
        "version": CATALOG_VERSION, "saved_at": time.time(), "prices": {},
        "models": {},
        "benchmarks": {"good": {"source": "openrouter", "intelligence_index": 1.0},
                       "bad": {"source": "openrouter"},      # 缺指数字段
                       "worse": "not-a-dict",
                       "": {"intelligence_index": 2.0}},
    }
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path))[1] == {
        "good": {"source": "openrouter", "intelligence_index": 1.0}}

def test_load_catalog_skips_bad_models(tmp_path):
    """坏条目（非 dict）跳过而不拖垮整份目录。"""
    payload = {"version": CATALOG_VERSION, "saved_at": time.time(),
               "prices": {}, "benchmarks": {},
               "models": {"good": {"id": "good"}, "bad": "x", "": {"id": ""}}}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path))[2] == {"good": {"id": "good"}}

def test_load_catalog_non_dict_sections_are_empty(tmp_path):
    """三块整体类型不对（非 dict）时各自退空，不抛。"""
    payload = {"version": CATALOG_VERSION, "saved_at": time.time(),
               "prices": [], "benchmarks": "x", "models": 5}
    (tmp_path / CATALOG_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    assert load_catalog_snapshot(str(tmp_path)) == ({}, {}, {}, payload["saved_at"])

def test_save_catalog_failure_is_logged(tmp_path, monkeypatch, caplog):
    """落盘失败只记日志，不抛（绝不影响模型列表）。"""
    import logging
    import os

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    save_catalog(str(tmp_path), {"m": (1.0, 2.0, 0.5)}, {}, {"m": {"id": "m"}})
    assert any("模型目录落盘失败" in r.getMessage()
               for r in caplog.records if r.levelno >= logging.WARNING)

# ------------------------------------------------------- 成本估算接价表

def test_estimate_cost_matches_across_openrouter_prefix():
    """价表由 OpenRouter 构建：`z-ai/glm-5.3` 能对上本项目键 `glm-5.3`。"""
    table = build_price_table({"data": [{
        "id": "z-ai/glm-5.3", "pricing": {"prompt": "0.000001",
                                          "completion": "0.000002"}}]})
    cost = estimate_cost_usd(table, "glm-5.3", input_tokens=1_000_000,
                             output_tokens=0, cached_tokens=0)
    assert cost == pytest.approx(1.0)
