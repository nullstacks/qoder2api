"""实时事件广播：`LiveEventHub` 的缓冲/上限/注销，与 SSE 生成器的帧格式。"""

from __future__ import annotations

import pytest

from src.api.admin_stats import _event_matches, _sse_frame, _stream_events
from src.stats.live import (
    MAX_BUFFERED,
    MAX_SUBSCRIBERS,
    LiveEventHub,
    TooManySubscribers,
)


class FakeRequest:
    """最小 `Request` 替身：只实现 `is_disconnected`（生成器只调它）。

    `disconnect_after` = 前几次调用返回 False（让循环正常跑），之后返回 True。
    """

    def __init__(self, disconnect_after: int) -> None:
        self.calls = 0
        self.disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.calls += 1
        return self.calls > self.disconnect_after


def test_hub_subscribe_and_publish_fan_out():
    hub = LiveEventHub()
    first = hub.subscribe()
    second = hub.subscribe()
    assert hub.subscriber_count == 2
    hub.publish({"id": "e1"})
    assert first.drain() == [{"id": "e1"}]
    assert second.drain() == [{"id": "e1"}]
    # drain 清空缓冲并复位唤醒信号：下一轮 publish 才能再次唤醒
    assert first.drain() == []
    assert not first.wakeup.is_set()

def test_hub_buffer_drops_oldest_when_full():
    """慢客户端不能撑爆内存：超过上限丢最旧的一条，保留最新的。"""
    hub = LiveEventHub()
    subscription = hub.subscribe()
    for index in range(MAX_BUFFERED + 5):
        hub.publish({"id": index})
    batch = subscription.drain()
    assert len(batch) == MAX_BUFFERED
    assert batch[0] == {"id": 5}
    assert batch[-1] == {"id": MAX_BUFFERED + 4}

def test_hub_rejects_beyond_max_subscribers():
    hub = LiveEventHub(max_subscribers=1)
    hub.subscribe()
    with pytest.raises(TooManySubscribers):
        hub.subscribe()
    assert hub.subscriber_count == 1

def test_hub_unsubscribe_is_idempotent():
    hub = LiveEventHub()
    subscription = hub.subscribe()
    hub.unsubscribe(subscription)
    hub.unsubscribe(subscription)          # 重复注销（finally + 显式清理）无害
    assert hub.subscriber_count == 0
    hub.publish({"id": "e1"})              # 没有订阅者时空转
    assert subscription.drain() == []

def test_default_subscriber_limit_is_used_by_build_app():
    assert MAX_SUBSCRIBERS == 8

def test_event_matches_filters():
    event = {"username": "alice", "provider": "trae", "model": "m",
             "credential_id": "cred-1", "ok": 0}
    assert _event_matches(event, username=None, provider=None, model=None,
                          credential_id=None)
    assert _event_matches(event, username="alice", provider="trae", model="m",
                          credential_id="cred-1")
    assert not _event_matches(event, username="bob", provider=None, model=None,
                              credential_id=None)
    assert not _event_matches(event, username=None, provider="codebuddy", model=None,
                              credential_id=None)
    assert not _event_matches(event, username=None, provider=None, model="other",
                              credential_id=None)
    assert not _event_matches(event, username=None, provider=None, model=None,
                              credential_id="cred-2")
    # 成功/失败过滤：与明细端点同一语义（None = 不限制）
    assert _event_matches(event, username=None, provider=None, model=None,
                          credential_id=None, ok=0)
    assert not _event_matches(event, username=None, provider=None, model=None,
                              credential_id=None, ok=1)

def test_sse_frame_is_single_data_line():
    frame = _sse_frame({"model": "glm-5.2", "ok": 1})
    assert frame == 'data: {"model":"glm-5.2","ok":1}\n\n'

async def test_stream_emits_connected_frame_then_events_and_unsubscribes():
    hub = LiveEventHub()
    subscription = hub.subscribe()
    hub.publish({"id": "e1", "provider": "trae", "model": "m"})
    hub.publish({"id": "e2", "provider": "codebuddy", "model": "m"})
    request = FakeRequest(disconnect_after=1)
    frames = [frame async for frame in _stream_events(
        request, hub, subscription, username=None, provider="trae", model=None,
        credential_id=None, resolve_name=None)]
    # 第一条是注释帧（让 EventSource 进入 OPEN），之后只剩过滤命中的事件
    assert frames[0] == ": connected\n\n"
    assert frames[1] == 'data: {"id":"e1","provider":"trae","model":"m"}\n\n'
    assert len(frames) == 2
    assert hub.subscriber_count == 0            # 断开即注销

async def test_stream_resolves_credential_name_only_when_operator():
    hub = LiveEventHub()
    subscription = hub.subscribe()
    hub.publish({"id": "e1", "credential_id": "cred-1", "provider": "trae"})
    resolved: list[str] = []

    def resolve(credential_id: str) -> str | None:
        resolved.append(credential_id)
        return "alice@example.com"

    frames = [frame async for frame in _stream_events(
        FakeRequest(disconnect_after=1), hub, subscription, username=None,
        provider=None, model=None, credential_id=None, resolve_name=resolve)]
    assert resolved == ["cred-1"]
    assert '"credential_name":"alice@example.com"' in frames[1]

async def test_stream_skips_name_lookup_without_credential_id():
    """没有凭证 id 的事件不查昵称（虚拟凭证 / 失败请求）。"""
    hub = LiveEventHub()
    subscription = hub.subscribe()
    hub.publish({"id": "e1", "credential_id": None, "provider": "trae"})

    def resolve(_credential_id: str) -> str | None:  # pragma: no cover - 不该被调用
        raise AssertionError("无凭证 id 时不应解析昵称")

    frames = [frame async for frame in _stream_events(
        FakeRequest(disconnect_after=1), hub, subscription, username=None,
        provider=None, model=None, credential_id=None, resolve_name=resolve)]
    assert "credential_name" not in frames[1]

async def test_stream_filters_only_failed_events():
    """`ok=0` 订阅只推失败行（失败归因下钻后开实时流）。"""
    hub = LiveEventHub()
    subscription = hub.subscribe()
    hub.publish({"id": "e1", "provider": "trae", "model": "m", "ok": 1})
    hub.publish({"id": "e2", "provider": "trae", "model": "m", "ok": 0})
    frames = [frame async for frame in _stream_events(
        FakeRequest(disconnect_after=1), hub, subscription, username=None,
        provider=None, model=None, credential_id=None, resolve_name=None, ok=0)]
    assert frames[0] == ": connected\n\n"
    assert '"id":"e2"' in frames[1]
    assert len(frames) == 2

async def test_stream_sends_heartbeat_when_idle():
    """空闲时按 `heartbeat` 发注释帧保活（代理会断掉静默连接）。"""
    hub = LiveEventHub()
    subscription = hub.subscribe()
    frames = [frame async for frame in _stream_events(
        FakeRequest(disconnect_after=1), hub, subscription, username=None,
        provider=None, model=None, credential_id=None, resolve_name=None,
        heartbeat=0.01)]
    assert frames[0] == ": connected\n\n"
    assert ": heartbeat\n\n" in frames

async def test_stream_unsubscribes_when_client_closes_early():
    """客户端提前 aclose 也必须注销（否则订阅槽位泄漏，8 次后全员 429）。"""
    hub = LiveEventHub()
    subscription = hub.subscribe()
    generator = _stream_events(FakeRequest(disconnect_after=0), hub, subscription,
                               username=None, provider=None, model=None,
                               credential_id=None, resolve_name=None)
    assert await anext(generator) == ": connected\n\n"
    await generator.aclose()
    assert hub.subscriber_count == 0
