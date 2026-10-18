"""管理台活跃上报入口。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.provider.codebuddy.activity import ActivityResult
from tests.helpers.constants import SECRET


def test_admin_activity_endpoint(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class _Provider:
        id = "codebuddy"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "")}

        def activity_scope(self, _data):
            return "s1"

        async def activity(self, _data):
            return ActivityResult(ok=True, message="", user_id="u1")

        async def list_models(self, _data):
            return []

        async def probe_quota(self, _data):  # pragma: no cover - 未被本用例调用
            raise NotImplementedError

    app = build_app(settings, providers={"codebuddy": _Provider()})
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        assert created.status_code == 200
        cid = created.json()["id"]
        ok = client.post(f"/api/credentials/{cid}/activity")
        assert ok.status_code == 200 and ok.json()["ok"] is True
        # 已落一条 growth_events
        listed = client.get(f"/api/credentials/{cid}/growth").json()["events"]
        assert listed and "活跃上报" in listed[0]["report"]

def test_admin_activity_endpoint_failure_records_nothing(tmp_path):
    """上报失败（ok=False）返回 200 但不落 growth_events（无事可汇报）。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class _Failing:
        id = "codebuddy"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "")}

        def activity_scope(self, _data):
            return "s1"

        async def activity(self, _data):
            return ActivityResult(ok=False, message="无法确定账号 userId")

        async def list_models(self, _data):
            return []

        async def probe_quota(self, _data):  # pragma: no cover - 未被本用例调用
            raise NotImplementedError

    app = build_app(settings, providers={"codebuddy": _Failing()})
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        cid = created.json()["id"]
        resp = client.post(f"/api/credentials/{cid}/activity")
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "message": "无法确定账号 userId"}
        assert client.get(f"/api/credentials/{cid}/growth").json()["events"] == []

def test_admin_activity_endpoint_rejects_unsupported(tmp_path):
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")

    class _NoActivity:
        id = "codebuddy"

        def import_credential(self, raw):
            return {"bearer_token": raw.get("token", "")}

        async def list_models(self, _data):
            return []

        async def probe_quota(self, _data):  # pragma: no cover - 未被本用例调用
            raise NotImplementedError

    app = build_app(settings, providers={"codebuddy": _NoActivity()})
    from src.auth.session import create_session_token

    with TestClient(app) as client:
        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        created = client.post("/api/credentials", json={
            "provider": "codebuddy", "credential": {"token": "abc"}})
        cid = created.json()["id"]
        bad = client.post(f"/api/credentials/{cid}/activity")
        assert bad.status_code == 400
        missing = client.post("/api/credentials/cred_nope/activity")
        assert missing.status_code == 400
