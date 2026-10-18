"""CodeBuddy 出站请求头：身份拼装、企业/个人上下文、客户端指纹变体。"""

from __future__ import annotations

from src.provider.codebuddy.client import CodeBuddyCredential, build_headers
from src.provider.codebuddy.headers import encode_department, host_of


def test_host_and_domain_derived_from_same_endpoint():
    headers = build_headers(CodeBuddyCredential(bearer_token="t"), "https://copilot.tencent.com")
    assert headers["Host"] == headers["X-Domain"] == "copilot.tencent.com"

def test_department_encoded_as_utf8_percent():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部/平台组"),
        "https://copilot.tencent.com")
    encoded = "%E6%8A%80%E6%9C%AF%E9%83%A8%2F%E5%B9%B3%E5%8F%B0%E7%BB%84"
    assert headers["X-Department-Info"] == encoded
    assert headers["X-Enterprise-Id"] == "e"

def test_account_uid_takes_priority_over_user_id():
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", user_id="user", account_uid="acct"),
        "https://copilot.tencent.com")
    assert headers["X-User-Id"] == "acct"

def test_quota_only_headers_drop_enterprise_context():
    """手动凭证的额度探测只切接口，不发企业上下文头（AGENTS.md 约束）。"""
    headers = build_headers(
        CodeBuddyCredential(bearer_token="t", enterprise_id="e",
                            department_full_name="技术部", user_id="u"),
        "https://copilot.tencent.com", quota_only=True)
    assert "X-Enterprise-Id" not in headers
    assert "X-Department-Info" not in headers
    assert "X-User-Id" not in headers

def test_host_of_and_encode_department_helpers():
    assert host_of("https://www.codebuddy.ai") == "www.codebuddy.ai"
    assert encode_department("") == ""

def test_auth_start_headers_mark_anonymous():
    from src.provider.codebuddy.headers import auth_start_headers

    headers = auth_start_headers("copilot.tencent.com")
    assert headers["X-No-Authorization"] == "true"
    assert "Authorization" not in headers

def test_chat_headers_carry_channel_identity():
    """聊天头必须带完整渠道特征（缺失触发 11128 渠道风控）。"""
    from src.provider.codebuddy.headers import generate_headers

    headers = generate_headers(
        endpoint="https://copilot.tencent.com", bearer_token="t",
        user_id="u1", enterprise_id="ent1")
    # 会话链路（每请求随机）
    assert headers["X-Conversation-ID"] != headers["X-Request-ID"]
    assert len(headers["X-Conversation-Request-ID"]) == 32
    # 渠道身份
    assert headers["X-Agent-Intent"] == "craft"
    assert headers["X-Agent-Purpose"] == "conversation"
    assert headers["X-IDE-Type"] == headers["X-IDE-Name"] == "CLI"
    assert headers["X-CodeBuddy-Request"] == "1"
    assert headers["X-Private-Data"] == "false"
    assert headers["X-Requested-With"] == "XMLHttpRequest"
    # OpenAI JS SDK 指纹
    assert headers["x-stainless-lang"] == "js"
    assert headers["x-stainless-runtime"] == "node"
    assert headers["x-stainless-retry-count"] == "0"
    # 企业租户
    assert headers["X-Tenant-Id"] == "ent1"

def test_quota_only_headers_carry_full_identity():
    """quota_only 只切换 URL；头部与聊天一致（原实现不区分，防渠道校验）。"""
    from src.provider.codebuddy.headers import generate_headers

    headers = generate_headers(
        endpoint="https://copilot.tencent.com", bearer_token="t", quota_only=True)
    assert "X-Conversation-ID" in headers
    assert headers["X-Agent-Intent"] == "craft"

def test_stainless_fingerprint_variants():
    """x-stainless-arch/os 的平台分支（headers 64-79）。"""
    from unittest import mock

    from src.provider.codebuddy import headers as h

    with mock.patch.object(h.platform, "machine", return_value="x86_64"):
        assert h._stainless_arch() == "x64"
    with mock.patch.object(h.platform, "machine", return_value="aarch64"):
        assert h._stainless_arch() == "arm64"
    with mock.patch.object(h.platform, "machine", return_value="riscv"):
        assert h._stainless_arch() == "riscv"
    with mock.patch.object(h.platform, "system", return_value="Darwin"):
        assert h._stainless_os() == "MacOS"
    with mock.patch.object(h.platform, "system", return_value="Windows"):
        assert h._stainless_os() == "Windows"
    with mock.patch.object(h.platform, "system", return_value="Linux"):
        assert h._stainless_os() == "Linux"
    with mock.patch.object(h.platform, "system", return_value="SunOS"):
        assert h._stainless_os() == "SunOS"
