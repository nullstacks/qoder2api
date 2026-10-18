"""会话粘性（E1）：请求头会话标识参与的优先级与隔离。

优先级硬性要求：**请求体显式键 > 请求头会话标识 > 消息前缀指纹**。
`pin_for` 与 `remember` 必须用同一套优先级，否则「这轮靠头命中、下轮却写
body 键」会立刻断粘。头值同样掺 username，防两个用户填同一个头值串号。
"""

from __future__ import annotations

import pytest

from src.engine.affinity import (
    ConversationAffinity,
    explicit_key,
    has_user_id,
    header_key,
    resolve_explicit,
)
from src.request_context import set_session_id

NOW = 1000.0
TURN1 = [{"role": "system", "content": "s"}, {"role": "user", "content": "q1"}]
TURN1_REPLY = [{"role": "assistant", "content": "a1"}]
TURN2 = TURN1 + TURN1_REPLY + [{"role": "user", "content": "q2"}]


def raw(messages: list[dict], **extra) -> dict:
    return {"messages": messages, **extra}


@pytest.fixture(autouse=True)
def _clear_session_id():
    """每个用例前后复原 contextvar，避免用例之间串会话。"""
    set_session_id(None)
    yield
    set_session_id(None)


# --------------------------------------------------------------- 键解析

def test_header_key_reads_contextvar_and_prefixes_source():
    set_session_id("sess-1")
    assert header_key() == "header:sess-1"
    # 与 body 同值不撞键：来源前缀不同
    assert header_key() != explicit_key({"conversation_id": "sess-1"})


def test_header_key_rejects_illegal_or_missing_value():
    assert header_key() is None          # contextvar 为空
    set_session_id("has space")
    assert header_key() is None          # 中间件之外被塞了非法值
    set_session_id("x" * 129)
    assert header_key() is None          # 超长


def test_header_key_accepts_injected_reader():
    """可注入读取器：不用构造真实请求即可测（默认走 contextvar）。"""
    assert header_key(read_session_id=lambda: " injected ") == "header:injected"
    assert header_key(read_session_id=lambda: None) is None


def test_resolve_explicit_prefers_body_then_header():
    set_session_id("sess-1")
    assert resolve_explicit({"conversation_id": "conv-1"}) == "conversation_id:conv-1"
    assert resolve_explicit({"messages": TURN1}) == "header:sess-1"
    set_session_id(None)
    assert resolve_explicit({"messages": TURN1}) is None
    # 注入读取器同样生效（覆盖显式传入分支）
    assert resolve_explicit({}, read_session_id=lambda: "s2") == "header:s2"


# --------------------------------------------------------------- 粘性命中

def test_header_pins_before_prefix_fallback():
    """头命中优先于消息前缀兜底：两者都能命中时用头的凭证。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("sess-1")
    affinity.remember(raw(TURN1), "alice", "c_header", now=NOW)     # 头键
    set_session_id(None)
    affinity.remember(raw(TURN1), "alice", "c_prefix", now=NOW)     # 前缀键
    assert len(affinity) == 2
    set_session_id("sess-1")
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c_header"
    # 去掉头 → 回到前缀兜底
    set_session_id(None)
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c_prefix"


def test_body_explicit_key_wins_over_header():
    """同一请求既有 body 显式键又有头时，两边读写都用 body 键。"""
    set_session_id("sess-1")
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c_header", now=NOW)          # 只因头而写
    affinity.remember(raw(TURN1, conversation_id="conv-1"),
                      "alice", "c_body", now=NOW)                        # body 键
    # 两者同时存在时用 body 的
    assert affinity.pin_for(raw(TURN1, conversation_id="conv-1"),
                            "alice", now=NOW) == "c_body"
    # 只有头（无 body 键）时命中头键，不会被 body 键顶掉
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) == "c_header"
    # 只有另一个 body 键时是新会话
    assert affinity.pin_for(raw(TURN1, conversation_id="conv-2"),
                            "alice", now=NOW) is None


def test_header_and_body_same_value_do_not_collide():
    """body `conversation_id:abc` 与头 `header:abc` 是两个键。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1, conversation_id="abc"), "alice", "c_body", now=NOW)
    set_session_id("abc")
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) is None
    assert len(affinity) == 1


def test_same_header_value_does_not_cross_users():
    """两个 API 用户填同一个头值，粘性记录互不可见。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("shared")
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) == "c1"
    assert affinity.pin_for(raw(TURN1), "bob", now=NOW) is None


def test_remember_by_header_then_next_turn_pins():
    """靠头命中写入的条目，下一轮同头能 pin 到（remember/pin 同优先级）。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("sess-1")
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    # 消息完全换掉也不影响：头是显式身份
    other = [{"role": "user", "content": "brand new"}]
    assert affinity.pin_for(raw(other), "alice", now=NOW) == "c1"


def test_header_still_applies_when_body_has_user_id():
    """user_id 只否定前缀兜底，不否定头这种显式身份。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("sess-1")
    affinity.remember(raw(TURN1, user_id="u1"), "alice", "c1", now=NOW)
    assert len(affinity) == 1
    assert affinity.pin_for(raw(TURN2, user_id="u1"), "alice", now=NOW) == "c1"
    # metadata 内的 user_id 同理
    set_session_id("sess-2")
    affinity.remember(raw(TURN1, metadata={"user_id": "u1"}), "alice", "c2", now=NOW)
    assert affinity.pin_for(raw(TURN2, metadata={"user_id": "u1"}),
                            "alice", now=NOW) == "c2"


def test_illegal_header_falls_back_to_prefix():
    """非法头值（含空格/超长）被丢弃，退回前缀兜底。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    affinity.remember(raw(TURN1), "alice", "c_prefix", now=NOW)
    set_session_id("bad value")           # 含空格
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c_prefix"
    set_session_id("x" * 129)             # 超长
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c_prefix"


def test_illegal_header_does_not_write_entry():
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("bad value")
    affinity.remember(raw(TURN1, user_id="u1"), "alice", "c1", now=NOW)
    assert len(affinity) == 0


def test_header_honours_ttl_and_non_dict_raw():
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("sess-1")
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW + 601) is None
    assert len(affinity) == 0
    # ttl 关闭时头也不生效
    disabled = ConversationAffinity(ttl_seconds=0)
    disabled.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert len(disabled) == 0
    assert disabled.pin_for(raw(TURN1), "alice", now=NOW) is None


def test_non_dict_raw_still_ignored_with_header():
    affinity = ConversationAffinity(ttl_seconds=600)
    set_session_id("sess-1")
    affinity.remember(None, "alice", "c1", now=NOW)  # type: ignore[arg-type]
    assert len(affinity) == 0
    assert affinity.pin_for(None, "alice", now=NOW) is None  # type: ignore[arg-type]


# ------------------------------------------------------- 无头时与旧实现一致

@pytest.mark.asyncio
async def test_executor_sticks_by_request_header_end_to_end():
    """端到端：中间件设的 session id 经 contextvar 传到 executor，两轮粘同一凭证。"""
    from src.compat.openai.request import parse_chat_request
    from src.engine.executor import Executor, ExecutorDeps
    from src.engine.scheduler import Candidate, Scheduler
    from tests.engine.test_affinity import FakeProvider, FakeRepo, _drain

    affinity = ConversationAffinity(ttl_seconds=600)
    repo = FakeRepo(rows=[
        Candidate(credential_id="c1", provider="trae"),
        Candidate(credential_id="c2", provider="trae"),
    ])
    provider = FakeProvider()
    executor = Executor(ExecutorDeps(
        providers={"trae": provider}, credentials=repo,
        scheduler=Scheduler(), affinity=affinity))

    def body(messages):
        return parse_chat_request({"model": "glm-5.2", "messages": messages,
                                   "stream": True})

    set_session_id("sess-1")
    await _drain(executor.stream(body(TURN1), username="alice"))
    # 第二轮消息不延续（模拟客户端裁剪历史）：只能靠头粘住
    await _drain(executor.stream(body([{"role": "user", "content": "new"}]),
                                 username="alice"))
    assert provider.served == ["c1", "c1"]


def test_without_header_behaviour_matches_legacy():
    """不带任何会话头：body 显式键与消息前缀的行为逐项与旧实现一致。"""
    affinity = ConversationAffinity(ttl_seconds=600)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) is None
    affinity.remember(raw(TURN1), "alice", "c1", now=NOW)
    assert affinity.pin_for(raw(TURN1), "alice", now=NOW) == "c1"
    assert affinity.pin_for(raw(TURN2), "alice", now=NOW) == "c1"
    # 带 user_id 时不出前缀兜底键
    affinity.remember(raw(TURN2, user_id="u1"), "alice", "c2", now=NOW)
    assert len(affinity) == 1
    # body 显式键仍优先且不受消息影响
    affinity.remember(raw(TURN1, prompt_cache_key="k1"), "alice", "c3", now=NOW)
    assert affinity.pin_for(raw([{"role": "user", "content": "other"}],
                                prompt_cache_key="k1"), "alice", now=NOW) == "c3"
    assert has_user_id({"user_id": "u1"}) is True
