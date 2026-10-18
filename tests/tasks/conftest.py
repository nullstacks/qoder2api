"""tasks 包测试夹具。

告警任务在投递前会做一次 DNS 复核（`blocked_webhook_host`，fail closed），
而用例里的 webhook 主机名（`hook.example`）是保留 TLD、解析必然失败——若不
隔离，每个投递用例都会静默变成「被阻断」。这里把该复核桩成放行，让告警用例
只断言投递/脱敏语义；复核本身的真值与分支由
`tests/tasks/test_alerting_admin.py::test_blocked_webhook_host_*` 覆盖。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _allow_webhook_hosts(monkeypatch):
    """默认放行 webhook 主机；需要验证阻断路径的用例自行覆盖该桩。"""
    monkeypatch.setattr("src.tasks.alerting.blocked_webhook_host",
                        lambda url: False)
