"""运维告警：runner 接线、任务注册与保留任务联动。"""

from __future__ import annotations

import time

from src.config import Settings
from src.db.conn import Database
from src.db.migrate import apply_schema
from src.db.repo import AlertRepository
from src.stats.collector import StatsCollector
from src.tasks.retention import RetentionTask
from src.tasks.runner import TaskRunner, build_runner
from src.tasks.status import TaskStatusStore
from tests.helpers.constants import SECRET
from tests.tasks._alerting_support import StubCredentials as _StubCredentials
from tests.tasks._alerting_support import make_alert_task as _task


class _StubProvider:
    id = "codebuddy"

    async def probe_quota(self, _data):
        from src.provider.base import Quota
        return Quota(remaining=5, total=10, probed_at=1)

    async def checkin(self, _data):  # pragma: no cover - 不在本组触发
        raise AssertionError("不应触发签到")

    def checkin_scope(self, _data):  # pragma: no cover
        return "scope"

    def credential_from(self, data):  # pragma: no cover
        from src.provider.codebuddy.credential import CodeBuddyCredential
        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):  # pragma: no cover
        return data

def _runner_with_alert(repo_tuple, task):
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask

    credentials, db = repo_tuple
    provider = _StubProvider()
    return TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {"codebuddy": provider}, None),
        checkin=CheckinTask(credentials, {"codebuddy": provider}),
        refresh=RefreshTask(credentials, {"codebuddy": provider}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
        alert=task,
        alert_interval_minutes=1,
    )

def test_schema_v17_adds_alert_events_to_legacy_db(tmp_path):
    """老库升级：只有旧表的库在 apply_schema 后得到 alert_events，且可读写。"""
    import sqlite3

    from src.db.migrate import SCHEMA_VERSION

    db = Database(tmp_path / "legacy.sqlite3")
    conn = sqlite3.connect(db.path)
    # 旧库：仅一张与告警无关的表 + 老 user_version
    conn.execute("CREATE TABLE api_keys (id TEXT PRIMARY KEY, username TEXT NOT NULL)")
    conn.execute("PRAGMA user_version = 16")
    conn.commit()
    conn.close()

    apply_schema(db.connect())
    tables = {row[0] for row in db.connect().execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "alert_events" in tables
    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # 新表可写可读，且旧表数据未被破坏
    repo = AlertRepository(db)
    repo.record(rule="pool_empty", severity="critical", scope="pool",
                message="池空", now=5)
    assert repo.recent()[0]["rule"] == "pool_empty"
    db.close()

async def test_runner_sync_alert_records_run(repo, tmp_path):
    credentials, db = repo
    task, _alerts, adb = _task(
        tmp_path, credentials=_StubCredentials(counts={"total": 1, "ready": 0}),
        now=lambda: 1000)
    runner = _runner_with_alert(repo, task)
    # 经 _guarded 记入运行态（真实执行，非 no-op）
    assert await runner._guarded(runner._sync_alert(), "运维告警", key="alert") is True
    assert runner.status.get("alert") is not None
    assert runner.status.runs("alert") == 1
    assert runner.status.get("alert").report["fired"] == 1
    adb.close()
    _ = credentials, db

def test_runner_alert_interval_clamps():
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask

    runner = TaskRunner(
        quota_probe=QuotaProbeTask(None, {}, None),
        checkin=CheckinTask(None, {}),
        refresh=RefreshTask(None, {}, skew_seconds=3600),
        retention=RetentionTask(None),
        alert_interval_minutes=0,
    )
    assert runner._alert_interval == 60
    assert runner._interval_seconds("alert") == 60

def test_runner_task_status_includes_alert(repo):
    credentials, db = repo
    task, _alerts, adb = _task(db and credentials and __import__("pathlib").Path(db.path).parent)
    runner = _runner_with_alert(repo, task)
    by_key = {item["key"]: item for item in runner.task_status()}
    assert by_key["alert"]["name"] == "运维告警"
    assert by_key["alert"]["enabled"] is True
    adb.close()

def test_runner_task_status_skips_alert_when_absent(repo):
    credentials, db = repo
    from src.tasks.checkin import CheckinTask
    from src.tasks.quota_probe import QuotaProbeTask
    from src.tasks.refresh import RefreshTask

    runner = TaskRunner(
        quota_probe=QuotaProbeTask(credentials, {}, None),
        checkin=CheckinTask(credentials, {}),
        refresh=RefreshTask(credentials, {}, skew_seconds=3600),
        retention=RetentionTask(StatsCollector(db)),
    )
    assert all(item["key"] != "alert" for item in runner.task_status())
    _ = credentials

def test_build_runner_wires_alert(repo, tmp_path):
    """生产装配传 alerts 时建告警对象并复用共享运行态。"""
    credentials, db = repo
    alerts = AlertRepository(db)
    store = TaskStatusStore()
    config = Settings(_env_file=None, APP_SECRET=SECRET)
    runner = build_runner(credentials, {"codebuddy": _StubProvider()},
                          StatsCollector(db), config, alerts=alerts, status=store)
    assert runner._alert is not None
    assert runner._alert._task_status is store
    assert runner.status is store
    assert runner._interval_seconds("alert") == 300

def test_build_runner_alert_disabled_switch(repo):
    credentials, db = repo
    alerts = AlertRepository(db)
    config = Settings(_env_file=None, APP_SECRET=SECRET, ALERT_ENABLED=False)
    runner = build_runner(credentials, {"codebuddy": _StubProvider()},
                          StatsCollector(db), config, alerts=alerts)
    assert runner._task_enabled("alert") is False

# --------------------------------------------------------- retention


def test_retention_prunes_alerts(repo):
    _credentials, db = repo
    alerts = AlertRepository(db)
    alerts.record(rule="pool_empty", severity="critical", scope="pool",
                  message="x", now=0)
    alerts.record(rule="pool_empty", severity="critical", scope="pool",
                  message="x", now=int(time.time()))
    report = RetentionTask(StatsCollector(db), retention_days=90,
                           alerts=alerts).run_once()
    assert report["purged_alerts"] == 1
    assert len(alerts.recent()) == 1

def test_retention_without_alerts_reports_zero(repo):
    _credentials, db = repo
    report = RetentionTask(StatsCollector(db)).run_once()
    assert report["purged_alerts"] == 0

def test_retention_prunes_growth_events(repo):
    """成长中心运行记录按同一保留期清理：不回收会无界增长。"""
    from src.db.repo import GrowthRepository
    from src.provider.base import GrowthResult

    _credentials, db = repo
    growth = GrowthRepository(db)
    growth.record(credential_id="cred_1", now=0,
                  result=GrowthResult(report="旧", ok=True))
    growth.record(credential_id="cred_1", now=int(time.time()),
                  result=GrowthResult(report="新", ok=True))
    report = RetentionTask(StatsCollector(db), retention_days=90,
                           growth_events=growth).run_once()
    assert report["purged_growth_events"] == 1
    assert [e["report"] for e in growth.recent("cred_1")] == ["新"]
