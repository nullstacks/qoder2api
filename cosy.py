# cosy.py — COSY protocol for Qoder international (api1.qoder.sh).
# Python port of cosy.js (which was ported from RobbsLuo/Coding2API
# src/provider/qoder/{cosy,events}.py).
#
# Stdlib-only crypto (mirrors the zero-dependency Worker port):
#   - MD5 / SHA-512: hashlib
#   - AES-128-CBC: pure-Python implementation below (identity envelope;
#     key is reused as the IV, exactly like the Worker's WebCrypto call)
#   - RSA PKCS#1 v1.5 encryption: pow() modexp + minimal SPKI DER parser
#     (stdlib has no RSA; the protocol wraps the session AES key with the
#     server's 1024-bit public key)

import base64
import hashlib
import json
import secrets
import urllib.parse
import uuid

# --- base64 --------------------------------------------------------------------


def b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64decode(text: str) -> bytes:
    return base64.b64decode(text)


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


# --- custom base64 variant (Qoder wire format) ----------------------------------

_STD = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
_CUSTOM = "_doRTgHZBKcGVjlvpC,@aFSx#DPuNJme&i*MzLOEn)sUrthbf%Y^w.(kIQyXqWA!"
_ENC = {s: c for s, c in zip(_STD + "=", _CUSTOM + "$")}


def qoder_encode(data: bytes) -> str:
    """Standard base64, rotated (head = len/3 moves to the tail), then
    remapped onto Qoder's custom alphabet ('=' -> '$')."""
    std = b64encode(data)
    size = len(std)
    head = size // 3
    rearranged = std[size - head:] + std[head:size - head] + std[:head]
    return "".join(_ENC.get(c, c) for c in rearranged)


# --- sorted compact JSON ---------------------------------------------------------


def json_sorted_compact(mapping: dict) -> str:
    """Sorted keys, no whitespace, null -> "" (mirrors the JS helper; JSON
    strings are emitted with real UTF-8, not \\u escapes, like JSON.stringify)."""
    parts = []
    for key in sorted(mapping.keys()):
        value = mapping[key]
        if value is None:
            value = ""
        parts.append(
            json.dumps(key, ensure_ascii=False, separators=(",", ":"))
            + ":"
            + json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        )
    return "{" + ",".join(parts) + "}"


# --- MD5 (protocol fingerprint + signature; upstream uses it, not security) ------


def md5_hex(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# --- stable device fingerprints ---------------------------------------------------


def _derive_id(uid: str, salt: str) -> str:
    return md5_hex(f"{salt}:{uid or 'anonymous'}")[:36]


def _derive_machine_type(uid: str) -> str:
    return _derive_id(uid, "machinetype").replace("-", "")[:18]


def _derive_machine_token(uid: str) -> str:
    digest = hashlib.sha512(f"machinetoken:{uid}".encode("utf-8")).digest()
    return b64url_encode(digest)[:43]


# --- pure-Python AES-128 (encrypt only; CBC mode below) ---------------------------
# Generated S-box (multiplicative inverse in GF(2^8) + affine transform) so the
# table can't drift, then the standard FIPS-197 block cipher.


def _gf_mul(a: int, b: int) -> int:
    out = 0
    for _ in range(8):
        if b & 1:
            out ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return out


def _gen_sbox() -> list:
    sbox = [0] * 256
    for x in range(256):
        # inverse = x^254 in GF(2^8); 0 maps to 0
        inv = 0
        if x:
            base, acc, exp = x, 1, 254
            while exp:
                if exp & 1:
                    acc = _gf_mul(acc, base)
                base = _gf_mul(base, base)
                exp >>= 1
            inv = acc
        s = inv
        r = inv
        for _ in range(4):
            r = ((r << 1) | (r >> 7)) & 0xFF
            s ^= r
        sbox[x] = s ^ 0x63
    return sbox


_SBOX = _gen_sbox()
_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a = (a ^ 0x1B) & 0xFF
    return a


def _expand_key(key: bytes) -> list:
    """AES-128 key schedule -> 11 round keys (each 16 bytes, input order)."""
    if len(key) != 16:
        raise ValueError("aes: 128-bit key required")
    words = [list(key[4 * i:4 * i + 4]) for i in range(4)]
    for i in range(4, 44):
        temp = list(words[i - 1])
        if i % 4 == 0:
            temp = temp[1:] + temp[:1]                      # RotWord
            temp = [_SBOX[b] for b in temp]                 # SubWord
            temp[0] ^= _RCON[i // 4 - 1]
        words.append([words[i - 4][j] ^ temp[j] for j in range(4)])
    return [bytes(b for word in words[4 * k:4 * k + 4] for b in word) for k in range(11)]


def _encrypt_block(round_keys: list, block: bytes) -> bytes:
    state = list(block)  # state[r + 4c] == in[r + 4c] (FIPS-197 column-major)

    def add_round_key(rk: bytes) -> None:
        for i in range(16):
            state[i] ^= rk[i]

    def sub_shift_rows() -> None:
        out = [0] * 16
        for c in range(4):
            for r in range(4):
                out[r + 4 * c] = _SBOX[state[r + 4 * ((c + r) % 4)]]
        state[:] = out

    def mix_columns() -> None:
        for c in range(4):
            s0, s1, s2, s3 = state[4 * c:4 * c + 4]
            t = s0 ^ s1 ^ s2 ^ s3
            state[4 * c + 0] = s0 ^ t ^ _xtime(s0 ^ s1)
            state[4 * c + 1] = s1 ^ t ^ _xtime(s1 ^ s2)
            state[4 * c + 2] = s2 ^ t ^ _xtime(s2 ^ s3)
            state[4 * c + 3] = s3 ^ t ^ _xtime(s3 ^ s0)

    add_round_key(round_keys[0])
    for rnd in range(1, 10):
        sub_shift_rows()
        mix_columns()
        add_round_key(round_keys[rnd])
    sub_shift_rows()
    add_round_key(round_keys[10])
    return bytes(state)


def aes_cbc_encrypt(plain: bytes, key: bytes) -> bytes:
    """AES-128-CBC with the key doubling as the IV (protocol quirk).

    Wire-compat quirk: WebCrypto's AES-CBC encrypt ALWAYS appends its own
    PKCS#7 pad block — even to block-aligned input — so the Worker's
    manually-padded plaintext comes out double-padded (identity JSON length
    + manual pad + 16). Verified against Workers/Node WebCrypto; upstream
    decrypts exactly this. Returns ciphertext only, like the Worker call."""
    round_keys = _expand_key(key)
    pad = 16 - (len(plain) % 16)
    padded = plain + bytes([pad]) * pad
    # WebCrypto's implicit second pad block (full 0x10 x 16 when aligned,
    # same PKCS#7 formula applied on top of the manual padding)
    padded += bytes([16]) * 16
    prev = key  # IV == key
    out = bytearray()
    for off in range(0, len(padded), 16):
        block = bytes(p ^ q for p, q in zip(padded[off:off + 16], prev))
        prev = _encrypt_block(round_keys, block)
        out += prev
    return bytes(out)


# --- RSA PKCS#1 v1.5 (server key wraps the session AES key) -----------------------

_SERVER_PUBLIC_KEY_PEM_BODY = (
    "MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDA8iMH5c02LilrsERw9t6Pv5Nc"
    "4k6Pz1EaDicBMpdpxKduSZu5OANqUq8er4GM95omAGIOPOh+Nx0spthYA2BqGz+l"
    "6HRkPJ7S236FZz73In/KVuLnwI8JJ2CbuJap8kvheCCZpmAWpb/cPx/3Vr/J6I17"
    "XcW+ML9FoCI6AOvOzwIDAQAB"
)


def _parse_spki_der(der: bytes):
    """SubjectPublicKeyInfo DER -> (n, e) big-endian byte strings."""

    def read_len(pos):
        first = der[pos]
        if first < 0x80:
            return first, pos + 1
        num = first & 0x7F
        length = 0
        for i in range(num):
            length = length * 256 + der[pos + 1 + i]
        return length, pos + 1 + num

    def read_int(pos):
        length, nxt = read_len(pos + 1)  # expects der[pos] == 0x02
        value = der[nxt:nxt + length]
        if value and value[0] == 0:
            value = value[1:]
        return value, nxt + length

    _, body_start = read_len(1)                       # outer SEQUENCE
    alg_len, alg_payload = read_len(body_start + 1)   # alg SEQUENCE
    bit_string_tag = alg_payload + alg_len
    if der[bit_string_tag] != 0x03:
        raise ValueError("rsa: expected BIT STRING")
    _, bits_content = read_len(bit_string_tag + 1)
    inner_seq = bits_content + 1                      # skip unused-bits byte
    if der[inner_seq] != 0x30:
        raise ValueError("rsa: expected inner SEQUENCE")
    _, ints_start = read_len(inner_seq + 1)
    n, e_start = read_int(ints_start)
    e, _ = read_int(e_start)
    return n, e


SERVER_PUBLIC_KEY = _parse_spki_der(b64decode(_SERVER_PUBLIC_KEY_PEM_BODY))


def _rsa_wrap(message: bytes) -> bytes:
    """Raw RSAES-PKCS1-v1_5 encryption (type-2 padding, non-zero PS bytes)."""
    n, e = SERVER_PUBLIC_KEY
    mod_len = len(n)
    if len(message) > mod_len - 11:
        raise ValueError("rsa: message too long")
    ps_len = mod_len - len(message) - 3
    ps = bytes((b % 0xFE) + 1 for b in secrets.token_bytes(ps_len))
    padded = b"\x00\x02" + ps + b"\x00" + message
    m = int.from_bytes(padded, "big")
    c = pow(m, int.from_bytes(e, "big"), int.from_bytes(n, "big"))
    return c.to_bytes(mod_len, "big")


# --- COSY constants ----------------------------------------------------------------

COSY_VERSION = "1.1.64"
COSY_CLIENT_TYPE = "5"
COSY_CLIENT_IP = "169.254.198.161"
LOGIN_VERSION = "v2"
CLIENT_UA = "Go-http-client/2.0"


# --- session ------------------------------------------------------------------------


class CosySession:
    """Derives per-request COSY material (identity envelope + wrapped key) from
    the credential. Nothing is stored server-side."""

    def __init__(self, credential: dict):
        access_token = credential.get("access_token")
        if not access_token:
            raise ValueError("cosy: empty access token")
        self.uid = credential.get("uid") or ""
        self.access_token = access_token
        self.machine_id = _derive_id(self.uid, "machine")
        self.machine_type = _derive_machine_type(self.uid)
        self._key = credential.get("temp_key")
        self._meta = {
            "refresh_token": credential.get("refresh_token", ""),
            "nickname": credential.get("nickname", ""),
            "user_type": credential.get("user_type", "personal_professional_trial"),
            "org_id": credential.get("org_id", ""),
            "org_name": credential.get("org_name", ""),
        }
        self._material = None
        self.machine_token = ""

    def init(self) -> "CosySession":
        self.machine_token = _derive_machine_token(self.uid)
        key = self._key
        if not key or len(key) != 16:
            key = "".join("0123456789abcdef"[b % 16] for b in secrets.token_bytes(16))
        key_bytes = key.encode("utf-8")
        identity = {
            "name": self._meta["nickname"] or "",
            "aid": self.uid,
            "uid": self.uid,
            "yx_uid": "",
            "organization_id": self._meta["org_id"] or "",
            "organization_name": self._meta["org_name"] or "",
            "user_type": self._meta["user_type"] or "personal_professional_trial",
            "security_oauth_token": self.access_token,
            "refresh_token": self._meta["refresh_token"] or "",
        }
        identity_json = json_sorted_compact(identity).encode("utf-8")
        info = b64encode(aes_cbc_encrypt(identity_json, key_bytes))
        cosy_key = b64encode(_rsa_wrap(key_bytes))
        self._material = {"info": info, "cosy_key": cosy_key}
        return self

    def headers(self, *, body: str, raw_url: str, model_key: str = "",
                sse: bool = True, accept: str = "text/event-stream") -> dict:
        request_id = str(uuid.uuid4())
        payload_b64 = b64encode(json_sorted_compact({
            "cosyVersion": COSY_VERSION, "ideVersion": "", "info": self._material["info"],
            "requestId": request_id, "version": "v1",
        }).encode("utf-8"))
        stamp = str(int(__import__("time").time()))
        path = urllib.parse.urlsplit(raw_url).path
        if path.startswith("/algo"):
            path = path[5:]
        digest = md5_hex("\n".join([payload_b64, self._material["cosy_key"], stamp, body, path]))
        headers = {
            "cosy-data-policy": "AGREE",
            "content-type": "application/json",
            "cosy-machinetype": self.machine_type,
            "cosy-clienttype": COSY_CLIENT_TYPE,
            "cosy-date": stamp,
            "cosy-user": self.uid,
            "cosy-key": self._material["cosy_key"],
            "accept": accept,
            "cosy-clientip": COSY_CLIENT_IP,
            "authorization": f"Bearer COSY.{payload_b64}.{digest}",
            "accept-encoding": "identity",
            "cosy-version": COSY_VERSION,
            "cosy-machineid": self.machine_id,
            "cosy-machinetoken": self.machine_token,
            "login-version": LOGIN_VERSION,
            "user-agent": CLIENT_UA,
        }
        if sse:
            headers["cache-control"] = "no-cache"
        if model_key:
            headers["x-model-key"] = model_key
            headers["x-model-source"] = "system"
        return headers


if __name__ == "__main__":
    # quick self-check: AES known-answer vectors (FIPS-197 + NIST SP 800-38A CBC)
    rk = _expand_key(bytes(range(16)))
    out = _encrypt_block(rk, bytes.fromhex("00112233445566778899aabbccddeeff"))
    assert out.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", out.hex()
    # CBC chain check: with IV == key, block 1 must equal ECB(P xor K)
    P = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
    P2 = bytes.fromhex("ae2d8a571e03ac9c9eb76fac45af8e51")
    K = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
    rk2 = _expand_key(K)
    expect = _encrypt_block(rk2, bytes(p ^ q for p, q in zip(P, K)))
    cbc = aes_cbc_encrypt(P + P2, K)  # 32-byte plaintext -> no padding blocks
    assert cbc[:16] == expect, (cbc[:16].hex(), expect.hex())
    # and a second block chains: ECB(P2 xor C1)
    expect2 = _encrypt_block(rk2, bytes(p ^ q for p, q in zip(P2, cbc[:16])))
    assert cbc[16:32] == expect2, (cbc[16:32].hex(), expect2.hex())
    assert _SBOX[0] == 0x63 and _SBOX[1] == 0x7C and _SBOX[0x53] == 0xED
    n, e = SERVER_PUBLIC_KEY
    assert len(n) == 128 and e == b"\x01\x00\x01", (len(n), e.hex())
    sample = qoder_encode("hello world".encode("utf-8") * 3)
    print("cosy.py self-check OK")
    print("  qoder_encode('hello world'x3) =", sample)
    print("  md5_hex('abc') =", md5_hex("abc"))
    print("  machine fingerprints (uid='u1'):")
    print("    id    =", _derive_id("u1", "machine"))
    print("    type  =", _derive_machine_type("u1"))
    print("    token =", _derive_machine_token("u1"))
