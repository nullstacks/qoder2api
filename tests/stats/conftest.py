"""统计测试目录的共享夹具。"""

from __future__ import annotations

import pytest

from src.stats.collector import StatsCollector
from src.stats.query import StatsQuery


@pytest.fixture()
def stats(repo):
    """`(StatsCollector, StatsQuery)`：基于全局 `repo` 夹具的临时库。

    `test_collector.py` / `test_query.py` 曾各写一份逐字节等价的实现。
    """
    _credentials, db = repo
    yield StatsCollector(db), StatsQuery(db)
