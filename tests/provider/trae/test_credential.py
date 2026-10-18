"""TRAE 凭证解析与刷新判定。"""

from __future__ import annotations

import pytest

from src.provider.trae import events as trae_events
from src.provider.trae.client import TraeCredential, parse_credential


def test_parse_credential_nested_and_flat():
    nested = {"auth": {"accessToken": "a", "refreshToken": "r", "expiresAt": 10,
                       "machineId": "m", "deviceId": "d"},
              "account": {"uid": "u1", "nickname": "nick"}}
    flat = {"accessToken": "a", "uid": "u1"}
    assert parse_credential(nested).access_token == "a"
    assert parse_credential(nested).nickname == "nick"
    assert parse_credential(flat).uid == "u1"

@pytest.mark.parametrize("raw", [
    b"{broken", b'{"auth":[]}', b'{"auth":"x","account":{}}',
    b'{"auth":{"accessToken":""},"account":{}}', b"[1,2]",
])
def test_parse_credential_rejects_malformed(raw):
    with pytest.raises(trae_events.UpstreamProtocolViolation):
        parse_credential(raw)

def test_credential_needs_refresh():
    assert TraeCredential(access_token="a").needs_refresh(3600, now=0)
    cred = TraeCredential(access_token="a", expires_at=10_000)
    assert cred.needs_refresh(3600, now=7_000)
    assert not cred.needs_refresh(3600, now=1_000)

def test_credential_dict_roundtrip():
    original = TraeCredential(uid="u", access_token="a", refresh_token="r", expires_at=5,
                              machine_id="m", device_id="d", nickname="n")
    assert TraeCredential.from_dict(original.to_dict()) == original
