"""后台任务测试共享替身与装配助手。

`ProbeProvider`(3) 曾在该目录的多个测试文件里各写一份逐字节等价的实现；
`admin_app`(2) 是 conftest 全局 `admin_client` 夹具的等价副本，一并收敛。
"""

from __future__ import annotations

from src.provider.base import Quota
from src.provider.codebuddy.checkin import CheckinResult
from src.provider.codebuddy.client import CodeBuddyCredential

__all__ = ["ProbeProvider"]


class ProbeProvider:
    """可控的 CodeBuddy provider：额度探测、签到与刷新均可注入结果。"""

    id = "codebuddy"

    def __init__(self, quota=None, error=None, checkin_ok=True, refresh_new=None):
        self.quota = quota or Quota(remaining=8, total=10, probed_at=1)
        self.error = error
        self.checkin_ok = checkin_ok
        self.refresh_new = refresh_new
        self.checkin_calls = 0
        self.refresh_calls = 0
        self.seen_scopes: list[str] = []

    async def probe_quota(self, _data):
        if self.error is not None:
            raise self.error
        return self.quota

    async def checkin(self, _data):
        self.checkin_calls += 1
        return CheckinResult(ok=self.checkin_ok, credit=10 if self.checkin_ok else None)

    def checkin_scope(self, data):
        scope = f"acct:{data.get('account_uid', '')}"
        self.seen_scopes.append(scope)
        return scope

    def credential_from(self, data):
        return CodeBuddyCredential.from_dict(data)

    async def refresh(self, data):
        self.refresh_calls += 1
        return {**data, "bearer_token": self.refresh_new or "NEW"}
