"""CodeArts DPoP 证明。"""

from __future__ import annotations

import json

import pytest

from src.provider.codearts import dpop
from tests.provider.codearts._support import jwk as _jwk


def test_dpop_private_jwk_roundtrip_and_public_strip():
    jwk = _jwk()
    assert set(jwk) == {"kty", "crv", "x", "y", "d"}
    key = dpop.private_key_from_jwk(jwk)
    assert key.private_numbers().private_value == int.from_bytes(
        dpop._b64url_decode(jwk["d"]), "big")
    public = dpop.public_jwk(jwk)
    assert set(public) == {"kty", "crv", "x", "y"}
    assert public["x"] == jwk["x"]

def test_dpop_private_key_from_jwk_rejects_malformed():
    with pytest.raises(ValueError, match="curve"):
        dpop.private_key_from_jwk({"kty": "RSA", "crv": "P-256", "d": "AA"})
    with pytest.raises(ValueError, match="JWK d"):
        dpop.private_key_from_jwk({"kty": "EC", "crv": "P-256"})
    zero = dpop._b64url(b"\x00" * 32)
    with pytest.raises(ValueError, match="JWK d"):
        dpop.private_key_from_jwk({"kty": "EC", "crv": "P-256", "d": zero})

def test_dpop_normalize_low_s_both_branches():
    n = dpop.P256_ORDER
    half = n // 2
    assert dpop.normalize_low_s(1) == 1
    assert dpop.normalize_low_s(half) == half
    assert dpop.normalize_low_s(half + 1) == n - (half + 1)
    assert dpop.normalize_low_s(n - 1) == 1

def test_dpop_sign_proof_structure_and_random_defaults():
    jwk = _jwk()
    proof = dpop.sign_proof(jwk, "https://sts.test/v1/oauth2/tokens")
    parts = proof.split(".")
    assert len(parts) == 3
    header = dpop.decode_segment(parts[0])
    assert header["alg"] == "ES256" and header["typ"] == "dpop+jwt"
    assert header["jwk"] == dpop.public_jwk(jwk)          # 头里只有公钥
    payload = dpop.decode_segment(parts[1])
    assert payload["htm"] == "POST"
    assert payload["htu"] == "https://sts.test/v1/oauth2/tokens"
    assert payload["jti"] and len(payload["jti"]) == 32   # 默认随机 jti
    assert dpop.verify_proof(
        proof, dpop.public_key_from_jwk(jwk)) is True

def test_dpop_sign_proof_injectable_iat_and_jti():
    jwk = _jwk()
    proof = dpop.sign_proof(jwk, "https://x", htm="POST", issued_at=1234, jti="fixed")
    payload = dpop.decode_segment(proof.split(".")[1])
    assert payload["iat"] == 1234 and payload["jti"] == "fixed"

def test_dpop_sign_proof_always_low_s():
    jwk = _jwk()
    half = dpop.P256_ORDER // 2
    for _ in range(50):
        signature = dpop._b64url_decode(
            dpop.sign_proof(jwk, "https://x").split(".")[2])
        s = int.from_bytes(signature[32:], "big")
        assert s <= half

def test_dpop_sign_proof_forces_high_s_normalization(monkeypatch):
    """强制 ECDSA 返回 high-S，断言编码前被归一化（华为 STS 的硬要求）。"""
    jwk = _jwk()
    high_s = dpop.P256_ORDER - 5                    # > n/2，属 high-S
    monkeypatch.setattr(dpop, "decode_dss_signature", lambda der: (1234, high_s))
    proof = dpop.sign_proof(jwk, "https://x", issued_at=1, jti="fixed")
    signature = dpop._b64url_decode(proof.split(".")[2])
    assert int.from_bytes(signature[:32], "big") == 1234
    assert int.from_bytes(signature[32:], "big") == 5      # n - (n - 5)
    assert int.from_bytes(signature[32:], "big") <= dpop.P256_ORDER // 2

def test_dpop_verify_proof_rejects_malformed_and_tampered():
    jwk = _jwk()
    proof = dpop.sign_proof(jwk, "https://x", issued_at=1, jti="j")
    public = dpop.public_key_from_jwk(jwk)
    assert dpop.verify_proof("a.b", public) is False            # 段数不对
    # 段数为 3 但签名段 base64 非法（"a" 的字符数 mod 4 == 1）
    assert dpop.verify_proof("a.b.a", public) is False
    assert dpop.verify_proof("a.b." + dpop._b64url(b"1234"), public) is False
    head, payload, signature = proof.split(".")
    raw = bytearray(dpop._b64url_decode(signature))
    raw[-1] ^= 0xFF
    tampered = f"{head}.{payload}.{dpop._b64url(bytes(raw))}"
    assert dpop.verify_proof(tampered, public) is False

def test_dpop_decode_segment_rejects_non_object():
    with pytest.raises(ValueError, match="not an object"):
        dpop.decode_segment(dpop._b64url(json.dumps([1, 2]).encode()))
