"""聊天出口流式细节：指纹/角色归一、超时与事件上限、上游业务码冷却。"""

from __future__ import annotations

import json
import logging
import time

import httpx
import pytest

from src.engine.executor import Executor, ExecutorDeps, NoHealthyCredential
from src.engine.scheduler import Scheduler
from src.provider.base import ErrKind, Event, EventKind
from src.provider.codebuddy.client import (
    CodeBuddyCredential,
    CodeBuddyProvider,
    UpstreamHTTPError,
)
from src.provider.qoder.credential import QoderCredential
from src.tasks.pacer import stable_key
from tests.helpers.chat import chat_request as _request
from tests.helpers.events import GOOD
from tests.helpers.executor import DualProvider
from tests.helpers.executor import dual_executor as _dual_executor
from tests.provider.codebuddy._support import client as _client
from tests.provider.codebuddy._support import fixture

_CB_11128_BODY = (
    b'{"code":11128,"msg":"Illegal API invocation from an unapproved channel"}')

def _model_cooldown_rows(db):
    return [tuple(row) for row in db.connect().execute(
        "SELECT credential_id, model, hits, reason FROM credential_model_cooldowns"
    ).fetchall()]

def cred(**overrides) -> QoderCredential:
    base = {"access_token": "dt-token-1", "uid": "u-1", "realm": "cn",
            "refresh_token": "drt-1", "auth_source": "oauth",
            "nickname": "Tester", "expires_at": int(time.time()) + 86400}
    base.update(overrides)
    return QoderCredential(**base)

async def test_chat_pacer_throttles_and_can_be_disabled():
    """CB 聊天节流：请求前等待；None/关闭时不等待。"""
    import time as _time

    from src.provider.codebuddy.client import CodeBuddyProvider

    waited = []

    class FakePacer:
        async def wait_turn(self, key=None):
            waited.append((key, _time.monotonic()))

        def release(self, key=None):
            pass

    async def stream_ok(cred, payload, model):
        yield GOOD[0]

    class FakeClient:
        async def stream_chat(self, cred, payload, model):
            async for ev in stream_ok(cred, payload, model):
                yield ev

    provider = CodeBuddyProvider(client=FakeClient(), pacer=FakePacer())
    events = [e async for e in provider.stream_chat(
        {"bearer_token": "t", "account_uid": "acct-1"}, {}, "m")]
    assert events and waited
    # 桶键按凭证身份派生，而不是空键
    assert waited[0][0] == stable_key("codebuddy", "acct-1")

    provider2 = CodeBuddyProvider(client=FakeClient(), pacer=None)
    waited.clear()
    _ = [e async for e in provider2.stream_chat({"bearer_token": "t"}, {}, "m")]
    assert not waited

async def test_stream_inner_error_other_is_logged_and_counted(dual_repo, caplog):
    """流内非 4001/1005 错误 → 记冷却并打日志（可观测性回归）。"""
    import logging

    from src.provider.base import Event

    repo, _db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    calls = {"n": 0}

    async def gen(_cred, _payload, _model):
        calls["n"] += 1
        yield Event(kind=EventKind.ERROR, error_code=41291, error_message="rate limited")

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="m"))
    frames = [f async for f in executor.stream(_request("m"), username="u")]
    assert any(b"error" in f for f in frames)
    assert calls["n"] == 1
    assert any("流内错误" in r.getMessage() for r in caplog.records
               if r.levelno == logging.WARNING)

async def test_stream_inner_4001_yields_invalid_frame(dual_repo, caplog):
    """流内 4001 → INVALID：跳过上游 + invalid_request 帧。"""
    import json as _json
    import logging

    from src.provider.base import Event

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    calls = {"n": 0}

    async def gen(_cred, _payload, _model):
        calls["n"] += 1
        yield Event(kind=EventKind.ERROR, error_code=4001, error_message="param invalid",
                    error_kind=ErrKind.INVALID)

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="m",
        model_suggestions=lambda _n: ["alt-model"]))

    with caplog.at_level(logging.WARNING):
        frames = [f async for f in executor.stream(_request("m"), username="u")]
    payload = _json.loads(frames[0].split(b"\n\n")[0].removeprefix(b"data: "))
    assert payload["error"]["code"] == "invalid_request"
    assert "alt-model" in payload["error"]["message"]
    assert calls["n"] == 1                              # 不轮换
    row = db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials").fetchone()
    assert tuple(row) == (0, None)                      # 不冷却
    assert any("流内拒绝模型" in r.getMessage() for r in caplog.records)

async def test_stream_chat_body_carries_cli_signature_fields():
    """官方 CLI 特征字段：enable_thinking / reasoning_effort / include_usage。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [{"role": "user", "content": "hi"}],
               "stream_options": {"include_usage": False}}
    events = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    body = captured["body"]
    assert body["enable_thinking"] is True          # 缺失触发 11128 渠道风控
    assert body["reasoning_effort"] == "medium"     # 缺失则思考被写进正文
    assert body["stream_options"]["include_usage"] is True
    assert body["stream"] is True
    assert body["model"] == "m"
    assert events[-1].kind is EventKind.FINISH

async def test_stream_chat_keeps_client_reasoning_effort():
    """客户端显式给的 effort 不被覆盖：补缺省不改写客户端意图。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [{"role": "user", "content": "hi"}],
               "reasoning_effort": "low"}
    [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    assert captured["body"]["reasoning_effort"] == "low"

async def test_trae_4001_falls_through_to_codebuddy(dual_repo, caplog):
    """TRAE 流内 4001 → 跳过 TRAE → CB 成功接住；TRAE 凭证不被冷却。"""

    from src.provider.base import Event

    class Trae4001:
        id = "trae"
        calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            yield Event(kind=EventKind.ERROR, error_code=4001, error_message="param",
                        error_kind=ErrKind.INVALID)

    class CbOk:
        id = "codebuddy"
        calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})
    repo.add(provider="trae", credential_data={"accessToken": "tr"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE provider = 'trae'")
    trae, cb = Trae4001(), CbOk()
    executor = Executor(ExecutorDeps(
        providers={"trae": trae, "codebuddy": cb}, credentials=repo,
        scheduler=Scheduler(), default_model="m"))

    result = await executor.complete(_request("m"), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert trae.calls == 1 and cb.calls == 1
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count, cooling_until FROM credentials ORDER BY provider")]
    assert rows == [(0, None), (0, None)]       # TRAE 4001 不冷却

async def test_complete_timeout_rotates_to_next_credential(dual_repo):
    """非流式聚合超时：按瞬态错误（SOFT）换号重试，请求不悬挂。"""
    import asyncio

    class SlowFirst:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls > 1:
                yield Event(kind=EventKind.CONTENT, content="ok")
                yield Event(kind=EventKind.FINISH, finish_reason="stop")
                return
            yield Event(kind=EventKind.CONTENT, content="x")
            await asyncio.Event().wait()    # 首次调用模拟半开停滞的连接

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    slow = SlowFirst()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": slow}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_timeout_seconds=0.05))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert slow.calls == 2
    # 超时按 SOFT 处理：短冷却，不累计 err_count（不罚死号）
    rows = [tuple(r) for r in db.connect().execute(
        "SELECT err_count FROM credentials ORDER BY id")]
    assert rows == [(0,), (0,)]

async def test_complete_timeout_without_rotation_raises_unavailable(dual_repo):
    """无号可换时聚合超时以 503 语义收尾（NoHealthyCredential）。"""
    import asyncio

    class AlwaysSlow:
        async def stream_chat(self, _cred, _payload, _model):
            yield Event(kind=EventKind.CONTENT, content="x")
            await asyncio.Event().wait()

    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": AlwaysSlow()}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_timeout_seconds=0.05))

    # 对外只给受控标识（异常类名），不回显内部错误正文；完整原因在服务端日志
    with pytest.raises(NoHealthyCredential, match="UpstreamStreamError"):
        await executor.complete(_request(), username="u")

async def test_complete_timeout_zero_disables_backstop(dual_repo):
    """≤0 关闭兜底：请求正常完成（覆盖关闭分支）。"""
    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": DualProvider("codebuddy", [GOOD])},
        credentials=repo, scheduler=Scheduler(), default_model="glm-5.2",
        complete_timeout_seconds=0))
    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"

async def test_complete_max_events_overflow_rotates(dual_repo):
    """非流式聚合事件数超上限：按瞬态错误换号，不无界增长。"""
    class EndlessFirst:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls > 1:
                yield Event(kind=EventKind.CONTENT, content="ok")
                yield Event(kind=EventKind.FINISH, finish_reason="stop")
                return
            # 首个凭证持续吐 chunk 且不结束（模拟死循环输出）
            for _ in range(50):
                yield Event(kind=EventKind.CONTENT, content="x")

    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    endless = EndlessFirst()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": endless}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_max_events=2))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert endless.calls == 2

async def test_complete_max_events_zero_disables(dual_repo):
    """≤0 关闭上限：事件再多也照常聚合（覆盖关闭分支）。"""
    class ManyEvents:
        async def stream_chat(self, _cred, _payload, _model):
            for _ in range(10):
                yield Event(kind=EventKind.CONTENT, content="x")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    repo, _ = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": ManyEvents()}, credentials=repo,
        scheduler=Scheduler(), default_model="m", complete_max_events=0))
    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "x" * 10

async def test_stream_chat_normalizes_developer_role():
    """PI 的 developer 角色归一为 system（腾讯实测 developer → 11128）。"""
    import json as _json

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = _json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [
        {"role": "developer", "content": "You are PI."},
        {"role": "user", "content": "hi"},
    ]}
    _ = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]
    roles = [m["role"] for m in captured["body"]["messages"]]
    assert roles == ["system", "user"]

async def test_stream_chat_mutations_do_not_leak_into_payload():
    """出站改写（developer→system / 指纹中和 / 脏 tool_call 清理）绝不穿透回
    payload：executor 的会话粘性在改写前后都用请求原文算指纹（pin_for /
    remember），穿透会让指纹链失配、粘性静默失效。"""
    import copy

    from src.provider.codebuddy.client import CHANNEL_MARKERS

    captured: dict = {}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.read())
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    provider = CodeBuddyProvider(client=_client(capture_handler))
    payload = {"messages": [
        {"role": "developer", "content": f"plan. {CHANNEL_MARKERS[0]}"},
        {"role": "assistant", "content": [{"type": "text",
                                           "text": CHANNEL_MARKERS[1]}]},
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function", "function": {}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "x"},
        {"role": "user", "content": "hi"},
    ]}
    snapshot = copy.deepcopy(payload)
    _ = [e async for e in provider.stream_chat({"bearer_token": "t"}, payload, "m")]

    assert payload == snapshot
    # 出站体确实发生了全部三类改写（测试有效性自证）
    outbound = json.dumps(captured["body"], ensure_ascii=False)
    assert captured["body"]["messages"][0]["role"] == "system"
    assert CHANNEL_MARKERS[0] not in outbound and CHANNEL_MARKERS[1] not in outbound
    assert len(captured["body"]["messages"]) == 3    # 脏 assistant 与悬空 tool 被清

async def test_stream_chat_sanitizes_markers_and_warns(caplog):
    """默认开启：出站正文被中和并告警；可关。"""
    import json as _json

    marker = "Main branch (you will usually use this for PRs):"

    def fresh_payload() -> dict:
        return {"messages": [
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": f"见 {marker}"},
            {"role": "user", "content": "hi"},
        ]}

    def capture_handler(request: httpx.Request) -> httpx.Response:
        capture_handler.body = _json.loads(request.read())  # type: ignore[attr-defined]
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    with caplog.at_level(logging.WARNING):
        [e async for e in _client(capture_handler).stream_chat(
            CodeBuddyCredential(bearer_token="t"), fresh_payload(), "m")]
        body = capture_handler.body  # type: ignore[attr-defined]
    assert body["messages"][1]["content"] == "见 [external-client-identity]"
    assert body["messages"][2]["content"] == "hi"     # user 原样
    assert any("伪装客户端指纹" in r.getMessage() for r in caplog.records)

    def passthrough_handler(request: httpx.Request) -> httpx.Response:
        passthrough_handler.body = _json.loads(request.read())  # type: ignore[attr-defined]
        return httpx.Response(200, text=fixture("chat-basic.sse"))

    client = _client(passthrough_handler)
    client.sanitize_markers = False
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        [e async for e in client.stream_chat(
            CodeBuddyCredential(bearer_token="t"), fresh_payload(), "m")]
        body = passthrough_handler.body  # type: ignore[attr-defined]
    assert body["messages"][1]["content"] == f"见 {marker}"   # 关闭后透传
    assert not any("伪装客户端指纹" in r.getMessage() for r in caplog.records)

async def test_stream_6004_cools_only_that_model(dual_repo):
    """429 + 6004：只写 (凭证, 模型) 条目，账号级冷却保持为空。"""
    from src.provider.base import Event

    repo, db = dual_repo
    cred_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    async def gen(_cred, _payload, _model):
        yield Event(kind=EventKind.ERROR, error_code=6004, error_message="model quota",
                    error_kind=ErrKind.MODEL)

    class P:
        id = "codebuddy"
        stream_chat = staticmethod(gen)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=1), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"error" in f for f in frames)

    account = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchone()
    assert tuple(account) == (None, 0)                   # 账号未被冷却
    assert _model_cooldown_rows(db) == [(cred_id, "glm-5.2", 1, "model")]

async def test_stream_11102_blocks_account_model_pair(dual_repo):
    """11102「该后端无此模型」→ negative cache，且换号能继续服务。"""
    from src.provider.base import Event

    repo, db = dual_repo
    blocked_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    healthy_id = repo.add(provider="codebuddy", credential_data={"bearer_token": "h"})
    # 让被负缓存的凭证先被选中（pin 优先）
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (blocked_id,))
    db.connect().commit()

    class P:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, cred, _payload, _model):
            self.calls += 1
            if cred["bearer_token"] == "b":
                yield Event(kind=EventKind.ERROR, error_code=11102,
                            error_message="no such model", error_kind=ErrKind.BLOCKED)
                return
            yield Event(kind=EventKind.CONTENT, content="ok")
            yield Event(kind=EventKind.FINISH, finish_reason="stop")

    provider = P()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo,
        scheduler=Scheduler(), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"ok" in f for f in frames)                # 第二个凭证接住
    assert _model_cooldown_rows(db) == [(blocked_id, "glm-5.2", 1, "blocked")]
    # 该凭证的其他模型不受影响
    assert repo.candidates()[0].model_cooldowns is not None
    assert repo.candidates()[1].model_cooldowns is None
    assert healthy_id != blocked_id

async def test_stream_11101_request_error_does_not_touch_credential(dual_repo):
    """请求级错误（11101 请求体坏）：不冷却、不累计，但仍换号重试。

    err_count 预置为 2（阈值 3）：若请求级错误被当成普通错误累计，
    第三次就会触发熔断冷却，健康凭证被踢出池——这正是要防的误伤。
    """
    from src.engine.scheduler import ErrorOutcome
    from src.provider.base import Event

    repo, db = dual_repo
    for token in ("a", "b", "c"):
        cred_id = repo.add(provider="codebuddy", credential_data={"bearer_token": token})
        repo.save_error(cred_id, ErrorOutcome(err_count=2))

    calls = {"n": 0}

    class P:
        id = "codebuddy"

        async def stream_chat(self, _cred, _payload, _model):
            calls["n"] += 1
            yield Event(kind=EventKind.ERROR, error_code=11101,
                        error_message="Unmarshal chat params failed",
                        error_kind=ErrKind.REQUEST)

    executor = Executor(ExecutorDeps(
        providers={"codebuddy": P()}, credentials=repo,
        scheduler=Scheduler(max_rotate=3), default_model="glm-5.2"))
    frames = [f async for f in executor.stream(_request(), username="u")]
    assert any(b"error" in f for f in frames)
    assert calls["n"] == 3                               # 换号重试到上限，不是原地放弃

    rows = db.connect().execute(
        "SELECT cooling_until, err_count FROM credentials").fetchall()
    assert [tuple(row) for row in rows] == [(None, 2)] * 3   # 原样保留：没冷却也没累计
    assert _model_cooldown_rows(db) == []

async def test_http_400_11128_rotates_instead_of_skipping_provider(dual_repo):
    """渠道风控 11128（400）→ REQUEST：换号重试，不是跳过上游直接 400。

    修复前 11128 落 INVALID：_skip_provider 把该上游全部凭证塞进 tried，
    should_rotate 立即为假，第一条请求就以 invalid_request 400 收尾（零重试）。
    回归证据：真实日志里同凭证同时刻换模型即成功、同 (凭证, 模型) 秒级交替
    成功/失败，属于瞬时风控而非模型/凭证问题。
    """
    repo, db = dual_repo
    first = repo.add(provider="codebuddy", credential_data={"bearer_token": "a"})
    repo.add(provider="codebuddy", credential_data={"bearer_token": "b"})
    db.connect().execute("UPDATE credentials SET pinned = 1 WHERE id = ?", (first,))

    class P:
        id = "codebuddy"

        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, _cred, _payload, _model):
            self.calls += 1
            if self.calls == 1:
                raise UpstreamHTTPError(400, _CB_11128_BODY)
            for event in GOOD:
                yield event

    provider = P()
    executor = Executor(ExecutorDeps(
        providers={"codebuddy": provider}, credentials=repo,
        scheduler=Scheduler(max_rotate=3), default_model="glm-5.2"))

    result = await executor.complete(_request(), username="u")
    assert result["choices"][0]["message"]["content"] == "ok"
    assert provider.calls == 2                     # 换到第二个凭证重试成功

    rows = db.connect().execute(
        "SELECT cooling_until, err_count, disabled FROM credentials").fetchall()
    assert [tuple(row) for row in rows] == [(None, 0, 0)] * 2   # 两个凭证都零惩罚
    assert _model_cooldown_rows(db) == []

async def test_complete_402_cools_until_next_credit_reset(dual_repo):
    """402 余额不足：非流式路径冷到次日签到时刻（不是固定 12h）。"""
    from src.engine.scheduler import next_credit_reset

    repo, db = dual_repo
    repo.add(provider="codebuddy", credential_data={"bearer_token": "cb"})

    class PaymentRequired(Exception):
        def kind(self):
            return ErrKind.CREDIT

    executor, _trae, codebuddy = _dual_executor(repo, GOOD, [[PaymentRequired()]])
    with pytest.raises(NoHealthyCredential):
        await executor.complete(_request())

    cooling_until = db.connect().execute(
        "SELECT cooling_until FROM credentials").fetchone()["cooling_until"]
    assert cooling_until == next_credit_reset(int(time.time()))
