"""CodeArts 登录票据与令牌交换 / 刷新。"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from src.provider.codearts import auth as codearts_auth
from src.provider.codearts import events as codearts_events
from tests.provider.codearts._support import jwk as _jwk
from tests.provider.codearts._support import token_response as _token_response


def _login_config() -> codearts_auth.LoginConfig:
    return codearts_auth.LoginConfig(
        portal_host="https://portal.test",
        snap_manager="https://snap.test/snap-manager",
        sts_host="https://sts.test")


def test_auth_new_ticket_identity_is_hex32():
    ticket, secret = codearts_auth.new_ticket_identity()
    assert len(ticket) == 32 and len(secret) == 32
    int(ticket, 16) and int(secret, 16)          # 合法十六进制
    assert (ticket, secret) != codearts_auth.new_ticket_identity()

def test_auth_pkce_pair_generates_and_accepts_verifier():
    verifier, challenge = codearts_auth.pkce_pair()
    assert len(verifier) >= 43 and challenge
    fixed_verifier, fixed_challenge = codearts_auth.pkce_pair("abc")
    assert fixed_verifier == "abc"
    expected = codearts_auth.base64.urlsafe_b64encode(
        hashlib.sha256(b"abc").digest()).rstrip(b"=").decode()
    assert fixed_challenge == expected

def test_auth_build_authorize_url_has_full_parameter_set():
    config = _login_config()
    url = codearts_auth.build_authorize_url(
        config, ticket_id="t" * 32, code_challenge="ch", port=8123)
    assert url.startswith("https://portal.test/authorize?")
    query = dict(httpx.URL(url).params)
    assert query == {
        "theme": "2", "locale": "zh-cn",
        "uri_scheme": config.client_id, "client_id": config.client_id,
        "port": "8123", "code_challenge": "ch",
        # portal 只认 SHA-256，"S256" 会被拒
        "code_challenge_method": "SHA-256", "ticket_id": "t" * 32,
        "plugin-name": codearts_auth.PLUGIN_NAME,
        "plugin-version": codearts_auth.PLUGIN_VERSION,
    }

def test_auth_new_login_session_bundles_state_and_dpop_key():
    session = codearts_auth.new_login_session(_login_config(), port=9000)
    assert len(session.ticket_id) == 32 and len(session.secret) == 32
    verifier, challenge = codearts_auth.pkce_pair(session.code_verifier)
    assert challenge == session.code_challenge
    assert session.auth_url.startswith("https://portal.test/authorize?")
    assert "ticket_id=" + session.ticket_id in session.auth_url
    # DPoP 私钥随会话生成，必须与 refresh_token 一起落盘
    assert session.dpop_private_jwk["kty"] == "EC"
    assert codearts_auth.redirect_uri(_login_config(), 9000) == (
        "http://127.0.0.1:9000/oauth/callback")

async def test_auth_exchange_code_reuses_or_generates_dpop_key():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == codearts_events.EP_OAUTH_TOKENS
        assert request.headers["DPoP"]           # 必带 DPoP 证明
        seen.append(dict(httpx.QueryParams(request.content.decode())))
        return httpx.Response(200, json=_token_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        jwk = _jwk()
        result = await codearts_auth.exchange_code(
            http, _login_config(), code="c", code_verifier="v", port=1234,
            dpop_private_jwk=jwk)
        assert result.dpop_private_jwk == jwk         # 复用注入的私钥
        assert seen[-1]["grant_type"] == "authorization_code"
        assert seen[-1]["redirect_uri"] == "http://127.0.0.1:1234/oauth/callback"

        generated = await codearts_auth.exchange_code(
            http, _login_config(), code="c", code_verifier="v", port=1234)
        assert generated.dpop_private_jwk["kty"] == "EC"  # 未提供则新生成

async def test_auth_post_form_error_and_non_json():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, content=b"bad request")

    transport = httpx.MockTransport(http_error)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_auth.TokenEndpointError) as caught:
            await codearts_auth.exchange_code(
                http, _login_config(), code="c", code_verifier="v", port=1)
        assert caught.value.status == 400 and caught.value.body == b"bad request"

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="nope")

    transport = httpx.MockTransport(non_json)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_events.UpstreamProtocolViolation):
            await codearts_auth.exchange_code(
                http, _login_config(), code="c", code_verifier="v", port=1)

async def test_auth_refresh_tokens_requires_token_and_key():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_events.UpstreamProtocolViolation,
                           match="refresh_token"):
            await codearts_auth.refresh_tokens(
                http, _login_config(), refresh_token="", dpop_private_jwk=_jwk())
        with pytest.raises(codearts_events.UpstreamProtocolViolation,
                           match="dpop_private_jwk"):
            await codearts_auth.refresh_tokens(
                http, _login_config(), refresh_token="rt", dpop_private_jwk={})

        seen: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json=_token_response())

        transport2 = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport2) as http:
        out = await codearts_auth.refresh_tokens(
            http, _login_config(), refresh_token="rt", dpop_private_jwk=_jwk(),
            code_verifier="v")
        assert out["refresh_token"] == "rt2"
        assert seen[-1]["grant_type"] == "refresh_token"

async def test_auth_poll_ticket_empty_and_full():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/snap-manager" + codearts_events.EP_LOGIN_TICKET
        ticket = request.url.params["ticket_id"]
        if ticket == "waiting":
            return httpx.Response(200, json={"status": "not_logged_in"})
        if ticket == "legacy":
            return httpx.Response(200, json={
                "credential": {"access": "AK", "secret": "SK",
                               "securitytoken": "TOKS"}})
        return httpx.Response(200, json=_token_response())

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        assert await codearts_auth.poll_ticket(
            http, _login_config(), ticket_id="waiting", secret="s") == {}
        legacy = await codearts_auth.poll_ticket(
            http, _login_config(), ticket_id="legacy", secret="s")
        assert legacy["credential"]["securitytoken"] == "TOKS"
        full = await codearts_auth.poll_ticket(
            http, _login_config(), ticket_id="ok", secret="s")
        assert full["refresh_token"] == "rt2"

async def test_auth_poll_ticket_error_and_json_violations():
    def http_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    transport = httpx.MockTransport(http_error)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_auth.TokenEndpointError):
            await codearts_auth.poll_ticket(
                http, _login_config(), ticket_id="t", secret="s")

    def non_object(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[1, 2])

    transport = httpx.MockTransport(non_object)
    async with httpx.AsyncClient(transport=transport) as http:
        with pytest.raises(codearts_events.UpstreamProtocolViolation):
            await codearts_auth.poll_ticket(
                http, _login_config(), ticket_id="t", secret="s")

def test_auth_credential_data_from_tokens_and_dump_jwk():
    jwk = _jwk()
    data = codearts_auth.credential_data_from_tokens(_token_response(), jwk)
    assert data["access_key_id"] == "AK2" and data["refresh_token"] == "rt2"
    assert data["dpop_private_jwk"] == jwk
    # nickname 缺省回落 user_name
    assert data["nickname"] == "bob"
    named = codearts_auth.credential_data_from_tokens(
        _token_response(nickname="N"), jwk)
    assert named["nickname"] == "N"
    assert codearts_auth.dump_jwk({"b": "2", "a": "1"}) == '{"a":"1","b":"2"}'
