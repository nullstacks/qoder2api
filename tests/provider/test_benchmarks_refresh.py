"""OpenRouter 目录：后台刷新单一任务、启动预热与应用装配。"""

from __future__ import annotations

import asyncio
import time

import pytest

from src import main
from src.benchmarks import load_catalog_snapshot, save_catalog
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from tests.helpers.constants import SECRET
from tests.provider._benchmarks_support import benchmarks_app as _benchmarks_app
from tests.provider._benchmarks_support import price_app as _price_app

pytestmark = pytest.mark.real_upstream("openrouter")

# ---------------------------------------------------- 后台刷新（单一任务三表）

async def test_refresh_openrouter_catalog_roundtrip(tmp_path, monkeypatch):
    """后台刷新一轮：拉 OpenRouter → 建三表 → 落盘 → 就地换入，回报条目数。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    with TestClient(app):
        pass
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        assert await runner._guarded(runner._openrouter_catalog(),
                                     "模型目录刷新（OpenRouter）",
                                     key="openrouter_catalog")
        run = runner.status.get("openrouter_catalog")
        assert run.ok is True and run.report == {
            "models": len(app.state.price_table)}
    assert app.state.price_table["glm-5.3"] == pytest.approx((1.0, 2.0, 1.0))
    assert app.state.model_benchmarks["glm-5.3"]["intelligence_index"] == 44.8
    assert app.state.price_saved_at is not None
    # 明细目录也换入了（与价表同一次抓取）
    assert app.state.models_dev_catalog["z-ai/glm-5.3"]["context"] is None
    # 落盘快照与回灌一致（下次启动零上游请求）
    assert load_catalog_snapshot(str(tmp_path))[1] == app.state.model_benchmarks

async def test_refresh_openrouter_catalog_rejects_empty(tmp_path, monkeypatch):
    """拉到空价表视为失败：不把已有的数据清空，只记运行态。"""
    from fastapi.testclient import TestClient

    app = _benchmarks_app(tmp_path, monkeypatch, table=None)
    app.state.price_table["glm-5.3"] = (1.0, 2.0, 1.0)
    app.state.model_benchmarks.update({"glm-5.3": {"source": "openrouter",
                                                   "intelligence_index": 1.0}})

    async def empty_fetch(_url):
        return {}

    monkeypatch.setattr(main, "fetch_openrouter_models", empty_fetch)
    with TestClient(app):
        pass
    async with app.router.lifespan_context(app):
        runner = app.state.task_runner
        ok = await runner._guarded(runner._openrouter_catalog(),
                                   "模型目录刷新（OpenRouter）",
                                   key="openrouter_catalog")
        assert ok is False
    assert app.state.price_table == {"glm-5.3": (1.0, 2.0, 1.0)}
    assert app.state.model_benchmarks == {"glm-5.3": {"source": "openrouter",
                                                      "intelligence_index": 1.0}}

async def test_startup_warm_model_catalog_only_when_missing(caplog):
    """启动预热：无落盘快照时才补拉一次，有快照直接跳过（不打上游）。"""
    calls: list[str] = []

    async def refresh():
        calls.append("ran")
        return {"models": 1}

    # 有表：直接返回，不拉
    await main._warm_model_catalog(refresh, {"m": (1.0, 2.0, 1.0)})
    assert calls == []

    # 无表：补拉一次
    await main._warm_model_catalog(refresh, {})
    assert calls == ["ran"]

    # 补拉失败只记日志，不抛
    async def failing():
        raise RuntimeError("boom")

    with caplog.at_level("WARNING"):
        await main._warm_model_catalog(failing, {})
    assert any("启动预热模型目录失败" in r.getMessage() for r in caplog.records)

def test_app_restores_catalog_snapshot_and_estimates_cost(tmp_path):
    """启动回灌落盘模型目录：新写入的明细立刻能估出成本（零上游请求）。"""
    from fastapi.testclient import TestClient

    save_catalog(str(tmp_path), {"glm-5.2": (1.0, 2.0, 0.1)}, {},
                 {"glm-5.2": {"id": "glm-5.2", "name": "GLM"}})
    app = _price_app(tmp_path)
    with TestClient(app):
        assert app.state.price_table == {"glm-5.2": (1.0, 2.0, 0.1)}
        assert app.state.models_dev_catalog == {
            "glm-5.2": {"id": "glm-5.2", "name": "GLM"}}
        app.state.stats_collector.record(
            username="u", provider="trae", model="glm-5.2", ok=True,
            input_tokens=1_000_000, output_tokens=0)
        row = app.state.stats_collector._db.connect().execute(
            "SELECT cost_usd FROM usage_events").fetchone()
    assert row["cost_usd"] == pytest.approx(1.0)

def test_app_restore_catalog_failure_is_logged(tmp_path, monkeypatch, caplog):
    """恢复快照抛异常只记日志，服务照常启动。"""
    from fastapi.testclient import TestClient

    def boom(_data_dir):
        raise RuntimeError("bad snapshot")

    monkeypatch.setattr(main, "load_catalog_snapshot", boom)
    app = _price_app(tmp_path)
    with caplog.at_level("WARNING"), TestClient(app):
        assert app.state.price_table == {}
        assert app.state.models_dev_catalog == {}
        assert app.state.price_saved_at is None
    assert any("恢复落盘模型目录失败" in r.getMessage() for r in caplog.records)

def test_openrouter_catalog_task_status_and_interval(tmp_path):
    from fastapi.testclient import TestClient

    app = _price_app(tmp_path)
    with TestClient(app):
        status = {item["key"]: item for item in app.state.task_runner.task_status()}
    assert status["openrouter_catalog"]["interval_seconds"] == 86400
    assert status["openrouter_catalog"]["enabled"] is True
    assert status["openrouter_catalog"]["last_ok"] is None

def test_build_runner_without_openrouter_catalog_keeps_task_hidden(tmp_path):
    """不注入刷新协程时不装配这条循环（老调用方/测试保持原行为）。"""
    from src.tasks.runner import build_runner

    db = Database(tmp_path / "c.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      OPENROUTER_CATALOG_MINUTES=1)
    runner = build_runner(credentials, {}, None, config)
    keys = {item["key"] for item in runner.task_status()}
    assert "openrouter_catalog" not in keys
    assert runner._openrouter_catalog_interval == 3600      # 下限 60 分钟
    db.close()

def test_shutdown_cancels_inflight_catalog_warmup(tmp_path, monkeypatch):
    """关机时模型目录预热若仍在飞，取消它，别把上游请求带出事件循环。"""
    from fastapi.testclient import TestClient

    started = asyncio.Event()
    cancelled = []

    async def hang(_url):
        started.set()
        try:
            await asyncio.Event().wait()        # 永不返回，直到被取消
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    monkeypatch.setattr(main, "fetch_openrouter_models", hang)
    app = _price_app(tmp_path)
    with TestClient(app):
        # 预热在后台线程的事件循环里跑，轮询等它进入抓取（避免时序竞态）
        deadline = time.monotonic() + 2
        while not started.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert started.is_set()                 # 预热已进入抓取
    assert cancelled == [True]                  # 关机时被取消
