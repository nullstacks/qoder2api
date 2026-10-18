"""会话令牌签发与校验。"""

from __future__ import annotations

import pytest

from src.auth import session as session_mod


def test_session_roundtrip_and_expiry():
    token = session_mod.create_session_token("alice", "sec", issued_at=1000, ttl_seconds=60)
    assert session_mod.verify_session_token(token, "sec", now=1030) == ("alice", 0)
    assert session_mod.verify_session_token(token, "sec", now=2000) is None
    assert session_mod.verify_session_token(token, "wrong", now=1030) is None

@pytest.mark.parametrize("bad", ["", "abc", "a.b", "notbase64!!.x"])
def test_session_rejects_malformed(bad):
    assert session_mod.verify_session_token(bad, "sec", now=1) is None


@pytest.mark.parametrize("bad", [
    "abc.\u00ff\u00fe",   # 签名段非 ASCII：compare_digest 抛 TypeError
    "\u00ff.\u00ff",      # payload 段非 ASCII：_sign 的 encode("ascii") 抛 UnicodeError
])
def test_session_rejects_non_ascii_cookie_without_raising(bad):
    """非 ASCII Cookie 由客户端任意构造，必须归一为「非法」而不是冒泡 500。"""
    assert session_mod.verify_session_token(bad, "sec", now=1) is None
