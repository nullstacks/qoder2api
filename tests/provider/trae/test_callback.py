"""TRAE 回调解析与登录 URL 构造。"""

from __future__ import annotations

import pytest

from src.api.admin_auth import resolve_public_callback_url
from src.config import Settings
from src.provider.trae import events as trae_events
from src.provider.trae.callback import (
    build_login_url,
    credential_from_callback,
    new_machine_identity,
    parse_callback_url,
)
from tests.helpers.constants import SECRET


def test_parse_callback_url_with_refresh_token():
    url = ("http://127.0.0.1:8000/authorize?refreshToken=RT&userInfo="
           "%7B%22uid%22%3A%22u1%22%2C%22nickname%22%3A%22nick%22%7D")
    info = parse_callback_url(url)
    assert info.refresh_token == "RT" and info.uid == "u1" and info.nickname == "nick"

def test_parse_callback_url_falls_back_to_user_jwt():
    url = ('http://x/authorize?userJwt=%7B%22Token%22%3A%22T%22%2C%22RefreshToken%22%3A%22R%22%7D')
    assert parse_callback_url(url).refresh_token == "R"

@pytest.mark.parametrize("url", ["", "   ", "http://x/authorize?other=1"])
def test_parse_callback_url_rejects_missing_token(url):
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        parse_callback_url(url)

def test_build_login_url_contains_configurable_callback():
    url = build_login_url("https://gw.example/authorize", machine_id="m" * 32,
                          device_id="d" * 32)
    assert "auth_callback_url=https%3A%2F%2Fgw.example%2Fauthorize" in url
    assert "login_version=1" in url and "machine_id=" in url

def test_resolve_public_callback_url():
    settings = Settings(_env_file=None, APP_SECRET=SECRET, PUBLIC_BASE_URL="https://gw.example/")
    assert resolve_public_callback_url(settings) == "https://gw.example/authorize"

def test_new_machine_identity_is_hex32():
    machine_id, device_id = new_machine_identity()
    assert len(machine_id) == 32 and len(device_id) == 32 and machine_id != device_id

def test_credential_from_callback():
    url = "http://x/authorize?refreshToken=R&userInfo=%7B%22uid%22%3A%22u%22%7D"
    info = parse_callback_url(url)
    credential = credential_from_callback(info, "A", machine_id="m", device_id="d")
    assert credential.access_token == "A" and credential.refresh_token == "R"
    # 回调不带 expiresAt → 不臆造 TTL（保持 0），needs_refresh 保守判为需刷新
    assert credential.uid == "u" and credential.expires_at == 0
    assert credential.needs_refresh(0, now=0) is True


def test_credential_from_callback_keeps_explicit_expiry():
    """回调带 expiresAt 时原样保留。"""
    url = ("http://x/authorize?refreshToken=R"
           "&userInfo=%7B%22uid%22%3A%22u%22%2C%22expiresAt%22%3A999%7D")
    info = parse_callback_url(url)
    credential = credential_from_callback(info, "A", machine_id="m", device_id="d")
    assert credential.expires_at == 999
    assert credential.needs_refresh(0, now=100) is False
