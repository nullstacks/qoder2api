"""CodeArts SDK-HMAC 签名。"""

from __future__ import annotations

from src.provider.codearts import signer


def test_signer_canonical_uri_encodes_segments_and_appends_slash():
    assert signer.canonical_uri("/v1/model/builtin") == "/v1/model/builtin/"
    # 已带尾斜杠：不重复补
    assert signer.canonical_uri("/v1/model/builtin/") == "/v1/model/builtin/"
    assert signer.canonical_uri("/") == "/"
    # 每段独立 percent-encode，分隔符自身不被编码
    assert signer.canonical_uri("/a b/c+d") == "/a%20b/c%2Bd/"

def test_signer_canonical_query_sorts_and_skips_blank():
    assert signer.canonical_query("") == ""
    assert signer.canonical_query("b=2&a=1") == "a=1&b=2"
    # 空段（连续 &&）跳过；同 key 多值也排序
    assert signer.canonical_query("a=2&&a=1") == "a=1&a=2"
    # key/value 都 quote
    assert signer.canonical_query("k=a b") == "k=a%20b"

def test_signer_canonical_headers_lowercases_and_sorts_all_headers():
    canonical, signed = signer.canonical_headers(
        {"X-B": " 2 ", "A": "1", "Content-Type": "application/json"})
    assert canonical == "a:1\ncontent-type:application/json\nx-b:2\n"
    assert signed == "a;content-type;x-b"

def test_signer_canonical_headers_strips_crlf():
    """L5：头值里的 CR/LF 会破坏 canonical request 的换行结构，必须剥离。"""
    canonical, _ = signer.canonical_headers({"A": "x\r\ny"})
    assert canonical == "a:xy\n"

def test_signer_fixed_vector_canonical_request_and_string_to_sign():
    payload = b'{"a":1}'
    payload_hash = signer.sha256_hex(payload)
    assert payload_hash == (
        "015abd7f5cc57a2dd94b7590f04ad8084273905ee33ec5cebeae62276a97f862")
    headers = {
        "Content-Type": "application/json", "X-Language": "zh-cn",
        "X-Sdk-Date": "20250102T030405Z", "X-Security-Token": "TOKEN123",
        "X-Sdk-Content-Sha256": payload_hash,
    }
    request, signed = signer.canonical_request(
        "POST", "/v1/model/builtin", "b=2&a=1", headers, payload_hash)
    assert signed == ("content-type;x-language;x-sdk-content-sha256;"
                      "x-sdk-date;x-security-token")
    assert request == (
        "POST\n"
        "/v1/model/builtin/\n"
        "a=1&b=2\n"
        "content-type:application/json\n"
        "x-language:zh-cn\n"
        f"x-sdk-content-sha256:{payload_hash}\n"
        "x-sdk-date:20250102T030405Z\n"
        "x-security-token:TOKEN123\n"
        "\n"
        f"{signed}\n"
        f"{payload_hash}")
    assert signer.string_to_sign(request, "20250102T030405Z") == (
        "SDK-HMAC-SHA256\n"
        "20250102T030405Z\n"
        + signer.sha256_hex(request.encode("utf-8")))

def test_signer_sign_headers_full_flow_with_token_and_trace():
    credential = signer.SignCredential("AKIDEXAMPLE", "SECRETEXAMPLE", "TOKEN123")
    source_headers = {"Content-Type": "application/json", "X-Language": "zh-cn"}
    out = signer.sign_headers(
        credential, method="POST", path="/v1/model/builtin", raw_query="b=2&a=1",
        headers=source_headers, payload=b'{"a":1}', x_sdk_date="20250102T030405Z",
        trace_id="trace-1")
    # 全部头都进 SignedHeaders（含 trace_id 与补入的签名头）
    assert out["X-Sdk-Content-Sha256"] == signer.sha256_hex(b'{"a":1}')
    assert out["X-Security-Token"] == "TOKEN123"
    assert out["x-snap-traceid"] == "trace-1"
    assert out["Authorization"].startswith("SDK-HMAC-SHA256 Access=AKIDEXAMPLE, ")
    signed = out["Authorization"].split("SignedHeaders=")[1].split(",")[0]
    assert signed == ("content-type;x-language;x-sdk-content-sha256;"
                      "x-sdk-date;x-security-token;x-snap-traceid")
    # 入参不被就地修改（返回合并后的新 dict）
    assert source_headers == {"Content-Type": "application/json",
                              "X-Language": "zh-cn"}

def test_signer_sign_headers_empty_security_token_omits_header():
    credential = signer.SignCredential("AK", "SK", "")
    out = signer.sign_headers(
        credential, method="GET", path="/x", headers={}, payload=b"",
        x_sdk_date="20250102T030405Z")
    assert "X-Security-Token" not in out
    assert "x-snap-traceid" not in out
    assert signer.hmac_sha256_hex("k", "m") == signer.hmac_sha256_hex("k", "m")
