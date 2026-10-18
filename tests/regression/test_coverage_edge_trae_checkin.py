"""覆盖率收尾（TRAE 签到/生命周期/建议回调）。"""

from __future__ import annotations

import httpx
import pytest

from src.provider.trae.client import TraeCredential
from tests.helpers.constants import SECRET
from tests.helpers.paths import tmpdir as _tmpdir
from tests.provider.trae._support import client as _trae_client


async def test_trae_checkin_status_and_claim_use_ug_headers():
    """签到两端点必须走 ug_headers（含 X-User-Region），且用 POST。"""
    seen: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.headers)))
        if request.url.path.endswith("checkin_credits/status"):
            return httpx.Response(200, json={"checked_in": False, "credits": 0,
                                             "enable": True})
        return httpx.Response(200, json={"credits": 200})

    client = _trae_client(handler)
    credential = TraeCredential(access_token="a", device_id="d", machine_id="m", uid="u")
    status = await client.fetch_checkin_status(credential)
    assert status == {"checked_in": False, "credits": 0, "enable": True}

    claim = await client.claim_checkin(credential)
    assert claim == {"credits": 200}

    assert len(seen) == 2
    for path, headers in seen:
        assert "checkin_credits" in path
        # httpx 内部存储全小写
        assert headers.get("x-user-region") == "CN"
        assert headers.get("authorization") == "Cloud-IDE-JWT a"
        # 设备头是隐藏必填项；每次请求生成新的 16 位数字串
        assert headers.get("x-device-id", "").isdigit()
        assert len(headers.get("x-device-id", "")) == 16
        assert headers.get("x-machine-id") == "m"
        assert headers.get("x-uid") == "u"

async def test_trae_provider_checkin_already_is_success():
    """已签到（status.checked_in=true）→ ok=true + already_checked_in。"""
    from src.provider.trae.client import TraeProvider

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"checked_in": True, "credits": 0, "enable": True})

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is True and result.already_checked_in is True
    assert result.message == "今天已签到"

async def test_trae_provider_checkin_disabled_reports_not_ok():
    """status.enable=false → 不可签到。"""
    from src.provider.trae.client import TraeProvider

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"checked_in": False, "credits": 0, "enable": False})

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is False

async def test_trae_provider_checkin_claims_when_eligible():
    """未签且可签 → claim → 回查确认积分增加才算成功。"""
    paths: list[str] = []
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        paths.append(request.url.path)
        if request.url.path.endswith("checkin_credits/status"):
            status_calls += 1
            # 领取前未签、余额 150；claim 之后回查：已签且余额 +200
            return httpx.Response(200, json={
                "checked_in": status_calls > 1, "enable": True,
                "credits": 350 if status_calls > 1 else 150})
        return httpx.Response(200, json={"code": 0})

    from src.provider.trae.client import TraeProvider

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is True and result.already_checked_in is False
    assert result.credit == 350 and "200" in result.message
    assert any(p.endswith("checkin_credits/claim") for p in paths)
    # 必须回查确认；不能只凭 claim 的 code:0——已签账号的 claim 也返回 0
    assert status_calls == 2

async def test_trae_checkin_claim_ok_without_credit_delta_is_failure():
    """claim 说成功但回查没涨积分 → 报失败（不把幂等 code:0 当成功）。"""
    status_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_calls
        if request.url.path.endswith("checkin_credits/status"):
            status_calls += 1
            # 前后都是未签、余额不变（服务端什么都没做）
            return httpx.Response(200, json={"checked_in": False, "enable": True,
                                             "credits": 150})
        return httpx.Response(200, json={"code": 0})

    from src.provider.trae.client import TraeProvider

    provider = TraeProvider(client=_trae_client(handler))
    result = await provider.checkin({"accessToken": "a"})
    assert result.ok is False
    assert "未确认积分到账" in result.message

async def test_trae_checkin_credit_delta_edge_cases():
    """增量计算：字段缺失/布尔/非有限值一律返回 None（按未确认处理）。"""
    from src.provider.trae.client import _credit_delta

    assert _credit_delta(100, 300) == 200.0
    assert _credit_delta(100, 100) == 0.0
    assert _credit_delta(None, 100) is None
    assert _credit_delta(100, None) is None
    assert _credit_delta(True, 100) is None
    assert _credit_delta(100, False) is None
    assert _credit_delta(float("inf"), 100) is None
    assert _credit_delta("100", 200) is None

def test_trae_checkin_scope_uses_uid():
    from src.provider.trae.client import TraeProvider

    provider = TraeProvider()
    assert provider.checkin_scope({"uid": "u1"}) == "trae|u1"
    assert provider.checkin_scope({}) == "trae|"

async def test_checkin_task_skips_provider_without_scope(tmp_path):
    """provider 没有 checkin_scope 时跳过（background 148-150）。"""
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository

    db = Database(tmp_path / "ns.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})

    from src.tasks.checkin import CheckinTask

    class NoScope:
        id = "codebuddy"

    task = CheckinTask(credentials, {"codebuddy": NoScope()})
    report = await task.run_once()
    assert report.skipped == 1
    db.close()

def test_lifespan_warmup_failure_is_logged_not_raised(tmp_path, caplog):
    """启动预热模型列表失败 → 仅记日志，服务正常起（main 74-75）。"""
    import logging

    from fastapi.testclient import TestClient

    from src.config import Settings
    from src.main import build_app

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path))

    class Exploding:
        id = "boom"

        async def list_models(self, _data):
            raise RuntimeError("upstream down")

        def import_credential(self, raw):  # pragma: no cover
            return raw

    app = build_app(settings, providers={"boom": Exploding()})
    # candidates 在单上游 try 之外，让它抛错以触发 lifespan 兜底 except
    app.state.credentials.candidates = (
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("db gone")))
    with caplog.at_level(logging.WARNING), TestClient(app):
        pass
    assert any("预热模型列表失败" in r.getMessage() for r in caplog.records)

def test_similar_models_suggests_available_alternatives():
    """400 建议列表：相近模型优先，前缀兜底，排除自身。"""
    from src.main import _similar_models

    aliases = {"trae": {"qwen-3.7-plus": "qwen-3.7-plus", "glm-5.2": "GLM-5.2"},
               "codebuddy": {"glm-5.2": "glm-5.2", "kimi-k2.6": "kimi-k2.6"}}
    # 前缀兜底：不存在 qwen3.8-max，但存在 qwen 开头的
    assert _similar_models("qwen3.8-max", aliases) == ["qwen-3.7-plus"]
    # 完全不相关 → 空列表
    assert _similar_models("xyzzy", aliases) == []
    # 自身被排除
    assert _similar_models("GLM-5.2", aliases) == []

async def test_suggestion_callback_failure_does_not_break_400():
    """建议回调抛错 → 主错误照常（executor 74-75）。"""
    from src.compat.openai.request import InvalidRequest
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Scheduler
    from src.provider.codebuddy.client import CodeBuddyProvider
    from tests.api.test_chat import _request
    from tests.provider.codebuddy.test_client import _client

    db = Database(str(_tmpdir() / "s.sqlite3"))
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    def handler(_request):
        return httpx.Response(400, json={"code": 1002, "msg": "no"})

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": CodeBuddyProvider(client=_client(handler))},
        credentials=repo, scheduler=Scheduler(), default_model="qwen3.8",
        model_suggestions=lambda _name: (_ for _ in ()).throw(RuntimeError("boom"))))

    with pytest.raises(InvalidRequest):
        await executor.complete(_request("qwen3.8"), username="u")
    db.close()
