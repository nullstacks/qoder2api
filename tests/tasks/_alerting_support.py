"""告警测试共享替身与装配助手。

`test_alerting_task.py` / `test_alerting_runner.py` 原先各自复制了一份凭证/统计
替身与 `_task` 装配（逐字节等价）；这里收敛成一份。
"""

from __future__ import annotations

from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import AlertRepository
from src.tasks.alerting import AlertTask
from src.tasks.status import TaskStatusStore

__all__ = ["StubCredentials", "StubStats", "make_alert_task"]


class StubCredentials:
    """凭证仓库替身：池计数与到期令牌均可注入。"""

    def __init__(self, counts=None, expiring=None):
        self._counts = counts or {"total": 1, "ready": 1, "cooling": 0,
                                  "paused": 0, "disabled": 0}
        self._expiring = expiring or []

    def pool_counts(self, now=None):
        return self._counts

    def expiring_tokens(self, *, within_seconds, now=None):
        return self._expiring


class StubStats:
    """统计收集器替身：`window_error_rate` 返回注入的 (requests, failures)。"""

    def __init__(self, counts=(0, 0)):
        self._counts = counts

    def window_error_rate(self, *, since):
        return self._counts


def make_alert_task(tmp_path, *, credentials=None, stats=None, status=None, **kwargs):
    db = Database(tmp_path / "alert.sqlite3")
    apply_schema(db.connect())
    alerts = AlertRepository(db)
    task = AlertTask(
        credentials or StubCredentials(),
        alerts,
        stats or StubStats(),
        task_status=status or TaskStatusStore(),
        **kwargs,
    )
    return task, alerts, db
