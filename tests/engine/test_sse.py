"""SSE 帧解析（同步与流式）与 OpenAI 帧格式化。"""

from __future__ import annotations

import json

from src.engine.sse import SSE_DONE, format_openai_frame, iter_frames
from src.provider.trae import events as trae_events
from tests.helpers.paths import provider_fixture
from tests.helpers.sse import parse_frames


def test_parse_frames_from_real_fixture():
    frames = parse_frames(provider_fixture("trae", "chat-basic.sse"))
    assert [f.event for f in frames] == [
        "metadata", "timing_cost", "output", "output", "extra_info",
        "token_usage", "done",
    ]
    assert json.loads(frames[2].data)["response"] == "中国"

def test_parse_frames_handles_comments_blank_and_multiline():
    text = ": keepalive\nevent: x\ndata: a\ndata: b\n\n"
    frames = parse_frames(text)
    assert len(frames) == 1 and frames[0].data == "a\nb"

def test_parse_frames_without_trailing_blank_line():
    assert parse_frames("event: x\ndata: 1") == [trae_events.SSEFrame(event="x", data="1")]

def test_parse_frames_no_space_after_colon():
    frames = parse_frames("event:x\ndata:1\n\n")
    assert frames[0].event == "x" and frames[0].data == "1"

def test_parse_frames_ignores_data_less_events():
    assert parse_frames("event: ping\n\n") == []

async def test_iter_frames_streaming_matches_sync_parse():
    raw = provider_fixture("trae", "chat-basic.sse").encode()
    chunks = [raw[i:i + 7] for i in range(0, len(raw), 7)]

    async def gen():
        for chunk in chunks:
            yield chunk

    streamed = [f async for f in iter_frames(gen())]
    assert streamed == parse_frames(provider_fixture("trae", "chat-basic.sse"))

def test_format_openai_frame_and_done():
    assert format_openai_frame('{"a":1}') == b'data: {"a":1}\n\n'
    assert SSE_DONE == b"data: [DONE]\n\n"
