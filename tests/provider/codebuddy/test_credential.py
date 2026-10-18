"""CodeBuddy 凭证解析、来源归一与刷新判定。"""

from __future__ import annotations

import pytest

from src.provider.codebuddy.client import CodeBuddyCredential, parse_credential
from src.provider.codebuddy.events import UpstreamProtocolViolation


def test_credential_parse_rejects_non_object_after_json():
    from src.provider.codebuddy.credential import parse_credential

    with pytest.raises(UpstreamProtocolViolation):
        parse_credential(b"[1,2]")

async def test_credential_refresh_skew_boundary():
    from src.provider.codebuddy.credential import CodeBuddyCredential

    credential = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=100,
                                     auth_source="oauth")
    assert credential.needs_refresh(0, now=100) is True      # 正好到期
    assert credential.needs_refresh(0, now=99) is False

def test_parse_credential_bearer_only():
    credential = parse_credential({"token": "abc"})
    assert credential.bearer_token == "abc"
    assert credential.auth_source == "manual"
    assert credential.is_oauth is False

@pytest.mark.parametrize("raw", [
    b"{broken", b"[1,2]", b"{}", b'{"token":""}', b'{"token":"   "}',
])
def test_parse_credential_rejects_malformed(raw):
    with pytest.raises(UpstreamProtocolViolation):
        parse_credential(raw)

def test_parse_credential_accepts_all_token_key_names():
    for key in ("bearer_token", "access_token", "token"):
        assert parse_credential({key: "v"}).bearer_token == "v"

def test_parse_credential_validates_domain_as_hostname():
    """domain 直接进 Host/X-Domain 头：畸形值必须被拒（LOW）。"""
    ok = parse_credential({"token": "t", "domain": "copilot.tencent.com:443"})
    assert ok.domain == "copilot.tencent.com:443"
    for bad in ("evil.com/path", "a b.com", "host\r\nX-Evil: 1", "h@x.com", "a?b"):
        with pytest.raises(UpstreamProtocolViolation, match="domain"):
            parse_credential({"token": "t", "domain": bad})

def test_auth_source_normalization():
    assert parse_credential({"token": "t", "auth_source": "oauth"}).auth_source == "oauth"
    assert parse_credential({"token": "t", "auth_source": "hacked"}).auth_source == "unknown"
    assert CodeBuddyCredential.from_dict({"auth_source": "manual"}).auth_source == "manual"

def test_quota_probe_mode_normalization():
    enterprise = CodeBuddyCredential.from_dict({"quota_probe_mode": "enterprise"})
    assert enterprise.quota_probe_mode == "enterprise"
    junk = CodeBuddyCredential.from_dict({"quota_probe_mode": "junk"})
    assert junk.quota_probe_mode == "personal"

def test_bearer_only_credential_never_needs_refresh():
    """手动 bearer-only 凭证不得进入 OAuth 刷新流程（AGENTS.md 约束）。"""
    manual = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=1)
    assert manual.needs_refresh(86400, now=10_000) is False
    oauth = CodeBuddyCredential(bearer_token="t", refresh_token="r", expires_at=100,
                                auth_source="oauth")
    assert oauth.needs_refresh(86400, now=10_000) is True
    assert oauth.needs_refresh(1, now=1) is False

def test_credential_dict_roundtrip():
    original = CodeBuddyCredential(bearer_token="t", user_id="u", account_uid="a",
                                   enterprise_id="e", auth_source="oauth",
                                   quota_probe_mode="enterprise", nickname="n")
    assert CodeBuddyCredential.from_dict(original.to_dict()) == original
