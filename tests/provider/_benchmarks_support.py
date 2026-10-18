"""OpenRouter 目录测试共享样本与装配助手。

`_OR_PAYLOAD`(4) 与 `_benchmarks_app`/`_PriceStub`/`_price_app`(2) 曾在该目录
的多个测试文件里各写一份逐字节等价的实现，收敛到这里。
"""

from __future__ import annotations

import json
from typing import Any

from src import main
from src.benchmarks import BenchmarkTable
from src.config import Settings
from tests.helpers.constants import SECRET

__all__ = ["OR_PAYLOAD", "PriceStub", "benchmarks_app", "price_app"]

OR_PAYLOAD = {
    "data": [
        {"id": "z-ai/glm-5.3", "name": "Z.ai: GLM 5.3",
         "pricing": {"prompt": "0.000001", "completion": "0.000002"},
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": 44.8, "coding_index": 74.8,
             "agentic_index": 53.1}}},
        {"id": "moonshotai/kimi-k3", "name": "MoonshotAI: Kimi K3",
         "pricing": {"prompt": "0.0000005", "completion": "0.000001"},
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": 43.6, "coding_index": 76.2,
             "agentic_index": None}}},
        {"id": "xai/grok-no-score", "name": "Grok",
         "pricing": {"prompt": "0.000002"},
         "benchmarks": {"artificial_analysis": {
             "intelligence_index": None, "coding_index": None,
             "agentic_index": None}}},
        {"id": "broken/no-benchmarks", "name": "Broken"},
    ],
}


class PriceStub:
    """最小 provider：只满足启动预热对 list_models 的调用。"""

    id = "kilo"

    async def list_models(self, _data):
        return []


def benchmarks_app(tmp_path, monkeypatch, *, table: BenchmarkTable | None,
                   raw: Any = OR_PAYLOAD):
    """带真实装配的 app，并直接把能力分表塞进 app.state（不打上游）。

    `raw` 是 OpenRouter 抓取的替身返回：默认给带两个分数的正常表，传 `` 模拟
    「上游不可用」（此时启动预热与后台刷新都拿不到分数，表保持空）。
    """
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    async def fake_fetch(_url):
        return json.loads(json.dumps(raw))

    monkeypatch.setattr(main, "fetch_openrouter_models", fake_fetch)
    app = build_app(settings, providers={})
    # 就地换值而不是重新绑定：`build_app` 里的刷新协程持有的是**同一个 dict
    # 引用**，重新绑定会让它写到旧对象上（与生产「换引用会让路由读到旧表」
    # 同一个坑）。
    app.state.model_benchmarks.clear()
    app.state.model_benchmarks.update(table or {})
    return app


def price_app(tmp_path, **env):
    """带最小 provider 装配的 app（用于汇率/周期热更用例）。"""
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root", **env)
    return build_app(settings, providers={"kilo": PriceStub()})
